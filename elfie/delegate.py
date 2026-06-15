"""
Elfie delegation — hand work to background agents, get a report back.

Two kinds of background task:

  "claude"  — runs Claude Code headless (`claude -p ...`) on this machine.
              For real work: coding, multi-step research, analysing files.
              Each task gets its own workspace folder so produced files
              don't collide: data/workspace/<task_id>/

  "report"  — a single long-form LLM call (Groq) that writes a structured
              markdown report. For explanations too long to speak aloud.
              Much cheaper and faster than spinning up Claude Code.

Both end the same way: a report in data/reports/, the task marked done,
and every registered on_complete callback fired (the agent uses this to
announce the result in voice + chat + Telegram).

Usage:
    from elfie.delegate import MANAGER
    task = MANAGER.start("research X and summarise", kind="claude")
    MANAGER.on_complete.append(my_async_callback)   # (task_dict, report_meta)
"""
import asyncio
import json
import logging
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from elfie import reports
from elfie.config import CONFIG

logger = logging.getLogger("elfie.delegate")

TASKS_FILE = Path(__file__).parent.parent / "data" / "tasks.json"
WORKSPACE_DIR = Path(__file__).parent.parent / "data" / "workspace"

CLAUDE_PROMPT_TEMPLATE = """\
You are a background agent working for Elfie, a voice assistant. The user \
asked for this over voice and will read your output later as a report.

TASK: {description}

Rules:
- Do the work fully — don't ask clarifying questions, make sensible assumptions \
and state them.
- Your final message must be a complete, well-structured markdown report: \
a one-paragraph TL;DR first, then sections. Use tables where they help.
- Save any files you produce (code, data, documents) in the current directory.
"""

SKILL_BUILDER_PROMPT = """\
You are building a new SKILL (voice tool) for Elfie, a personal voice assistant.

THE USER ASKED FOR: {description}

STEP 1 — RESEARCH FIRST. Use web search to check whether an existing free or
cheap API/service already solves this. Strongly prefer wrapping an existing
API over building logic from scratch. If a key is needed, the skill must read
it from os.getenv and degrade gracefully when missing.

STEP 2 — WRITE exactly one self-contained Python file at: {staging_path}
Contract (violations get the skill rejected by an automated gate):
- `from livekit.agents import function_tool, RunContext`
- One or more `async def` functions decorated with @function_tool. Each
  docstring must clearly say WHEN the assistant should call it — that is the
  only thing the voice LLM reads.
- Module-level `TOOLS = [...]` listing those functions.
- Python stdlib + httpx ONLY. Never: subprocess, ctypes, socket, pickle,
  eval/exec, os.system, file deletion. File writes only under ./data/.
- Secrets only via os.getenv("SOME_KEY"); when missing, return a short spoken
  setup instruction instead of raising.
- Return values are SPOKEN ALOUD: short sentences, no markdown, no URLs
  unless asked.
- Handle API errors with a friendly spoken message; timeout all HTTP at 10s.

STEP 3 — REPORT: what the skill does, which API you chose and why (vs the
alternatives you researched), any env keys the user must add to .env, and
three example voice commands that would trigger it.
"""

REPORT_SYSTEM_PROMPT = """\
You write reports for a voice assistant's dashboard. The user asked something \
too long or technical to answer aloud, so you write the full version they read \
on screen.

Structure every report as markdown:
- Start with a 2-3 sentence TL;DR in plain language.
- Then clear sections with headers. Tables for comparisons, code blocks for code.
- Explain like the reader is smart but not an expert in this topic.
- No preamble like "Here is your report" — start with the content.
"""


def _read_tasks() -> list[dict]:
    try:
        return json.loads(TASKS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _write_tasks(tasks: list[dict]) -> None:
    TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    TASKS_FILE.write_text(json.dumps(tasks, indent=2, ensure_ascii=False), encoding="utf-8")


class DelegationManager:
    """
    Owns the background-task lifecycle. One instance per worker process
    (module-level MANAGER below) — tasks survive across voice sessions.
    """

    def __init__(self) -> None:
        self._tasks: list[dict] = _read_tasks()
        self._running: set[str] = set()
        # Async callbacks fired on completion: (task, report_meta | None)
        self.on_complete: list[Callable[[dict, dict | None], Awaitable[None]]] = []

    # ── Public API ──────────────────────────────────────────────────────────

    def start(
        self,
        description: str,
        kind: str = "claude",
        title: str = "",
        continue_of: str | None = None,
        skill_name: str = "",
    ) -> dict:
        """
        Create a task and run it in the background. Returns immediately.

        continue_of: id of a finished claude task — the new task resumes that
        task's Claude Code session in the same workspace folder, with full
        memory of the earlier work.
        """
        # Debounce: spoken requests often arrive as fragments ("...keep me
        # posted", "when it's done") that re-trigger the same delegation.
        # If a task of this kind started seconds ago and is still running,
        # treat this as the same request.
        now = datetime.now(timezone.utc)
        if continue_of is None:   # deliberate follow-ups are never duplicates
            for t in self._tasks[-5:]:
                if t["kind"] == kind and t["status"] == "running":
                    age = (now - datetime.fromisoformat(t["created"])).total_seconds()
                    if age < 90:
                        logger.info(f"[Delegate] Debounced duplicate {kind} task ({t['id']} is {age:.0f}s old)")
                        return t

        task = {
            "id": uuid.uuid4().hex[:8],
            "kind": kind,
            "title": ("↳ " if continue_of else "") + (title or description[:60]),
            "description": description,
            "status": "running",
            "created": datetime.now(timezone.utc).isoformat(),
            "finished": None,
            "report_id": None,
            "error": None,
            "continue_of": continue_of,
            "skill_name": skill_name,
        }
        self._tasks.append(task)
        self._running.add(task["id"])
        _write_tasks(self._tasks)

        runners = {"claude": self._run_claude, "report": self._run_report, "skill": self._run_skill_builder}
        runner = runners[kind]
        asyncio.ensure_future(self._run_safely(task, runner))
        logger.info(f"[Delegate] Started {kind} task {task['id']}: {task['title']!r}")
        return task

    def list_tasks(self, limit: int = 10) -> list[dict]:
        """Newest first."""
        return list(reversed(self._tasks))[:limit]

    def running_count(self) -> int:
        return len(self._running)

    # ── Runners ─────────────────────────────────────────────────────────────

    async def _run_safely(self, task: dict, runner) -> None:
        try:
            report_meta = await runner(task)
            task["status"] = "done"
            task["report_id"] = report_meta["id"] if report_meta else None
        except asyncio.TimeoutError:
            task["status"] = "timeout"
            task["error"] = f"Exceeded {CONFIG.delegate.timeout_minutes} minute limit"
            report_meta = None
        except Exception as e:
            task["status"] = "failed"
            task["error"] = str(e)[:300]
            report_meta = None
            logger.error(f"[Delegate] Task {task['id']} failed: {e}")
        finally:
            task["finished"] = datetime.now(timezone.utc).isoformat()
            self._running.discard(task["id"])
            _write_tasks(self._tasks)

        for callback in self.on_complete:
            try:
                await callback(task, report_meta)
            except Exception as e:
                logger.warning(f"[Delegate] Completion callback failed: {e}")

    async def _run_claude(self, task: dict) -> dict:
        """Run Claude Code headless in a per-task workspace."""
        claude = shutil.which(CONFIG.delegate.claude_path)
        if not claude:
            raise RuntimeError(
                f"Claude Code not found ({CONFIG.delegate.claude_path!r}). "
                "Install it or set CLAUDE_PATH in .env."
            )

        # Continuation: reuse the original task's workspace and resume its
        # Claude Code session so all prior context carries over.
        resume_args: list[str] = []
        workspace = WORKSPACE_DIR / task["id"]
        if task.get("continue_of"):
            prior = next((t for t in self._tasks if t["id"] == task["continue_of"]), None)
            if prior and prior.get("claude_session_id"):
                workspace = WORKSPACE_DIR / prior["id"]
                resume_args = ["--resume", prior["claude_session_id"]]
        workspace.mkdir(parents=True, exist_ok=True)

        cmd = [
            claude,
            "-p", CLAUDE_PROMPT_TEMPLATE.format(description=task["description"]),
            "--output-format", "json",
            "--permission-mode", CONFIG.delegate.permission_mode,
            *resume_args,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=CONFIG.delegate.timeout_minutes * 60
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise

        if proc.returncode != 0:
            detail = (stderr.decode().strip() or stdout.decode().strip())[-300:]
            raise RuntimeError(f"claude exited {proc.returncode}: {detail}")

        # Headless JSON output: {"type": "result", "result": "<final text>",
        # "session_id": "..."} — keep the session id so the work can be resumed.
        try:
            result = json.loads(stdout.decode())
            body = result.get("result", "") or "(Claude Code returned no text.)"
            if result.get("session_id"):
                task["claude_session_id"] = result["session_id"]
        except json.JSONDecodeError:
            body = stdout.decode()

        produced = [str(p.relative_to(workspace)) for p in workspace.rglob("*") if p.is_file()]
        if produced:
            body += "\n\n---\n\n**Files produced** (in `data/workspace/" + task["id"] + "/`):\n"
            body += "\n".join(f"- `{f}`" for f in produced)

        return reports.create_report(
            title=task["title"],
            content=body,
            source="claude-code",
            task_id=task["id"],
            files=produced,
        )

    async def _run_skill_builder(self, task: dict) -> dict:
        """
        Self-extension: Claude Code researches existing APIs, writes a new
        skill into staging; the validation gate decides whether it goes live.
        """
        from elfie import skills as skill_system

        claude = shutil.which(CONFIG.delegate.claude_path)
        if not claude:
            raise RuntimeError("Claude Code not found — can't build skills.")

        skill_name = task.get("skill_name") or "new_skill"
        workspace = WORKSPACE_DIR / task["id"]
        workspace.mkdir(parents=True, exist_ok=True)
        staging_path = workspace / f"{skill_name}.py"

        prompt = SKILL_BUILDER_PROMPT.format(
            description=task["description"], staging_path=staging_path
        )
        proc = await asyncio.create_subprocess_exec(
            claude, "-p", prompt,
            "--output-format", "json",
            "--permission-mode", CONFIG.delegate.permission_mode,
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=CONFIG.delegate.timeout_minutes * 60
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise
        if proc.returncode != 0:
            detail = (stderr.decode().strip() or stdout.decode().strip())[-300:]
            raise RuntimeError(f"skill builder exited {proc.returncode}: {detail}")

        try:
            body = json.loads(stdout.decode()).get("result", "")
        except json.JSONDecodeError:
            body = stdout.decode()

        # The guardrail: nothing reaches the live skills folder unvalidated.
        if not staging_path.exists():
            raise RuntimeError(f"builder produced no file at {staging_path.name}")
        rejection = skill_system.validate_skill(staging_path)
        if rejection:
            body = (
                f"# Skill '{skill_name}' REJECTED by the safety gate\n\n"
                f"**Reason:** {rejection}\n\nThe file stays in staging "
                f"(`{staging_path}`) for review — nothing was installed.\n\n---\n\n" + body
            )
            return reports.create_report(
                title=f"Skill rejected: {skill_name}", content=body,
                source="skill-builder", task_id=task["id"],
            )

        installed = skill_system.install_skill(staging_path, skill_name)
        body = (
            f"# New skill installed: `{installed.stem}` ✅\n\n"
            "Passed the safety gate (syntax, banned-operations scan, isolated "
            "import test) and was git-committed. **Available the next time you "
            "connect.**\n\n---\n\n" + body
        )
        return reports.create_report(
            title=f"New skill: {installed.stem}", content=body,
            source="skill-builder", task_id=task["id"],
            files=[installed.name],
        )

    async def _run_report(self, task: dict) -> dict:
        """Write a long-form report with one Groq call — no Claude needed."""
        from groq import AsyncGroq

        client = AsyncGroq(api_key=CONFIG.groq_api_key)

        async def generate(model: str):
            return await asyncio.wait_for(
                client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": REPORT_SYSTEM_PROMPT},
                        {"role": "user", "content": task["description"]},
                    ],
                    max_tokens=2048,
                    temperature=0.4,
                ),
                timeout=60.0,
            )

        try:
            resp = await generate(CONFIG.voice.llm_model)
        except Exception:   # rate limit etc. — Groq limits are per model
            resp = await generate(CONFIG.voice.llm_fallback_model)
        body = resp.choices[0].message.content or "(No content generated.)"
        return reports.create_report(
            title=task["title"],
            content=body,
            source="elfie",
            task_id=task["id"],
        )


# Single shared instance — tasks persist across voice sessions
MANAGER = DelegationManager()
