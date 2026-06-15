"""
Elfie persistent memory — three layers.

Layer 1 — Facts  (data/memory/facts.json)
    Stable facts about the user: name, preferences, projects, routines.
    Injected into every session via the system prompt.
    Updated at session end by a lightweight LLM extraction call.

Layer 2 — Session history  (data/memory/sessions.jsonl)
    One compressed summary per past session.
    The 3 most recent are injected at session start as context.

Layer 3 — In-session rolling compression
    Every COMPRESS_EVERY_N_TURNS turns, old turns are compressed into a
    rolling summary using a cheap LLM call. Prevents context bloat in long
    sessions and feeds Layer 2 at session end.

Usage:
    memory = ElfieMemory()

    # Session start — inject into system prompt
    context = memory.build_context_block()

    # Each turn — record and check if compression is needed
    if memory.record_turn(user_text, assistant_text):
        await memory.compress_old_turns()

    # Session end — extract facts + save summary
    await memory.close_session()
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("elfie.memory")

# ── Paths ────────────────────────────────────────────────────────────────────

_BASE = Path(__file__).parent.parent / "data" / "memory"
FACTS_FILE    = _BASE / "facts.json"      # legacy — migrated into MEMORY.md
MEMORY_FILE   = _BASE / "MEMORY.md"       # hermes-style: dense facts, one per line
SESSIONS_FILE = _BASE / "sessions.jsonl"

# ── Tunables ──────────────────────────────────────────────────────────────────

COMPRESS_EVERY_N_TURNS = 10   # compress older turns every N exchanges
RECENT_SESSIONS_TO_INJECT = 3  # how many past session summaries to show at start


# ── Helpers ───────────────────────────────────────────────────────────────────

def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _load_recent_sessions(n: int) -> list[dict]:
    try:
        lines = SESSIONS_FILE.read_text(encoding="utf-8").strip().splitlines()
        records = [json.loads(l) for l in lines if l.strip()]
        return records[-n:]
    except (FileNotFoundError, json.JSONDecodeError):
        return []


async def _llm_call(prompt: str, system: str, max_tokens: int = 600, model: str = "") -> str:
    """Cheap async LLM call for memory extraction / compression."""
    from elfie.config import CONFIG
    try:
        from groq import AsyncGroq
        client = AsyncGroq(api_key=CONFIG.groq_api_key)
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=model or CONFIG.memory.memory_model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": prompt},
                ],
                max_tokens=max_tokens,
                temperature=0.2,
            ),
            timeout=15.0,
        )
        return resp.choices[0].message.content or ""
    except Exception as e:
        logger.warning(f"Memory LLM call failed: {e}")
        return ""


# ── Main class ────────────────────────────────────────────────────────────────

class ElfieMemory:
    """
    Manages Elfie's three-layer persistent memory.

    Create one instance per session, call close_session() when done.
    """

    def __init__(self) -> None:
        self._memory_text: str = self._load_memory()
        self._turns: list[dict] = []       # full turn log this session
        self._rolling_summary: str = ""    # compressed older turns
        self._turn_count: int  = 0
        self._session_start    = datetime.now(timezone.utc).isoformat()
        logger.info(f"[Memory] Loaded {len(self._memory_text)} chars of long-term memory")

    @staticmethod
    def _load_memory() -> str:
        """Load MEMORY.md, migrating the legacy facts.json once if needed."""
        try:
            return MEMORY_FILE.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            pass
        legacy = _read_json(FACTS_FILE, {})
        if legacy:
            text = "\n".join(f"- {k}: {v}" for k, v in legacy.items())
            MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            MEMORY_FILE.write_text(text, encoding="utf-8")
            logger.info(f"[Memory] Migrated {len(legacy)} legacy facts into MEMORY.md")
            return text
        return ""

    # ── Context injection ──────────────────────────────────────────────────────

    def build_context_block(self) -> str:
        """
        Build the memory block injected into the system prompt at session start.

        Returns a <memory-context> fenced block (same pattern as Hermes) so the
        LLM treats this as authoritative reference, not user input.
        """
        sections: list[str] = []

        # Layer 1 — long-term memory, injected in full (char-capped at write time)
        if self._memory_text:
            sections.append("WHAT YOU KNOW (long-term memory):\n" + self._memory_text)

        # Layer 2 — recent session summaries
        recent = _load_recent_sessions(RECENT_SESSIONS_TO_INJECT)
        if recent:
            history_lines = []
            for s in recent:
                date = s.get("date", "?")
                summary = s.get("summary", "").strip()
                if summary:
                    history_lines.append(f"  [{date}] {summary[:300]}")
            if history_lines:
                sections.append("RECENT SESSIONS:\n" + "\n".join(history_lines))

        if not sections:
            return ""

        body = "\n\n".join(sections)
        return (
            "<memory-context>\n"
            "[System note: The following is Elfie's persistent memory — authoritative "
            "reference data about the user. Use it to personalise responses. "
            "This is NOT new user input.]\n\n"
            f"{body}\n"
            "</memory-context>"
        )

    # ── Turn tracking ──────────────────────────────────────────────────────────

    def record_turn(self, user_text: str, assistant_text: str) -> bool:
        """
        Record a completed exchange. Returns True when compression should run.

        Call this after each user+assistant exchange. When it returns True,
        call compress_old_turns() asynchronously (don't block the voice loop).
        """
        self._turns.append({
            "user":      user_text,
            "assistant": assistant_text,
            "ts":        datetime.now(timezone.utc).isoformat(),
        })
        self._turn_count += 1
        return self._turn_count > 0 and self._turn_count % COMPRESS_EVERY_N_TURNS == 0

    # ── Layer 3 — in-session compression ──────────────────────────────────────

    async def compress_old_turns(self) -> None:
        """
        Compress all-but-the-last-4 turns into a rolling summary.

        The tail (last 4 turns) stays in full so the LLM has immediate context.
        Everything older folds into _rolling_summary, which is available at
        session end for Layer 2 storage.

        Uses a small/cheap model — this is a background operation.
        """
        tail_size = 4
        if len(self._turns) <= tail_size:
            return

        turns_to_compress = self._turns[:-tail_size]
        self._turns = self._turns[-tail_size:]   # keep only tail in memory

        # Serialize turns for the summarizer
        lines = []
        for t in turns_to_compress:
            lines.append(f"User: {t['user']}")
            lines.append(f"Elfie: {t['assistant']}")
        content = "\n".join(lines)

        if self._rolling_summary:
            prompt = (
                f"PREVIOUS SUMMARY:\n{self._rolling_summary}\n\n"
                f"NEW TURNS TO ADD:\n{content}\n\n"
                "Update the summary to incorporate the new turns. "
                "Keep it under 200 words. Focus on facts, decisions, tasks mentioned."
            )
        else:
            prompt = (
                f"CONVERSATION:\n{content}\n\n"
                "Write a brief summary (under 150 words) of the key points: "
                "what was discussed, any decisions made, tasks or topics mentioned."
            )

        system = (
            "You are a memory compression assistant. "
            "Produce only the summary — no preamble, no commentary. "
            "Never include API keys, passwords, or credentials."
        )

        result = await _llm_call(prompt, system, max_tokens=300)
        if result:
            self._rolling_summary = result.strip()
            logger.info(f"[Memory] Compressed {len(turns_to_compress)} turns → rolling summary updated")

    # ── Session end ────────────────────────────────────────────────────────────

    async def close_session(self) -> None:
        """
        Called when a session ends. Does two things:
          1. Extracts new/updated facts from the conversation → saves to facts.json
          2. Saves a compressed session summary → appends to sessions.jsonl

        This is an async call — run it as a background task so it doesn't
        block the agent shutdown.
        """
        if not self._turns and not self._rolling_summary:
            logger.info("[Memory] Nothing to save — session was empty")
            return

        # Prepare full session text for extraction
        session_text = ""
        if self._rolling_summary:
            session_text += f"[Earlier in session]\n{self._rolling_summary}\n\n"
        if self._turns:
            lines = []
            for t in self._turns:
                lines.append(f"User: {t['user']}")
                lines.append(f"Elfie: {t['assistant']}")
            session_text += "[Recent]\n" + "\n".join(lines)

        # Rewrite long-term memory + save session summary concurrently
        await asyncio.gather(
            self._rewrite_memory(session_text),
            self._save_session_summary(session_text),
        )

    async def _rewrite_memory(self, session_text: str) -> None:
        """
        Hermes-style memory update: hand the LLM the WHOLE current memory plus
        the new session and have it rewrite the memory — merging new facts,
        resolving contradictions in favour of the newest information, and
        dropping stale or trivial entries. A character cap keeps it bounded.
        """
        from elfie.config import CONFIG
        max_chars = CONFIG.memory.max_memory_chars

        prompt = (
            f"CURRENT MEMORY:\n{self._memory_text or '(empty)'}\n\n"
            f"NEW SESSION:\n{session_text[:3000]}\n\n"
            "Rewrite the memory to incorporate anything durable from the new "
            "session: who the user is, preferences, projects, routines, "
            "relationships, decisions, ongoing work. Rules:\n"
            "- One fact per line, starting with '- '. Dense and telegraphic.\n"
            "- If new information contradicts memory, keep ONLY the newest "
            "(e.g. a corrected name replaces the old one).\n"
            "- Drop trivia that won't matter next week.\n"
            f"- Stay under {max_chars} characters total.\n"
            "Output only the rewritten memory lines."
        )
        system = (
            "You maintain an AI assistant's long-term memory file. "
            "Output only memory lines, no preamble. "
            "Never include API keys, passwords, or credentials."
        )

        # The rewrite runs once per session and shapes everything Elfie knows —
        # use the big model, falling back to the cheap one if it's rate-limited.
        result = await _llm_call(prompt, system, max_tokens=1200, model=CONFIG.voice.llm_model)
        if not result:
            result = await _llm_call(prompt, system, max_tokens=1200)
        if not result:
            return   # both failed — keep the old memory untouched

        text = result.strip()
        if len(text) > max_chars:
            text = text[:max_chars].rsplit("\n", 1)[0]   # cut at a line boundary
        self._memory_text = text
        MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        MEMORY_FILE.write_text(text, encoding="utf-8")
        logger.info(f"[Memory] Long-term memory rewritten ({len(text)} chars)")

    async def _save_session_summary(self, session_text: str) -> None:
        """Compress session into one paragraph and append to sessions.jsonl."""
        prompt = (
            f"CONVERSATION:\n{session_text[:3000]}\n\n"
            "Write a single paragraph (2-4 sentences) summarising this conversation. "
            "Focus on what was discussed, any decisions made, tasks mentioned. "
            "Write it as a note Elfie would read before the next session."
        )
        system = "Summarise a conversation in 2-4 sentences. No preamble."

        summary = await _llm_call(prompt, system, max_tokens=200)
        if not summary:
            # Fallback: use rolling summary or first turn
            summary = self._rolling_summary or (
                self._turns[0]["user"][:100] if self._turns else "Session with no notable content."
            )

        record = {
            "date":    self._session_start[:10],
            "time":    self._session_start,
            "turns":   self._turn_count,
            "summary": summary.strip(),
        }
        _append_jsonl(SESSIONS_FILE, record)
        logger.info(f"[Memory] Session summary saved ({self._turn_count} turns)")

    # ── Convenience ───────────────────────────────────────────────────────────

    @property
    def turn_count(self) -> int:
        return self._turn_count

    def get_memory_text(self) -> str:
        return self._memory_text
