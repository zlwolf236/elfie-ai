#!/usr/bin/env python3
"""
Elfie — always-on personal voice AI agent.

Stack:
  VAD  : Silero (local, free — only sends audio when you speak)
  STT  : Deepgram Nova-3
  LLM  : Groq Llama-3.3-70B
  TTS  : Cartesia Sonic-2

Run locally:
  python elfie_agent.py start \\
      --url ws://localhost:7880 --api-key devkey --api-secret secret

Deploy to Fly.io:
  fly deploy
"""
import asyncio
import logging
import os
import re
import time

from livekit.agents import AutoSubscribe, JobContext, WorkerOptions, cli, metrics
from livekit.agents import llm as llm_lib
from livekit.agents.voice import Agent, AgentSession
from livekit.plugins import cartesia, deepgram, silero
from livekit.plugins import groq as groq_plugin

from elfie import notify
from elfie.config import CONFIG
from elfie.delegate import MANAGER as DELEGATE
from elfie.memory import ElfieMemory
from elfie.persona import build_greeting, build_system_prompt
from elfie.session import SessionMetrics, log_session
from elfie.tools import ElfieTools

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("elfie")

# Phrases that signal the LLM has naturally ended the conversation
GOODBYE_PHRASES = ("goodbye", "take care", "talk soon", "bye", "farewell", "have a good")

# Open models occasionally leak tool-call syntax as text instead of calling
# the tool ('<open_website>{"url": ...}</function>'). Strip anything that
# looks like markup or a JSON payload before it reaches TTS.
_TOOL_LEAK_RE = re.compile(r"<[^<>]{0,80}>|\{\s*\"[^\n}]{0,200}\}")


async def _scrub_tool_leaks(chunks):
    """Filter a TTS text stream, buffering across chunk boundaries so split
    tags ('<open_web' + 'site>') still get caught."""
    buffer = ""
    async for chunk in chunks:
        buffer += chunk
        # Hold back a potentially unfinished tag/JSON tail
        cut = max(buffer.rfind("<"), buffer.rfind("{"))
        if cut != -1 and len(buffer) - cut < 250:
            emit, buffer = buffer[:cut], buffer[cut:]
        else:
            emit, buffer = buffer, ""
        emit = _TOOL_LEAK_RE.sub("", emit)
        if emit:
            yield emit
    if buffer:
        yield _TOOL_LEAK_RE.sub("", buffer)


async def _announce_finished_task(task: dict, report_meta: dict | None) -> None:
    """
    Fired by the delegation manager whenever a background task ends.
    Delivers on every channel the user has open; Telegram still works
    when no voice session is live.
    """
    notifier = notify.get_active() or notify.Notifier()
    if task["status"] == "done" and report_meta:
        await notifier.announce_report(
            report_meta,
            spoken_line=f"Your report on {task['title']} is ready — it's in your dashboard.",
        )
    else:
        await notifier.send_chat(f"⚠️ Task '{task['title']}' {task['status']}: {task.get('error') or ''}")
        await notifier.speak(f"Heads up — the task on {task['title']} didn't finish. It {task['status']}.")


DELEGATE.on_complete.append(_announce_finished_task)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class ElfieAgent(ElfieTools, Agent):
    """
    Elfie's runtime state for a single session.

    Inherits conversation tools from ElfieTools — add new tools there.
    Session lifecycle: on_enter → conversation → on_exit.
    """

    async def tts_node(self, text, model_settings):
        """Scrub leaked tool syntax from speech before it's synthesized."""
        return Agent.default.tts_node(self, _scrub_tool_leaks(text), model_settings)

    def __init__(self) -> None:
        # Build memory first so context is baked into the system prompt
        self._memory = ElfieMemory() if CONFIG.memory.enabled else None
        memory_context = self._memory.build_context_block() if self._memory else ""

        # Learned skills load fresh each session — a skill learned five
        # minutes ago is live on the next connect, no restart needed.
        from elfie.skills import load_skills
        super().__init__(
            instructions=build_system_prompt(CONFIG.owner_name, memory_context),
            tools=load_skills(),
        )
        self._start_time = time.monotonic()
        self._last_activity = time.monotonic()
        self._silence_retries = 0
        self._ending = False
        self._exit_reason = "completed"
        self._metrics = SessionMetrics()
        self._monitor_task: asyncio.Task | None = None
        self._hard_stop_task: asyncio.Task | None = None
        self._last_user_text: str = ""   # stash user turn until we have the reply
        self._transcript: list[dict] = []   # full exchange log for the Activity tab
        self._finalized = False          # guard so the session logs exactly once

    # --- lifecycle ---

    async def on_enter(self) -> None:
        logger.info("[Session] Started")
        self._hard_stop_task = asyncio.ensure_future(self._hard_stop())

        # Greet first — start silence monitor only after greeting so the
        # user gets a full window to respond, not a race against the timer.
        await self.session.say(build_greeting(CONFIG.owner_name), allow_interruptions=True)
        self._last_activity = time.monotonic()
        self._monitor_task = asyncio.ensure_future(self._silence_monitor())

    async def on_exit(self) -> None:
        await self.finalize()

    async def finalize(self) -> None:
        """
        Log the session + persist memory. Idempotent — runs exactly once,
        whether triggered by on_exit (clean drain) or the participant-left
        handler (user closed the tab). Either path that fires first wins; the
        earlier bug was relying only on on_exit, which didn't fire when the
        room dropped, so sessions never reached the Activity log.
        """
        if self._finalized:
            return
        self._finalized = True
        notify.clear_active()
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
        if self._hard_stop_task and not self._hard_stop_task.done():
            self._hard_stop_task.cancel()
        duration = time.monotonic() - self._start_time
        log_session("local", duration, self._exit_reason, self._metrics, self._transcript)
        if self._memory:
            await self._memory.close_session()

    # --- conversation hooks ---

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        """Reset silence timer and stash user text for memory recording."""
        self._last_activity = time.monotonic()
        self._silence_retries = 0
        # Brief natural pause before responding — less robotic
        await asyncio.sleep(0.8)

        text = (
            new_message.text_content
            if hasattr(new_message, "text_content")
            else str(new_message)
        )
        self._last_user_text = text
        logger.info(f"[STT] {text!r}")

    def on_agent_response(self, text: str) -> None:
        """Record the completed turn in memory and check for goodbye."""
        logger.info(f"[LLM] {text!r}")
        self._transcript.append({"user": self._last_user_text or None, "elfie": text})

        # Record user + assistant pair; trigger compression if needed
        if self._memory and self._last_user_text:
            should_compress = self._memory.record_turn(self._last_user_text, text)
            self._last_user_text = ""
            if should_compress:
                asyncio.ensure_future(self._memory.compress_old_turns())

        if not self._ending and any(p in text.lower() for p in GOODBYE_PHRASES):
            logger.info("[Session] Goodbye detected — scheduling end")
            self._ending = True
            asyncio.ensure_future(self._exit_natural())

    # --- exits ---

    async def _disconnect(self) -> None:
        """Clean up the session and room connection."""
        try:
            from livekit import api as lk_api
            lk_url = CONFIG.livekit_url.replace("wss://", "https://").replace("ws://", "http://")
            async with lk_api.LiveKitAPI(
                url=lk_url,
                api_key=CONFIG.livekit_api_key,
                api_secret=CONFIG.livekit_api_secret,
            ) as lk:
                for p in list(self._room.remote_participants.values()):
                    try:
                        await lk.room.remove_participant(
                            lk_api.RoomParticipantIdentity(
                                room=self._room.name, identity=p.identity
                            )
                        )
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"[Session] Cleanup warning: {e}")
        await self.finalize()   # idempotent — ensure the session is logged
        await self.session.aclose()
        await self._room.disconnect()

    async def _exit_natural(self) -> None:
        self._exit_reason = "completed"
        await asyncio.sleep(3.0)   # let TTS finish the goodbye
        await self._disconnect()

    async def _exit_silence(self) -> None:
        if self._ending:
            return
        self._ending = True
        self._exit_reason = "silence"
        await self.session.say("Okay, I'll be here when you need me.", allow_interruptions=False)
        await asyncio.sleep(1.5)
        await self._disconnect()

    async def _hard_stop(self) -> None:
        """Absolute session limit — safety net."""
        await asyncio.sleep(CONFIG.session.hard_stop_minutes * 60)
        if self._ending:
            return
        logger.warning("[Session] Hard stop reached")
        self._ending = True
        self._exit_reason = "timeout"
        await self.session.say("I need to wrap up — talk to you later!", allow_interruptions=False)
        await asyncio.sleep(2.0)
        await self._disconnect()

    # --- silence monitor ---

    async def _silence_monitor(self) -> None:
        """
        Polls every 2 seconds. Prompts after SILENCE_TIMEOUT_SEC of quiet,
        then ends the session after MAX_SILENCE_RETRIES unanswered prompts.

        Never fires while Elfie is speaking — waits until she's back in
        listening state before starting the clock.
        """
        cfg = CONFIG.session
        was_not_listening = False

        while not self._ending:
            await asyncio.sleep(2.0)

            currently_listening = "listening" in str(self.session.agent_state).lower()
            if not currently_listening:
                was_not_listening = True
                continue

            # Just transitioned back to listening — reset clock
            if was_not_listening:
                was_not_listening = False
                self._last_activity = time.monotonic()
                continue

            elapsed = time.monotonic() - self._last_activity
            if elapsed > cfg.silence_timeout_sec:
                if self._silence_retries < cfg.max_silence_retries:
                    self._silence_retries += 1
                    await self.session.say("Still there?", allow_interruptions=True)
                    self._last_activity = time.monotonic()
                elif cfg.end_on_silence:
                    await self._exit_silence()
                    break
                else:
                    # Always-on mode: go quiet but stay connected and listening.
                    # Retries reset on the next user turn, re-arming the prompts.
                    self._last_activity = time.monotonic()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

async def entrypoint(ctx: JobContext) -> None:
    """Called by LiveKit when a new room job is dispatched."""
    from elfie.config import apply_runtime_overrides
    apply_runtime_overrides()   # pick up dashboard settings changes per session
    logger.info(f"[Worker] Room: {ctx.room.name}")
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    try:
        await ctx.wait_for_participant()
    except RuntimeError:
        # User left before the session started (e.g. quick page reload) — benign
        logger.info("[Worker] Room closed before anyone joined")
        return

    cfg = CONFIG
    session = AgentSession(
        vad=silero.VAD.load(),
        stt=deepgram.STT(
            model=cfg.voice.stt_model,
            language=cfg.voice.stt_language,
            api_key=cfg.deepgram_api_key,
        ),
        # Primary 70B with automatic fallback to 8B — Groq limits are per
        # model, so exhausting the 70B daily tokens no longer mutes Elfie.
        llm=llm_lib.FallbackAdapter([
            groq_plugin.LLM(model=cfg.voice.llm_model, api_key=cfg.groq_api_key),
            groq_plugin.LLM(model=cfg.voice.llm_fallback_model, api_key=cfg.groq_api_key),
        ]),
        tts=cartesia.TTS(
            model=cfg.voice.tts_model,
            voice=cfg.voice.tts_voice,
            speed=cfg.voice.tts_speed,
            api_key=cfg.cartesia_api_key,
        ),
        # Barge-in settings — tuned for natural conversation
        allow_interruptions=cfg.voice.allow_interruptions,
        min_interruption_duration=cfg.voice.min_interruption_duration,
        min_interruption_words=cfg.voice.min_interruption_words,
        min_endpointing_delay=cfg.voice.min_endpointing_delay,
    )

    agent = ElfieAgent()
    agent._room = ctx.room   # needed for disconnect cleanup

    # Wire response text → goodbye detection
    @session.on("conversation_item_added")
    def _on_item(ev):
        if hasattr(ev.item, "role") and ev.item.role == "assistant":
            text = getattr(ev.item, "text_content", "") or ""
            if text:
                agent.on_agent_response(text)

    # Reset silence timer when Elfie transitions back to listening
    @session.on("agent_state_changed")
    def _on_state(ev):
        if "listening" in str(ev.new_state).lower():
            agent._last_activity = time.monotonic()

    # Debug visibility: did VAD hear speech, and what did STT make of it?
    @session.on("user_state_changed")
    def _on_user_state(ev):
        logger.info(f"[User] state → {ev.new_state}")

    @session.on("user_input_transcribed")
    def _on_transcribed(ev):
        logger.info(f"[STT-interim] {ev.transcript!r} (final={ev.is_final})")

    # Collect latency + token metrics for cost tracking
    @session.on("metrics_collected")
    def _on_metrics(ev):
        m = ev.metrics
        parts = []
        if hasattr(m, "stt_duration") and m.stt_duration:
            agent._metrics.stt_minutes += m.stt_duration / 60
            parts.append(f"STT={m.stt_duration*1000:.0f}ms")
        if hasattr(m, "llm_ttft") and m.llm_ttft:
            parts.append(f"LLM={m.llm_ttft*1000:.0f}ms")
        if hasattr(m, "tts_ttfb") and m.tts_ttfb:
            parts.append(f"TTS={m.tts_ttfb*1000:.0f}ms")
        if hasattr(m, "total_tokens"):
            agent._metrics.llm_tokens += getattr(m, "total_tokens", 0)
        if parts:
            logger.info(f"[Latency] {' | '.join(parts)}")
        metrics.log_metrics(m)

    # End the job when the user leaves — each /talk connection gets a fresh
    # room, so a lingering agent in an empty room would never be used again.
    @ctx.room.on("participant_disconnected")
    def _on_leave(participant):
        if not ctx.room.remote_participants and not agent._ending:
            logger.info("[Session] User left — shutting down")
            agent._ending = True

            async def _close():
                # Log + persist BEFORE shutdown so the record always lands,
                # even though on_exit may not fire on a dropped room.
                await agent.finalize()
                await session.aclose()
                ctx.shutdown(reason="user left")

            asyncio.ensure_future(_close())

    await session.start(agent, room=ctx.room)

    # Register this session as the live delivery target for finished
    # background tasks (voice announcement + room chat).
    notify.set_active(notify.Notifier(room=ctx.room, session=session))

    logger.info("[Worker] Elfie ready")


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
