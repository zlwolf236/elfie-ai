# Training the exact "Hey Elfie" wake word

Vosk can't recognize made-up names, and the openWakeWord trainer won't run on
this machine (it needs Python 3.10 + GPU + several GB of data). So the one
reliable way to get the *exact* "Hey Elfie" is a free Google Colab — ~1 hour,
mostly automated waiting. Everything on Elfie's side is already wired to use the
result the moment you drop it in.

## Steps (do these once)

1. Open the official openWakeWord training notebook (Google account needed):
   https://colab.research.google.com/drive/1q1oe2zOyZp7UsB3jJiQ1IFn8z5YfjwEb

2. In Colab: **Runtime → Change runtime type → T4 GPU** (free), then
   **Runtime → Run all**.

3. When it asks for the target word/phrase, enter:
       hey elfie
   Tip: if early results sound off, also try the phonetic spelling **"hey elfee"**
   or **"hey el fee"** — the community finds phonetic spellings train better.

4. Let it run (it synthesizes thousands of voices saying it, then trains).
   When done it produces a file ending in **.onnx** (a few hundred KB).
   Download it.

5. Rename it to exactly **hey_elfie.onnx** and put it in this folder:
       elfie-ai/models/hey_elfie.onnx

6. In the Elfie dashboard: **Settings → Wake word → "Hey Elfie"**, Save.
   Then restart the listener (or reboot):
       systemctl --user restart elfie-wake

That's it. Elfie auto-detects `models/hey_elfie.onnx` and switches from Vosk to
the trained model — now "Hey Elfie" works exactly, with the best accuracy.

## If you'd rather not train

- **"Hey Ellie"** — sounds nearly identical, works now, zero training (Vosk).
- **"Hey Jarvis"** — stock model, highest accuracy, zero setup.
Pick either in Settings → Wake word anytime.
