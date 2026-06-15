"""
Wake-word listener that runs IN WSL (no Windows step needed).

WSLg bridges the Windows microphone into WSL via PulseAudio's RDPSource, so
we capture with `parec` (robust — avoids PortAudio device-enumeration issues)
and run openWakeWord locally. On detection we POST /api/wake; the open Elfie
page polls that and connects. 100% on-device until the wake word — nothing
leaves the machine, no cost.

Reads wake_word / threshold / cooldown_sec from data/settings.json (set them
in the dashboard Settings tab). Runs as the elfie-wake systemd service.

Manual run:  PULSE_SERVER=unix:/mnt/wslg/PulseServer .venv/bin/python -m elfie.wake_service
"""
import json
import logging
import subprocess
import time
import urllib.request
from pathlib import Path

import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
logger = logging.getLogger("elfie.wake")

_ROOT = Path(__file__).parent.parent
SETTINGS_FILE = _ROOT / "data" / "settings.json"
CUSTOM_ONNX = _ROOT / "models" / "hey_elfie.onnx"
VOSK_MODEL = _ROOT / "models" / "vosk-small"
DASHBOARD = "http://localhost:8765"

SAMPLE_RATE = 16000
CHUNK_SAMPLES = 1280          # 80 ms — openWakeWord's frame size
CHUNK_BYTES = CHUNK_SAMPLES * 2

# Stock openWakeWord phrases (pre-trained, no setup). Anything else — like
# "hey_elfie" — is handled by Vosk keyword spotting, which needs NO training:
# you just give it the words as text.
STOCK_PHRASES = {"hey_jarvis", "alexa", "hey_mycroft"}


def load_settings() -> dict:
    defaults = {"wake_word": "hey_jarvis", "wake_phrase": "hey elfie",
                "threshold": 0.5, "cooldown_sec": 10}
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        for k in defaults:
            if k in saved:
                defaults[k] = saved[k]
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return defaults


def fire_wake() -> None:
    try:
        urllib.request.urlopen(DASHBOARD + "/api/wake", data=b"{}", timeout=3)
        logger.info("Wake event sent to dashboard")
    except Exception as e:
        logger.warning(f"Couldn't reach dashboard: {e}")


def _open_mic() -> subprocess.Popen:
    """Capture the WSLg-bridged Windows mic through PulseAudio."""
    return subprocess.Popen(
        ["parec", "--device=RDPSource", "--format=s16le",
         f"--rate={SAMPLE_RATE}", "--channels=1"],
        stdout=subprocess.PIPE,
    )


def run_openwakeword(cfg: dict, debug: bool) -> None:
    """Stock phrases (hey_jarvis / alexa / hey_mycroft) — pre-trained models."""
    import openwakeword
    from openwakeword.model import Model

    threshold, cooldown = float(cfg["threshold"]), float(cfg["cooldown_sec"])
    word = cfg["wake_word"]
    if word == "hey_elfie" and CUSTOM_ONNX.exists():
        path = str(CUSTOM_ONNX)
    else:
        paths = openwakeword.get_pretrained_model_paths()
        path = next((p for p in paths if word.replace("hey_", "").split("_")[0] in p.lower()),
                    next(p for p in paths if "jarvis" in p.lower()))
    model = Model(wakeword_model_paths=[path])
    logger.info(f"Listening for '{Path(path).stem}' (openWakeWord, threshold "
                f"{threshold}, cooldown {cooldown}s) via WSLg mic")

    proc = _open_mic()
    last_trigger = window_peak = 0.0
    last_report = time.time()
    try:
        while True:
            buf = proc.stdout.read(CHUNK_BYTES)
            if not buf or len(buf) < CHUNK_BYTES:
                continue
            audio = np.frombuffer(buf, dtype=np.int16)
            score = max(model.predict(audio).values())
            window_peak = max(window_peak, score)
            if debug and time.time() - last_report > 3:
                last_report = time.time()
                logger.info(f"[debug] mic amp {int(np.abs(audio).max())}, "
                            f"best score 3s: {window_peak:.3f} (need >{threshold})")
                window_peak = 0.0
            if score > threshold and time.time() - last_trigger > cooldown:
                last_trigger = time.time()
                logger.info(f"Wake word detected (score {score:.2f})")
                fire_wake()
                model.reset()
    finally:
        proc.terminate()


# Real-word phrases that SOUND like made-up names, so Vosk can catch them with
# no training. You say the real name; Vosk matches the closest entry and fires.
# (Vosk only knows dictionary words, so "elfie" itself is unspellable — these
# homophones stand in for it acoustically.)
HOMOPHONES = {
    "hey elfie": ["hey elf e", "hey elf he", "hey elsie", "hey ellie",
                  "hey alfie", "hey elf", "a elf e", "hey health e"],
}


def run_vosk(cfg: dict, debug: bool) -> None:
    """
    Custom phrase via local Vosk — NO training, NO onnx. We restrict Vosk to a
    small grammar (the phrase, or a set of real-word homophones for a made-up
    name) and fire when any of them is heard. Grammar mode is far more reliable
    than free recognition on the small model.
    """
    from vosk import KaldiRecognizer, Model, SetLogLevel
    SetLogLevel(-1)

    cooldown = float(cfg["cooldown_sec"])
    phrase = (cfg.get("wake_phrase") or "hey elfie").lower().strip()
    if not VOSK_MODEL.exists():
        logger.error(f"Vosk model missing at {VOSK_MODEL} — can't do custom wake word.")
        return

    # Build the phrase set: homophones for a known made-up name, else the phrase.
    phrases = HOMOPHONES.get(phrase, [phrase])
    targets = set(phrases)
    model = Model(str(VOSK_MODEL))
    rec = KaldiRecognizer(model, SAMPLE_RATE, json.dumps(phrases + ["[unk]"]))
    logger.info(f"Listening for '{phrase}' via {len(phrases)} sound-alike(s) "
                f"(Vosk, no training) — WSLg mic")

    proc = _open_mic()
    last_trigger = 0.0
    try:
        while True:
            buf = proc.stdout.read(CHUNK_BYTES)
            if not buf or len(buf) < CHUNK_BYTES:
                continue
            if rec.AcceptWaveform(bytes(buf)):
                heard = json.loads(rec.Result()).get("text", "")
                if debug and heard:
                    logger.info(f"[debug] vosk heard: {heard!r}")
                # Fire if any sound-alike appears in what was heard (the user may
                # say it once, twice, or with filler — don't require exact match).
                hit = next((t for t in targets if t in heard), None)
                if hit and time.time() - last_trigger > cooldown:
                    last_trigger = time.time()
                    logger.info(f"Wake word detected ('{heard}' ≈ {phrase})")
                    fire_wake()
                    rec.Reset()
    finally:
        proc.terminate()


def main() -> None:
    import os
    cfg = load_settings()
    debug = bool(os.getenv("WAKE_DEBUG"))
    # Stock phrase -> openWakeWord; anything custom (hey_elfie) -> Vosk, no training.
    if cfg["wake_word"] in STOCK_PHRASES or (cfg["wake_word"] == "hey_elfie" and CUSTOM_ONNX.exists()):
        run_openwakeword(cfg, debug)
    else:
        run_vosk(cfg, debug)


if __name__ == "__main__":
    main()
