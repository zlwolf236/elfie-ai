"""
Session logging and cost estimation.

Sessions are written to data/sessions.jsonl — one JSON line per session.
Cost is estimated post-hoc from token/audio metrics collected by LiveKit.
"""
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("elfie.session")

SESSIONS_FILE = Path(__file__).parent.parent / "data" / "sessions.jsonl"

# Approximate cost per unit (USD) — update if pricing changes
COST_PER_STT_MINUTE  = 0.0058   # Deepgram Nova-3 streaming, pay-as-you-go (~$0.35/hr)
COST_PER_LLM_1K_TOK  = 0.0006   # Groq Llama-3.3-70B blended (in+out avg)
COST_PER_TTS_1K_CHAR = 0.015    # Cartesia Sonic-2


@dataclass
class SessionMetrics:
    stt_minutes: float  = 0.0
    llm_tokens: int     = 0
    tts_chars: int      = 0

    def estimated_cost_usd(self) -> float:
        return round(
            self.stt_minutes  * COST_PER_STT_MINUTE
            + (self.llm_tokens / 1000) * COST_PER_LLM_1K_TOK
            + (self.tts_chars  / 1000) * COST_PER_TTS_1K_CHAR,
            4,
        )


def log_session(
    user_id: str,
    duration_sec: float,
    exit_reason: str,
    metrics: SessionMetrics | None = None,
    transcript: list[dict] | None = None,
) -> None:
    record = {
        "timestamp":    datetime.now(timezone.utc).isoformat(),
        "user_id":      user_id,
        "duration_sec": round(duration_sec, 1),
        "exit_reason":  exit_reason,
    }
    if transcript:
        record["turns"] = transcript[:100]   # conversation log for the Activity tab
    if metrics:
        record["cost_usd"] = metrics.estimated_cost_usd()
        record["stt_min"]  = round(metrics.stt_minutes, 3)
        record["llm_tok"]  = metrics.llm_tokens
        record["tts_chr"]  = metrics.tts_chars

    try:
        SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with SESSIONS_FILE.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        logger.error(f"Failed to write session log: {e}")

    logger.info(
        f"[Session] {exit_reason} | {duration_sec:.0f}s"
        + (f" | ~${metrics.estimated_cost_usd():.4f}" if metrics else "")
    )
