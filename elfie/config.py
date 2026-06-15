"""
Elfie configuration — all knobs in one place.

Copy .env.example → .env and fill in your API keys.
Everything else here has sensible defaults you can tune.
"""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")


@dataclass
class VoiceConfig:
    """How Elfie sounds and how responsive she is."""

    # STT — transcription
    stt_model: str = "nova-3"          # Deepgram model; nova-3 is fast + accurate
    stt_language: str = "en-US"        # set "multi" for multilingual

    # LLM — brain. Default is gpt-oss-120b: it calls tools reliably under
    # Elfie's long system prompt (4/4 in testing). llama-3.3-70b is "smarter"
    # but malforms tool calls ~half the time with a long prompt, which caused
    # the agent to hallucinate facts instead of looking them up — avoid it here.
    llm_model: str = "openai/gpt-oss-120b"
    # Fallback when the primary errors or hits its per-model rate limit.
    # Also a reliable tool-caller, faster + cheaper.
    llm_fallback_model: str = "meta-llama/llama-4-scout-17b-16e-instruct"

    # TTS — voice
    tts_model: str = "sonic-2"          # plain alias — dated versions get sunsetted
    tts_voice: str = "694f9389-aac1-45b6-b726-9d9369183238"  # Sarah — warm, natural
    tts_speed: str = "fast"            # "slow" | "normal" | "fast"

    # Barge-in — how quickly Elfie yields when you start talking mid-response.
    # Lower values = more responsive but may cut off on background noise.
    allow_interruptions: bool = True
    min_interruption_duration: float = 0.5   # seconds of speech before interrupt fires
    min_interruption_words: int = 0          # 0 = any sound triggers; raise to 1-2 if noisy env
    # Patience: how long a pause means "I'm done talking". Too low and slow,
    # thinking speech gets chopped into fragments the LLM answers separately.
    min_endpointing_delay: float = 1.0       # silence (sec) before "user finished speaking"


@dataclass
class SessionConfig:
    """Behaviour during a conversation."""

    silence_timeout_sec: float = 30.0   # seconds of silence before "are you still there?"
    max_silence_retries: int = 1        # "still there?" prompts before going quiet
    # Always-on (Jarvis) mode: after the prompts, stay connected and keep
    # listening silently. Set True for call-style sessions that should hang up.
    end_on_silence: bool = False
    hard_stop_minutes: int = 480        # absolute session limit — safety net


@dataclass
class MemoryConfig:
    """Persistent memory settings."""

    enabled: bool = True
    compress_every_n_turns: int = 10    # compress old turns every N exchanges
    recent_sessions_to_inject: int = 3  # past session summaries shown at start
    memory_model: str = "llama-3.1-8b-instant"  # cheap model for memory ops
    # Hermes-style long-term memory: one MEMORY.md of dense facts, always
    # injected in full, rewritten (not appended) at session end so
    # contradictions resolve. The cap keeps the prompt cost bounded.
    max_memory_chars: int = 4000


@dataclass
class WakeWordConfig:
    """Wake word detection (optional — requires: pip install openwakeword sounddevice numpy)."""

    enabled: bool = bool(os.getenv("WAKE_WORD_ENABLED", ""))
    # The model the Windows listener loads. "hey_jarvis"/"alexa"/"hey_mycroft"
    # are stock; "hey_elfie" uses models/hey_elfie.onnx once you train it.
    wake_word: str = "hey_jarvis"
    # Spoken phrase for the no-training (Vosk) path — must be REAL dictionary
    # words (Vosk can't hear made-up names). "hey ellie" ~ "hey elfie".
    wake_phrase: str = "hey ellie"
    threshold: float = 0.5    # detection sensitivity (0.0–1.0); lower = more sensitive
    cooldown_sec: int = 10    # ignore re-triggers for this long after one fires


@dataclass
class DelegateConfig:
    """Background task delegation (Claude Code headless + report writer)."""

    enabled: bool = True
    claude_path: str = os.getenv("CLAUDE_PATH", "claude")   # binary name or full path
    # Permission mode for headless Claude Code runs. "acceptEdits" lets it
    # write files in its workspace; use "bypassPermissions" only if you trust
    # every task you'll delegate.
    permission_mode: str = os.getenv("CLAUDE_PERMISSION_MODE", "acceptEdits")
    timeout_minutes: int = 20           # kill a delegated task after this long


@dataclass
class DashboardConfig:
    """Local web dashboard for reports and tasks (python -m elfie.dashboard)."""

    host: str = "127.0.0.1"             # localhost only — not exposed to the network
    port: int = int(os.getenv("DASHBOARD_PORT", "8765"))
    # Pop finished reports straight into the default browser (via explorer.exe
    # on WSL) so Elfie literally puts them in front of you.
    auto_open_reports: bool = True


@dataclass
class ElfieConfig:
    # Who this is for — shown in logs and used in the greeting
    owner_name: str = os.getenv("OWNER_NAME", "")

    voice: VoiceConfig = None
    session: SessionConfig = None
    memory: MemoryConfig = None
    wakeword: WakeWordConfig = None
    delegate: DelegateConfig = None
    dashboard: DashboardConfig = None

    # LiveKit (room / WebRTC layer)
    livekit_url: str        = os.getenv("LIVEKIT_URL", "")
    livekit_api_key: str    = os.getenv("LIVEKIT_API_KEY", "")
    livekit_api_secret: str = os.getenv("LIVEKIT_API_SECRET", "")

    # API keys
    deepgram_api_key: str   = os.getenv("DEEPGRAM_API_KEY", "")
    groq_api_key: str       = os.getenv("GROQ_API_KEY", "")
    cartesia_api_key: str   = os.getenv("CARTESIA_API_KEY", "")

    # Telegram report delivery (optional — leave empty to disable)
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str   = os.getenv("TELEGRAM_CHAT_ID", "")

    # Gmail via IMAP app password (optional — myaccount.google.com/apppasswords)
    gmail_address: str      = os.getenv("GMAIL_ADDRESS", "")
    gmail_app_password: str = os.getenv("GMAIL_APP_PASSWORD", "")

    def __post_init__(self):
        if self.voice is None:
            self.voice = VoiceConfig()
        if self.session is None:
            self.session = SessionConfig()
        if self.memory is None:
            self.memory = MemoryConfig()
        if self.wakeword is None:
            self.wakeword = WakeWordConfig()
        if self.delegate is None:
            self.delegate = DelegateConfig()
        if self.dashboard is None:
            self.dashboard = DashboardConfig()


# Single shared instance — import this everywhere
CONFIG = ElfieConfig()


# ── Runtime settings (editable from the dashboard) ───────────────────────────

RUNTIME_SETTINGS_FILE = Path(__file__).parent.parent / "data" / "settings.json"

# Whitelist of dashboard-editable knobs: name -> (section, caster)
RUNTIME_TUNABLES = {
    "llm_model":             ("voice",     str),
    "tts_voice":             ("voice",     str),
    "tts_speed":             ("voice",     str),
    "min_endpointing_delay": ("voice",     float),
    "silence_timeout_sec":   ("session",   float),
    "max_silence_retries":   ("session",   int),
    "end_on_silence":        ("session",   bool),
    "auto_open_reports":     ("dashboard", bool),
    "wake_word":             ("wakeword",  str),
    "wake_phrase":           ("wakeword",  str),     # spoken phrase for hey_elfie (Vosk)
    "threshold":             ("wakeword",  float),   # wake-word sensitivity
    "cooldown_sec":          ("wakeword",  int),
}


def apply_runtime_overrides() -> None:
    """
    Overlay data/settings.json onto CONFIG. The agent calls this at the start
    of every session, so dashboard changes apply on the next connect without
    a restart.
    """
    import json
    try:
        overrides = json.loads(RUNTIME_SETTINGS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return
    for key, value in overrides.items():
        spec = RUNTIME_TUNABLES.get(key)
        if spec is None:
            continue
        section, caster = spec
        try:
            setattr(getattr(CONFIG, section), key, caster(value))
        except (TypeError, ValueError):
            pass


def current_runtime_settings() -> dict:
    """Effective values of the dashboard-editable knobs."""
    apply_runtime_overrides()
    return {
        key: getattr(getattr(CONFIG, section), key)
        for key, (section, _) in RUNTIME_TUNABLES.items()
    }
