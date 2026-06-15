# Elfie AI — Claude Context

## What this is

Elfie is an always-on personal voice AI agent. The user talks; Elfie
listens, thinks, and responds in real time. Forked from PingOwl
(elderly wellness check-ins) and repurposed as a general personal assistant.

## Stack

| Layer | Technology |
|-------|-----------|
| Orchestration | LiveKit Agents 1.4.3 (`AgentSession` + `Agent` subclass) |
| VAD | Silero (local, free) |
| STT | Deepgram Nova-3 |
| LLM | Groq Llama-3.3-70B |
| TTS | Cartesia Sonic-2 |
| Hosting | Fly.io |

**CRITICAL — livekit-agents 1.4.x API:**
- Use `AgentSession` + `Agent` subclass — NOT `VoicePipelineAgent`
- System prompt goes in `Agent.__init__(instructions=str)`
- `Agent.on_enter()` fires when the session starts
- Tools are `@function_tool` methods on the `Agent` subclass (via `ElfieTools` mixin)
- `await session.start(agent, room=ctx.room)` launches everything

## Key files

- `elfie_agent.py` — entrypoint; `ElfieAgent` class + LiveKit wiring
- `elfie/config.py` — all settings (voice, barge-in, silence, delegation, API keys)
- `elfie/persona.py` — system prompt and greeting templates
- `elfie/tools.py` — `ElfieTools` mixin; add new tools here
- `elfie/memory.py` — three-layer persistent memory (facts / session summaries / rolling compression)
- `elfie/delegate.py` — `MANAGER` singleton; background tasks ("claude" = Claude Code headless per-task workspace, "report" = one Groq long-form call)
- `elfie/reports.py` — report store: `data/reports/*.md` + `index.json`
- `elfie/notify.py` — delivery: spoken announce + room chat (`lk.chat` topic) + optional Telegram
- `elfie/dashboard.py` — stdlib-only local web UI on :8765 (reports + tasks)
- `elfie/wakeword.py` — optional wake word listener process
- `elfie/session.py` — session logging and cost estimation

## Voice/report split (core design)

Elfie speaks 1–2 plain sentences max. Persona instructs the LLM to call
`write_report` for long/technical answers and `delegate_task` for real work.
Background completion flows: `delegate.MANAGER.on_complete` →
`_announce_finished_task` in `elfie_agent.py` → active `Notifier`
(voice + chat + Telegram). The notifier is registered per-session via
`notify.set_active()` and cleared in `on_exit`.

## Infrastructure

| Thing | Value |
|-------|-------|
| LiveKit | local Docker server (`deploy/install_services.sh`), dev keys |
| Services | systemd user units: `elfie-agent`, `elfie-dashboard` |
| Fly.io app | `elfie-ai` (optional cloud deploy) |

## Environment variables (.env)

See `.env.example` — never put real keys in this file or anywhere tracked by git.

## Design principles

1. **VAD is the cost lever** — Silero runs locally; cloud APIs only fire when speech is detected
2. **One config file** — `elfie/config.py` is the single place to tune everything
3. **Tools are the extension point** — add `@function_tool` methods to `ElfieTools`, done
4. **Persona is separable** — `elfie/persona.py` can be swapped without touching agent logic
5. **Clean > clever** — code should be readable by someone new to the project

## Barge-in settings (tuned from PingOwl)

```python
allow_interruptions = True
min_interruption_duration = 0.5   # seconds
min_interruption_words = 0        # any sound interrupts
min_endpointing_delay = 0.5       # silence before "done speaking"
```

These are the values that felt most natural in PingOwl testing.
Raise `min_interruption_words` to 1-2 in noisy environments.

## Next things to work on

1. **Live test the full loop** — talk → write_report → dashboard → announce
2. **Custom wake word** — train a "hey Elfie" ONNX model (currently hey_jarvis)
3. **More tools** — calendar integration, home automation, weather
4. **Mobile trigger** — iOS shortcut or widget to open the LiveKit room
5. **Hermes as second delegation backend** — long pipelines via kanban tasks
6. **PDF export** — render reports to PDF from the dashboard
7. **Session summaries** — daily digest of what was discussed
