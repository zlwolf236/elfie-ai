r"""
Elfie wake-word listener — runs on the WINDOWS side (WSL2 has no microphone).

Listens on the default mic with openWakeWord. On detection it opens the
Elfie page; the page auto-connects (after you've connected manually once),
so the full flow is: say the wake word -> browser opens -> Elfie greets you.

Setup (once, in Windows PowerShell or cmd):
    cd \\wsl.localhost\<distro>\mnt\c\Users\zilan\tools\elfie-ai\windows
      (or just the repo folder in Explorer: C:\Users\zilan\tools\elfie-ai\windows)
    python -m pip install openwakeword sounddevice numpy
    python wake_listener.py

Wake word: "hey jarvis" (stock model) until you train a custom one.
To switch to "hey Elfie": train hey_elfie.onnx with the openWakeWord Colab
(see the wake-word research report in the dashboard), drop the file next to
this script, and restart the listener — it's picked up automatically.

To start it with Windows: Win+R -> shell:startup -> add a shortcut to:
    pythonw.exe C:\Users\zilan\tools\elfie-ai\windows\wake_listener.py
"""
import time
import webbrowser
from pathlib import Path

import numpy as np
import sounddevice as sd

ELFIE_URL = "http://localhost:8765"
SAMPLE_RATE = 16000
CHUNK = 1280              # 80 ms — what openWakeWord expects

import json
SETTINGS_FILE = Path(__file__).parent.parent / "data" / "settings.json"
CUSTOM_MODEL = Path(__file__).parent / "hey_elfie.onnx"


def load_settings() -> dict:
    """Read wake-word settings saved from the dashboard (Settings tab).
    Falls back to sensible defaults. Re-read at startup — restart to apply."""
    defaults = {"wake_word": "hey_jarvis", "threshold": 0.5, "cooldown_sec": 10}
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        for k in defaults:
            if k in saved:
                defaults[k] = saved[k]
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return defaults


def main() -> None:
    from openwakeword.model import Model
    from openwakeword.utils import download_models

    cfg = load_settings()
    threshold = float(cfg["threshold"])
    cooldown = float(cfg["cooldown_sec"])
    wanted = cfg["wake_word"]

    # "hey_elfie" -> custom onnx if present; otherwise a stock model name.
    if wanted == "hey_elfie" and CUSTOM_MODEL.exists():
        model = Model(wakeword_models=[str(CUSTOM_MODEL)])
        wake_name = "hey_elfie"
    elif wanted == "hey_elfie":
        download_models(["hey_jarvis"])
        model = Model(wakeword_models=["hey_jarvis"])
        wake_name = "hey_jarvis"
        print('No hey_elfie.onnx yet — falling back to "hey jarvis". Train the '
              'model and drop hey_elfie.onnx in this folder, then restart.')
    else:
        download_models([wanted])         # no-op when already cached
        model = Model(wakeword_models=[wanted])
        wake_name = wanted

    last_trigger = 0.0
    print(f"Listening for '{wake_name.replace('_', ' ')}' "
          f"(threshold {threshold}, cooldown {cooldown}s)... Ctrl-C to stop")

    def callback(indata, frames, t, status):
        nonlocal last_trigger
        if status:
            return
        scores = model.predict(np.frombuffer(indata, dtype=np.int16))
        score = max(scores.values())
        if score > threshold and time.time() - last_trigger > cooldown:
            last_trigger = time.time()
            print(f"Wake word detected (score {score:.2f}) — waking Elfie")
            # Tell the open Elfie page to connect (it polls /api/wake). Also
            # open the page if it isn't up yet, so the first wake still works.
            try:
                import urllib.request
                urllib.request.urlopen(ELFIE_URL + "/api/wake", data=b"{}", timeout=3)
            except Exception:
                webbrowser.open(ELFIE_URL)
            model.reset()

    with sd.RawInputStream(
        samplerate=SAMPLE_RATE, blocksize=CHUNK, dtype="int16",
        channels=1, callback=callback,
    ):
        while True:
            time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
