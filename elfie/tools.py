"""
Elfie's tools — what she can do beyond conversation.

Each tool is a method on ElfieTools decorated with @function_tool.
The LLM discovers and calls them automatically when relevant.

To add your own tool:
  1. Add a method here with @function_tool
  2. Give it a clear docstring — the LLM reads it to decide when to call it
  3. That's it. No registration needed.
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx
from livekit.agents import function_tool, RunContext

from elfie.config import CONFIG

logger = logging.getLogger("elfie.tools")

NOTES_FILE = Path(__file__).parent.parent / "data" / "notes.json"
REMINDERS_FILE = Path(__file__).parent.parent / "data" / "reminders.json"
TOOL_USAGE_FILE = Path(__file__).parent.parent / "data" / "tool_usage.jsonl"


def record_use(tool: str) -> None:
    """Append a one-line tool-usage event so the dashboard can light up a tile.

    Best-effort and silent: a logging failure must never break the actual tool.
    """
    try:
        TOOL_USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with TOOL_USAGE_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"tool": tool, "ts": datetime.now(timezone.utc).isoformat()}) + "\n")
    except Exception:
        pass


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


class ElfieTools:
    """
    Mixin class — add to ElfieAgent so the LLM can call these methods.

    All methods must be async, return str, and have a docstring that
    describes when to use them.
    """

    @function_tool
    async def get_current_time(self, context: RunContext) -> str:
        """Returns the current local date and time. Call when the user asks what time or day it is."""
        record_use("get_current_time")
        return datetime.now().strftime("%A, %B %d at %I:%M %p")

    @function_tool
    async def search_web(self, context: RunContext, query: str) -> str:
        """
        Searches the web for current information.
        Call for: weather, news, sports scores, prices, facts, anything time-sensitive.
        """
        record_use("search_web")
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.get(
                    "https://api.duckduckgo.com/",
                    params={"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"},
                )
                data = r.json()
                # Prefer the instant-answer abstract
                abstract = data.get("AbstractText", "").strip()
                if abstract:
                    return abstract[:400]
                # Fall back to first related topic
                topics = data.get("RelatedTopics", [])
                if topics and isinstance(topics[0], dict):
                    return topics[0].get("Text", "")[:400]
                return "No clear result found — try a more specific query."
        except Exception as e:
            logger.warning(f"Web search failed: {e}")
            return "I couldn't reach the web right now."

    @function_tool
    async def take_note(self, context: RunContext, note: str) -> str:
        """
        Saves a note for the user.
        Call when the user says 'remember', 'note that', 'write this down', or similar.
        """
        record_use("take_note")
        try:
            data = _read_json(NOTES_FILE)
            notes = data.get("notes", [])
            notes.append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "text": note,
            })
            _write_json(NOTES_FILE, {"notes": notes})
            logger.info(f"[Note] Saved: {note!r}")
            return f"Got it, I've noted: {note}"
        except Exception as e:
            logger.error(f"Failed to save note: {e}")
            return "Sorry, I couldn't save that note."

    @function_tool
    async def read_notes(self, context: RunContext) -> str:
        """
        Reads back saved notes.
        Call when the user asks what they noted, or to recall something they asked Elfie to remember.
        """
        record_use("read_notes")
        data = _read_json(NOTES_FILE)
        notes = data.get("notes", [])
        if not notes:
            return "You haven't saved any notes yet."
        recent = notes[-5:]  # last 5 notes
        lines = [f"- {n['text']}" for n in recent]
        return "Your recent notes: " + "; ".join(n["text"] for n in recent)

    @function_tool
    async def delegate_task(self, context: RunContext, task_description: str, short_title: str) -> str:
        """
        Hands a task to a background coding/research agent (Claude Code) on this machine.
        Call for anything that needs real work: writing code, deep multi-step research,
        analysing or producing files, anything taking more than a minute.
        The result arrives later as a report — tell the user it's started and that
        you'll announce when it's done. Don't wait for it.
        """
        record_use("delegate_task")
        from elfie.delegate import MANAGER
        if not CONFIG.delegate.enabled:
            return "Delegation is disabled in config."
        task = MANAGER.start(task_description, kind="claude", title=short_title)
        return (
            f"Background agent started on '{short_title}' (task {task['id']}). "
            "Tell the user it's running and you'll let them know when the report is ready."
        )

    @function_tool
    async def write_report(self, context: RunContext, title: str, request: str) -> str:
        """
        Writes a detailed report on a topic instead of reading it aloud.
        Call when a proper answer would be long, technical, or need tables/code —
        give the user a one-sentence plain version out loud, and put the depth here.
        The 'request' param should fully describe what the report must cover.
        """
        record_use("write_report")
        from elfie.delegate import MANAGER
        task = MANAGER.start(request, kind="report", title=title)
        return (
            f"Report '{title}' is being written (task {task['id']}). "
            "Give the user the short spoken version now; the full report lands "
            "in their dashboard in under a minute and you'll announce it."
        )

    @function_tool
    async def continue_task(self, context: RunContext, follow_up_instructions: str) -> str:
        """
        Continues the most recent finished background task — same agent session,
        same working folder, full memory of the earlier work.
        Call when the user says 'expand on that', 'continue that work',
        'add to it', or asks for changes to something a task already produced.
        """
        record_use("continue_task")
        from elfie.delegate import MANAGER
        prior = next(
            (t for t in MANAGER.list_tasks(20)
             if t["kind"] == "claude" and t["status"] == "done" and t.get("claude_session_id")),
            None,
        )
        if prior is None:
            return "There's no finished task to continue — use delegate_task to start fresh."
        task = MANAGER.start(
            follow_up_instructions,
            kind="claude",
            title=prior["title"].lstrip("↳ "),
            continue_of=prior["id"],
        )
        return (
            f"Continuing '{prior['title']}' (task {task['id']}) with the follow-up. "
            "Tell the user the same agent is expanding its earlier work and "
            "you'll announce the updated report."
        )

    @function_tool
    async def learn_skill(self, context: RunContext, what_it_should_do: str, short_name: str) -> str:
        """
        Teaches Elfie a NEW ability she doesn't have yet. A background agent
        researches existing APIs, writes the skill, and it goes live after an
        automated safety check — usable on the user's next connect.
        Call when the user asks for something no current tool can do, or says
        'learn to...', 'can you add the ability to...', 'I wish you could...'.
        short_name: snake_case, e.g. 'crypto_price' or 'weather_forecast'.
        """
        record_use("learn_skill")
        from elfie.delegate import MANAGER
        task = MANAGER.start(
            what_it_should_do,
            kind="skill",
            title=f"Learn skill: {short_name}",
            skill_name=short_name,
        )
        return (
            f"Skill builder started (task {task['id']}). Tell the user you're "
            "learning it — research, code, and a safety check take a few "
            "minutes, and the new ability works from their next session."
        )

    @function_tool
    async def check_email(self, context: RunContext) -> str:
        """
        Checks the owner's Gmail inbox for unread emails.
        Call when the user asks about new mail, unread messages, reading their
        email, or whether anything important came in.
        Returns the unread count and latest senders/subjects to summarize aloud.
        """
        record_use("check_email")
        if not CONFIG.gmail_address or not CONFIG.gmail_app_password:
            return (
                "Gmail isn't set up yet. Tell the user: create an app password at "
                "myaccount.google.com/apppasswords and add GMAIL_ADDRESS and "
                "GMAIL_APP_PASSWORD to the .env file."
            )
        import asyncio

        from elfie import gmail_imap
        try:
            result = await asyncio.to_thread(
                gmail_imap.check_unread, CONFIG.gmail_address, CONFIG.gmail_app_password, 5
            )
        except Exception as e:
            logger.warning(f"Gmail check failed: {e}")
            return "I couldn't reach Gmail — the app password may be wrong or expired."
        if result["count"] == 0:
            return "No unread emails."
        lines = [f"{result['count']} unread."]
        lines += [f"{m['subject']} — from {m['from']}" for m in result["messages"]]
        return " | ".join(lines)

    @function_tool
    async def open_website(self, context: RunContext, url: str) -> str:
        """
        Opens any website or URL in the user's web browser.
        Call when the user asks to open, go to, or bring up a site
        ('open google', 'go to github dot com', 'open youtube for me').
        Convert spoken forms to a real domain: 'google dot com' -> 'google.com'.
        For bare site names, use the obvious domain ('youtube' -> 'youtube.com').
        """
        record_use("open_website")
        from elfie import notify
        url = url.strip().replace(" ", "")
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        ok = notify.open_in_browser(url)
        return f"Opened {url} in the browser." if ok else "I couldn't open a browser on this machine."

    @function_tool
    async def show_report(self, context: RunContext) -> str:
        """
        Opens the most recent report in the user's browser, right in front of them.
        Call when the user says 'show me', 'open it', 'bring it up', 'put it on
        my screen', or asks to see a report.
        """
        record_use("show_report")
        from elfie import notify, reports
        items = reports.list_reports(limit=1)
        if not items:
            return "There are no reports yet."
        ok = notify.open_in_browser(reports.report_url(items[0]["id"]))
        return (
            f"Opened '{items[0]['title']}' in the browser."
            if ok else "I couldn't open a browser on this machine."
        )

    @function_tool
    async def check_tasks(self, context: RunContext) -> str:
        """
        Lists recent background tasks and their status.
        Call when the user asks how a delegated task or report is going.
        """
        record_use("check_tasks")
        from elfie.delegate import MANAGER
        tasks = MANAGER.list_tasks(limit=5)
        if not tasks:
            return "No background tasks yet."
        lines = []
        for t in tasks:
            status = t["status"]
            if status == "failed" and t.get("error"):
                status += f" ({t['error'][:80]})"
            lines.append(f"{t['title']}: {status}")
        return "Recent tasks — " + "; ".join(lines)

    @function_tool
    async def set_reminder(self, context: RunContext, text: str, when: str) -> str:
        """
        Saves a reminder with a time description.
        Call when the user says 'remind me to', 'don't let me forget', or similar.
        The 'when' param is a natural-language time like 'in 30 minutes' or 'at 3pm'.
        """
        record_use("set_reminder")
        try:
            data = _read_json(REMINDERS_FILE)
            reminders = data.get("reminders", [])
            reminders.append({
                "created": datetime.now(timezone.utc).isoformat(),
                "text": text,
                "when": when,
                "done": False,
            })
            _write_json(REMINDERS_FILE, {"reminders": reminders})
            logger.info(f"[Reminder] Set: {text!r} for {when!r}")
            return f"Reminder set: {text} — {when}"
        except Exception as e:
            logger.error(f"Failed to save reminder: {e}")
            return "Sorry, I couldn't set that reminder."
