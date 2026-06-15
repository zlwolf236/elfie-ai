# Elfie AI

**An always-listening personal voice assistant that's cheap to keep running —
because it only spends money when you actually talk to it.**

Talk to her like she's in the room. She answers in one or two plain sentences,
puts anything long into a written report, and hands real work (coding, research,
producing files) to background agents — Jarvis at the front desk, Claude Code in
the back room.

```
You:    "Hey Ellie… what's the weather in Miami, and show me a page?"
Elfie:  "It's 88 and humid in Miami, from Open-Meteo — opening a forecast for you."
        (a weather page pops up on your screen)
```

---

## Why this exists

Everyone wants an assistant that's *always there* — you just speak and it
responds. The problem is cost. A naive "always-on" agent streams your microphone
to the cloud 24/7 and runs a large model continuously; that's dollars per hour,
every hour, mostly to listen to silence.

Elfie is built around avoiding that waste:

- **A microphone is free; the cloud is not.** So a local voice-activity detector
  (Silero) and an on-device wake word do the constant listening for free, and the
  paid services (speech-to-text, the LLM, text-to-speech) only fire once you're
  actually talking.
- **Wake-word mode = $0 when idle.** Nothing is connected or streamed until you
  say her name. The wake word runs 100% on your machine — your voice never leaves
  the device until she's woken.
- **Voice is for talking, not for working.** The conversation stays fast and
  cheap on a small-but-quick model. Anything heavy — write this code, research
  that topic, produce a document — gets **delegated** to Claude Code (or, later,
  a Hermes analyst) running in the background, and she tells you when it's done.

The result is a "Jarvis-style" experience — converse naturally, delegate the hard
stuff — that costs roughly **nothing while idle** and **cents per conversation**.

---

## How it works

```
   Mic (always on, local & free)
     │
     ├─ Wake word  (openWakeWord / Vosk — on-device, free)   ── wake-word mode
     │     └─ only AFTER your wake word does anything connect
     │
     └─ Silero VAD (local — detects speech)
           → Deepgram STT      (cloud — transcribes)
             → Groq LLM         (cloud — thinks, calls tools)
               → Cartesia TTS   (cloud — speaks)
                 → Speaker
                       │
                       ├─ tools: web search, weather, email, open a webpage…
                       ├─ reports: long answers → dashboard (+ optional Telegram)
                       └─ delegation: real work → Claude Code → report when ready
```

LiveKit handles the audio transport (run locally in Docker — no cloud account
needed). The brain (Groq), ears (Deepgram), and voice (Cartesia) are the only
paid pieces, and only while you're in a conversation.

### Two ways to run her (Settings → Mode)

| | **Always-on** | **Wake word** |
|---|---|---|
| Start talking | just talk — stays connected | say "Hey Ellie" / "Hey Jarvis" |
| Idle cost | ~35¢/hour (streams to Deepgram) | **$0** — nothing connects until woken |
| Privacy when idle | mic streamed to cloud | voice stays on-device until the wake word |
| Best for | a session at your desk | true all-day ambient assistant |

---

## What it can do

Elfie's abilities are **tools** — and she can **learn new ones by voice**.

| Tool | What it does |
|------|--------------|
| `brave_search` | Web search with real sources (news, prices, traffic, facts) |
| `get_weather` | Current conditions + forecast (Open-Meteo, no key) |
| `open_website` | Opens any site/URL in your browser |
| `check_email` | Reads unread Gmail (IMAP app password) |
| `write_report` | Turns a long/technical answer into a dashboard report |
| `delegate_task` | Hands real work to Claude Code in the background |
| `continue_task` | Resumes a finished task with full context |
| `learn_skill` | **Researches + writes a brand-new tool for herself** |
| `show_report` / `check_tasks` | Open the latest report / list background jobs |
| notes, reminders, time | the basics |

Plus, under the hood:

- **Persistent memory** — a compact `MEMORY.md` she rewrites after each session,
  so she remembers you across days (and resolves contradictions, e.g. a corrected
  name) without the prompt ballooning.
- **Reports dashboard** — `localhost:8765`: talk to her, read reports, watch
  background tasks, see per-session cost, and change settings live.
- **Self-extension** — ask for an ability she lacks; a background agent researches
  an existing API, writes the tool, it passes a safety gate (no `subprocess`,
  `eval`, file deletion…), gets git-committed, and is live next time you connect.

---

## What you need

**1. API keys** (free tiers are plenty to start):

| Service | Role | Free tier | Link |
|---------|------|-----------|------|
| Deepgram | speech-to-text (ears) | $200 credit | https://console.deepgram.com |
| Groq | LLM (brain) | free tier | https://console.groq.com/keys |
| Cartesia | text-to-speech (voice) | trial credits | https://play.cartesia.ai/console |
| Brave (optional) | better web search | 2k queries/mo | https://api.search.brave.com |
| Gmail (optional) | reading email | app password | https://myaccount.google.com/apppasswords |

LiveKit needs **no account** — it runs locally in Docker.

**2. A machine** — Linux, macOS, or **Windows via WSL2** (what this repo is tuned
for; the wake word uses the WSLg microphone bridge). Docker, Python 3.11+.

---

## Setup

```bash
git clone https://github.com/zlwolf236/elfie-ai && cd elfie-ai
cp .env.example .env       # paste your keys, set OWNER_NAME

# Start everything as background services (LiveKit + agent + dashboard + wake):
bash deploy/install_services.sh
```

That script runs a local LiveKit container, then installs systemd user services
that **auto-start and auto-restart** (`elfie-agent`, `elfie-dashboard`, and
`elfie-wake` if a microphone bridge is present). Then open:

```
http://localhost:8765        # talk to Elfie + reports + settings
```

Click **Connect** and talk, or switch to **Wake word** mode in Settings and just
say the word.

**Manual / dev run instead of services:**

```bash
./start_elfie.sh            # dashboard + agent in the foreground
```

### Wake word

- **"Hey Jarvis"** (or Alexa / Hey Mycroft) — stock models, fastest, zero setup.
- **A custom phrase with no training** — pick "Custom phrase" in Settings and type
  real words, e.g. **"hey ellie"** (sounds like Elfie). Uses local Vosk; works
  immediately. *Note: Vosk only knows real dictionary words — made-up names won't
  register, which is why "ellie" stands in for "elfie".*
- **The exact "Hey Elfie"** — needs a trained model (`models/hey_elfie.onnx`) via
  the free ~1-hour openWakeWord Colab; drop the file in `models/`, select it, done.

On WSL2 the wake listener runs in WSL using the WSLg mic bridge (no Windows
process). On a machine where that bridge isn't available, run
`windows/wake_listener.py` on the host instead (see `windows/START_HERE.txt`).

---

## Examples — what it actually feels like

**Quick facts (grounded, with a source):**
> **You:** "How's the traffic to New York City right now from Jersey City?"
> **Elfie:** *(calls `brave_search`)* "Brave Search says the Holland Tunnel is
> backed up about 20 minutes; the Lincoln's lighter. Want me to open the live map?"

**Weather + show me a page:**
> **You:** "What's the weather right now in Miami? And show me on a webpage."
> **Elfie:** *(calls `get_weather`, then `open_website`)* "It's 88 and humid in
> Miami per Open-Meteo — opening a forecast page now." *(browser opens)*

**Delegate real work, get pinged when ready:**
> **You:** "Do some research about ABC Corp and keep me posted once it's available."
> **Elfie:** *(calls `delegate_task`)* "On it — my research agent is digging in.
> I'll let you know when the report's ready."
> *…minutes later…* "Your report on ABC Corp is ready — it's on your dashboard."
> *(opens as a formatted report with sources; any files it produced are linked)*

**Teach her something new (self-extension):**
> **You:** "I wish you could check crypto prices."
> **Elfie:** *(calls `learn_skill`)* "I'll learn that — give me a few minutes."
> *…later…* "Done — I can check crypto prices now." *(new tool, safety-checked
> and committed; live on your next connect)*

---

## Configuration

Everything lives in `elfie/config.py`, and the common knobs are editable live in
the dashboard **Settings** tab (applied on the next connect):

- **Mode** — always-on vs wake word
- **Brain** — which Groq model (default `gpt-oss-120b`: smart *and* reliable at
  tool-calling; `llama-3.3-70b` is smarter prose but flakier with tools)
- **Voice** — any Cartesia voice + speed
- **Wake word** — phrase, sensitivity, cooldown
- **Conversation** — patience before replying, silence timeout
- **Reports** — auto-open finished reports in the browser

---

## Cost

Idle: **$0** in wake-word mode. In a conversation, the paid pieces are roughly:

| Component | Rate | ~50 interactions/day |
|-----------|------|----------------------|
| Deepgram STT | $0.0058/min | ~$0.10 |
| Groq LLM | ~$0.0006/1K tok | ~$0.10 |
| Cartesia TTS | $0.015/1K chars | ~$0.10 |
| **Total** | | **~$0.30/day** |

The Activity tab shows measured per-session usage and your running total.

---

## Project layout

```
elfie-ai/
├── elfie_agent.py        # entrypoint — the LiveKit voice agent
├── deploy/               # systemd services + installer (always-on)
├── elfie/
│   ├── config.py         # all settings (+ live runtime overrides)
│   ├── persona.py        # personality, voice rules, grounding rules
│   ├── tools.py          # built-in tools (add your own here)
│   ├── skills/           # learned/loadable skills (brave_search, weather, …)
│   ├── memory.py         # persistent MEMORY.md (rewrite, not append)
│   ├── delegate.py       # background tasks → Claude Code; skill builder
│   ├── reports.py        # report store
│   ├── notify.py         # delivery: voice + chat + Telegram + open browser
│   ├── dashboard.py      # localhost web UI (talk, reports, activity, settings)
│   ├── wake_service.py   # WSL wake-word listener (openWakeWord / Vosk)
│   └── session.py        # logging + cost tracking
├── windows/              # wake listener for non-WSLg hosts
└── data/                 # memory, reports, tasks, settings (gitignored)
```

---

## Roadmap

- "Hey Elfie" exact wake word (train the onnx)
- Auto-start WSL at Windows login (survive a reboot)
- Hermes analyst as a second delegation backend (long multi-step pipelines)
- PDF export of reports

---

MIT licensed. Built to be read, forked, and extended — start with `elfie/tools.py`.
