"""
Elfie dashboard — where you read what's too long to hear, and watch her think.

A small local web page (no extra dependencies, stdlib only) showing:
  - Home     — reactive voice orb + Mission Control capability grid + progress
  - Skills   — the skill tree: what she can do, unlocked vs not yet learned
  - Reports  — everything Elfie wrote down instead of reading aloud
  - Tasks    — delegated background jobs and their status (auto-refreshes)
  - Activity — session costs and transcripts
  - Settings — voice, brain, wake word, conversation tuning

Run alongside the agent:
    python -m elfie.dashboard          # http://localhost:8765

Routes:
    /                  dashboard page
    /talk              talk to Elfie — mic + live chat box, fully local
    /report/<id>       a single report (rendered markdown)
    /api/reports       report index (JSON)
    /api/report/<id>   one report: {meta, content}
    /api/tasks         delegated task list (JSON)
    /api/capabilities  tools + learned skills, with last-used timestamps (JSON)
    /api/progress      headline counters: reports, tasks, skills, spend (JSON)
    /api/token         LiveKit join token for the /talk page
"""
import json
import logging
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from elfie import reports
from elfie.config import CONFIG

logger = logging.getLogger("elfie.dashboard")

ELFIE_DIR = Path(__file__).parent
DATA_DIR = ELFIE_DIR.parent / "data"
SKILLS_DIR = ELFIE_DIR / "skills"
TOOL_USAGE_FILE = DATA_DIR / "tool_usage.jsonl"

# The built-in tools, grouped, with the tool-call names that "light up" each
# tile. Keep in sync with elfie/tools.py — this is the human-facing catalogue.
CAPABILITIES = [
    # Conversation & knowledge
    {"key": "search_web",    "name": "Search the web",     "desc": "weather, news, prices, facts", "icon": "🔎", "group": "Conversation & knowledge", "ids": ["search_web"]},
    {"key": "time",          "name": "Time & date",        "desc": "what time / day is it",        "icon": "🕐", "group": "Conversation & knowledge", "ids": ["get_current_time"]},
    {"key": "notes",         "name": "Notes",              "desc": "remember & recall things",     "icon": "📝", "group": "Conversation & knowledge", "ids": ["take_note", "read_notes"]},
    {"key": "reminders",     "name": "Reminders",          "desc": "don't let me forget…",         "icon": "⏰", "group": "Conversation & knowledge", "ids": ["set_reminder"]},
    # Deep work
    {"key": "delegate_task", "name": "Delegate a task",    "desc": "Claude Code does real work",    "icon": "🤖", "group": "Deep work", "ids": ["delegate_task"]},
    {"key": "write_report",  "name": "Write a report",     "desc": "depth goes to the dashboard",   "icon": "📄", "group": "Deep work", "ids": ["write_report"]},
    {"key": "continue_task", "name": "Continue a task",    "desc": "same agent, full memory",       "icon": "↳",  "group": "Deep work", "ids": ["continue_task"]},
    {"key": "learn_skill",   "name": "Learn a new skill",  "desc": "writes + safety-checks tools",  "icon": "✨", "group": "Deep work", "ids": ["learn_skill"]},
    # Outside world
    {"key": "check_email",   "name": "Check email",        "desc": "unread Gmail summary",          "icon": "📧", "group": "The outside world", "ids": ["check_email"]},
    {"key": "open_website",  "name": "Open a website",     "desc": "open github for me",            "icon": "🌐", "group": "The outside world", "ids": ["open_website"]},
    {"key": "show_report",   "name": "Show a report",      "desc": "pops it onto your screen",       "icon": "🖥️", "group": "The outside world", "ids": ["show_report"]},
    {"key": "check_tasks",   "name": "Check task status",  "desc": "how's that job going",          "icon": "📋", "group": "The outside world", "ids": ["check_tasks"]},
]

# Aspirational nodes shown locked in the skill tree — they map to the
# "Next things to work on" roadmap and hint at what learn_skill could add.
LOCKED_NODES = [
    {"key": "calendar", "name": "Calendar",     "icon": "📅"},
    {"key": "home",     "name": "Home control", "icon": "🏠"},
    {"key": "music",    "name": "Music",        "icon": "🎵"},
]


def _tool_last_used() -> dict:
    """Map of tool-name -> latest ISO timestamp it was called, from the usage log."""
    out: dict = {}
    try:
        text = TOOL_USAGE_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return out
    except Exception as e:
        logger.warning(f"Tool-usage read failed: {e}")
        return out
    # Per-line tolerance: one corrupt/partial line (e.g. an interrupted append)
    # must not drop the valid entries around it.
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
            out[d["tool"]] = d["ts"]
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
    return out


def _learned_skills() -> list:
    """Skill files installed by learn_skill, newest mtime first."""
    if not SKILLS_DIR.exists():
        return []
    out = []
    for p in sorted(SKILLS_DIR.glob("*.py")):
        if p.stem == "__init__":
            continue
        out.append({"key": p.stem, "name": p.stem.replace("_", " ").title(), "mtime": p.stat().st_mtime})
    return out


def _capabilities() -> list:
    """Full capability catalogue for the grid + skill tree: built-ins, learned, locked."""
    last = _tool_last_used()
    caps = []
    for c in CAPABILITIES:
        lu = max((last[i] for i in c["ids"] if i in last), default=None)
        caps.append({**c, "last_used": lu, "learned": False, "locked": False, "is_new": False})
    now = time.time()
    for s in _learned_skills():
        caps.append({
            "key": s["key"], "name": s["name"], "desc": "learned skill", "icon": "✨",
            "group": "Learned skills", "ids": [s["key"]], "last_used": last.get(s["key"]),
            "learned": True, "locked": False, "is_new": (now - s["mtime"]) < 86400,
        })
    for n in LOCKED_NODES:
        caps.append({
            "key": n["key"], "name": n["name"], "desc": "not yet learned", "icon": n["icon"],
            "group": "Locked", "ids": [], "last_used": None, "learned": False, "locked": True, "is_new": False,
        })
    return caps


def _progress_summary() -> dict:
    """Headline counters for the Home stat row."""
    from elfie.delegate import _read_tasks
    try:
        tasks = _read_tasks()
    except Exception:
        tasks = []
    usage = _usage_summary()
    learned = _learned_skills()
    return {
        "reports": len(reports.list_reports()),
        "tasks_done": sum(1 for t in tasks if t.get("status") == "done"),
        "tasks_total": len(tasks),
        "skills_learned": len(learned),
        "skills_total": len(CAPABILITIES) + len(learned) + len(LOCKED_NODES),
        "skills_unlocked": len(CAPABILITIES) + len(learned),
        "spent_today": usage["today_usd"],
        "spent_total": usage["total_usd"],
        "deepgram_balance": usage["deepgram_balance_usd"],
    }


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Elfie</title>
<link rel="manifest" href="/manifest.json">
<link rel="icon" href="/icon.svg">
<meta name="theme-color" content="#0f1117">
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/livekit-client@2/dist/livekit-client.umd.min.js"></script>
<style>
  :root {
    --bg: #0f1117; --panel: #181b24; --panel2: #1d212c; --border: #2a2f3d;
    --text: #e8eaf0; --dim: #8b93a7; --accent: #7aa2f7; --green: #9ece6a;
    --red: #f7768e; --yellow: #e0af68; --violet: #bb9af7;
  }
  * { box-sizing: border-box; margin: 0; min-width: 0; }
  body {
    background:
      radial-gradient(1100px 560px at 12% -12%, #16203a 0%, transparent 55%),
      radial-gradient(820px 460px at 112% 6%, #221a33 0%, transparent 50%),
      var(--bg);
    color: var(--text);
    font: 17px/1.65 -apple-system, 'Segoe UI', Roboto, sans-serif;
    max-width: 880px; margin: 0 auto; padding: 22px 20px 70px;
  }
  header { display: flex; align-items: center; gap: 14px; margin-bottom: 16px; }
  header h1 { font-size: 26px; }
  #status { color: var(--dim); font-size: 15px; }
  #status.live { color: var(--green); }
  #connect {
    margin-left: auto; padding: 10px 22px; border-radius: 10px;
    border: 1px solid var(--accent); background: var(--panel);
    color: var(--text); font-size: 16px; font-weight: 600; cursor: pointer;
  }
  #connect.live { border-color: var(--red); }

  /* ── Reactive orb ─────────────────────────────────────────── */
  #orb-stage {
    position: relative; height: 178px; display: grid; place-items: center;
    margin: 4px 0 14px;
  }
  #orb-wrap { position: relative; width: 170px; height: 170px; display: grid; place-items: center; }
  .ring {
    position: absolute; border-radius: 50%; border: 1.5px solid rgba(122,162,247,.45);
    inset: 50% auto auto 50%; translate: -50% -50%; opacity: 0;
  }
  #orb-wrap.active .ring.r1 { animation: expand 2.6s ease-out infinite; }
  #orb-wrap.active .ring.r2 { animation: expand 2.6s ease-out infinite .85s; }
  #orb-wrap.active .ring.r3 { animation: expand 2.6s ease-out infinite 1.7s; }
  @keyframes expand { 0%{width:62px;height:62px;opacity:.85} 100%{width:168px;height:168px;opacity:0} }
  #orb {
    width: 70px; height: 70px; border-radius: 50%;
    background: radial-gradient(circle at 35% 30%, #acc3ff, var(--accent) 46%, #34509f 100%);
    box-shadow: 0 0 30px rgba(122,162,247,.6), inset 0 0 16px rgba(255,255,255,.35);
    display: grid; place-items: center; font-size: 30px; z-index: 2;
    transition: transform .08s linear, box-shadow .25s, background .4s;
    animation: breathe 3.6s ease-in-out infinite;
  }
  @keyframes breathe { 0%,100%{transform:scale(1)} 50%{transform:scale(1.05)} }
  #orb-wrap.speaking #orb { animation: none; background: radial-gradient(circle at 35% 30%, #c8f0a8, var(--green) 46%, #4a7a32 100%); box-shadow: 0 0 36px rgba(158,206,106,.6), inset 0 0 16px rgba(255,255,255,.4); }
  #orb-wrap.listening #orb { animation: none; box-shadow: 0 0 40px rgba(122,162,247,.85), inset 0 0 16px rgba(255,255,255,.45); }
  #orb-wrap.thinking #orb { background: radial-gradient(circle at 35% 30%, #e6cffb, var(--violet) 46%, #5a3f8f 100%); animation: breathe 1.1s ease-in-out infinite; }
  #orb-wrap.idle #orb { filter: saturate(.5) brightness(.8); box-shadow: 0 0 14px rgba(122,162,247,.25), inset 0 0 14px rgba(255,255,255,.2); }
  .wave { position: absolute; bottom: -4px; display: flex; gap: 3px; align-items: flex-end; height: 28px; opacity: 0; transition: opacity .25s; }
  #orb-wrap.speaking .wave, #orb-wrap.listening .wave { opacity: .9; }
  .wave i { width: 3px; height: 5px; background: var(--accent); border-radius: 2px; transition: height .07s linear; }
  #orb-wrap.speaking .wave i { background: var(--green); }
  #orb-label { position: absolute; bottom: 2px; font-size: 13px; color: var(--dim); letter-spacing: .3px; }

  /* voice chat */
  #chat {
    height: 220px; overflow-y: auto; background: var(--panel);
    border: 1px solid var(--border); border-radius: 12px;
    padding: 16px; margin-bottom: 10px;
  }
  #chat .hint { color: var(--dim); text-align: center; padding: 28px 0 0; }
  .msg { margin-bottom: 12px; }
  .msg .who { font-size: 13px; color: var(--dim); margin-bottom: 2px; }
  .msg.you .body { color: var(--accent); }
  .msg .body a { color: var(--green); }
  #composer { display: flex; gap: 8px; margin-bottom: 24px; }
  #input {
    flex: 1; padding: 12px 16px; border-radius: 10px; border: 1px solid var(--border);
    background: var(--panel); color: var(--text); font-size: 16px;
  }
  #send {
    padding: 12px 22px; border-radius: 10px; border: 1px solid var(--border);
    background: var(--panel); color: var(--text); font-size: 16px; cursor: pointer;
  }

  /* tabs + lists */
  .tabs { display: flex; gap: 8px; margin-bottom: 18px; flex-wrap: wrap; }
  .tab {
    padding: 8px 16px; border-radius: 9px; border: 1px solid var(--border);
    background: var(--panel); color: var(--dim); cursor: pointer; font-size: 15px;
  }
  .tab.active { color: var(--text); border-color: var(--accent); }
  .card {
    background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
    padding: 16px 20px; margin-bottom: 12px; cursor: pointer;
  }
  .card:hover { border-color: var(--accent); }
  .card .title { font-size: 18px; font-weight: 600; }
  .card .meta { color: var(--dim); font-size: 14px; margin-top: 4px; }
  .badge {
    display: inline-block; padding: 2px 10px; border-radius: 999px;
    font-size: 13px; font-weight: 600; margin-right: 8px;
  }
  .badge.done    { background: #1e2e1e; color: var(--green); }
  .badge.running { background: #2e2a1e; color: var(--yellow); }
  .badge.failed, .badge.timeout { background: #2e1e22; color: var(--red); }
  .empty { color: var(--dim); padding: 40px 0; text-align: center; font-size: 16px; }

  /* ── Mission Control capability grid ──────────────────────── */
  .group-label { color: var(--violet); font-size: 12px; letter-spacing: 1.5px; text-transform: uppercase; margin: 18px 0 9px; }
  .group-label:first-child { margin-top: 4px; }
  .cap-grid { display: grid; gap: 11px; grid-template-columns: repeat(auto-fill, minmax(min(210px,100%),1fr)); }
  .cap {
    background: linear-gradient(180deg, var(--panel) 0%, #15181f 100%);
    border: 1px solid var(--border); border-radius: 13px; padding: 13px 15px;
    display: flex; gap: 11px; align-items: flex-start; position: relative; overflow: hidden;
    transition: border-color .3s, box-shadow .3s;
  }
  .cap .ico { font-size: 20px; line-height: 1.3; }
  .cap .name { font-weight: 650; font-size: 15px; }
  .cap .desc { color: var(--dim); font-size: 12.5px; margin-top: 1px; }
  .cap.on { border-color: var(--accent); box-shadow: 0 0 18px -6px var(--accent) inset; }
  .cap.on .dot { position: absolute; top: 12px; right: 12px; width: 7px; height: 7px; border-radius: 50%; background: var(--green); box-shadow: 0 0 0 0 rgba(158,206,106,.6); animation: ping 2.2s ease-out infinite; }
  .cap.fresh { animation: flash 1.4s ease-out; }
  @keyframes ping { 0%{box-shadow:0 0 0 0 rgba(158,206,106,.5)} 70%{box-shadow:0 0 0 9px rgba(158,206,106,0)} 100%{box-shadow:0 0 0 0 rgba(158,206,106,0)} }
  @keyframes flash { 0%{box-shadow:0 0 0 2px var(--green) inset} 100%{box-shadow:0 0 18px -6px var(--accent) inset} }
  .cap.learned { border-color: #3a4a2e; }
  .cap.locked { opacity: .45; }

  /* progress stats */
  .stats { display: grid; gap: 11px; grid-template-columns: repeat(auto-fit, minmax(min(150px,100%),1fr)); margin: 4px 0 12px; }
  .stat { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 14px 16px; }
  .stat .n { font-size: 28px; font-weight: 750; font-variant-numeric: tabular-nums; }
  .stat .n.accent { color: var(--accent); } .stat .n.green { color: var(--green); } .stat .n.violet { color: var(--violet); }
  .stat .l { color: var(--dim); font-size: 13px; margin-top: 2px; }
  .bar { height: 7px; border-radius: 999px; background: #11141b; overflow: hidden; margin-top: 9px; }
  .bar > i { display: block; height: 100%; border-radius: 999px; background: linear-gradient(90deg, var(--accent), var(--violet)); animation: fill 1.4s cubic-bezier(.2,.7,.2,1) both; }
  @keyframes fill { from { width: 0 !important; } }

  /* ── Skill tree ───────────────────────────────────────────── */
  .tree-head { display: flex; align-items: baseline; gap: 12px; margin-bottom: 14px; flex-wrap: wrap; }
  .tree-head .lvl { font-size: 20px; font-weight: 750; color: var(--violet); }
  .xp-bar { flex: 1; min-width: 140px; height: 8px; border-radius: 999px; background: #11141b; overflow: hidden; }
  .xp-bar > i { display: block; height: 100%; background: linear-gradient(90deg, var(--violet), var(--accent)); animation: fill 1.6s both; }
  .tier-label { color: var(--dim); font-size: 12px; letter-spacing: 1.5px; text-transform: uppercase; margin: 16px 0 9px; }
  .tier { display: flex; flex-wrap: wrap; gap: 12px; }
  .tnode {
    display: grid; place-items: center; width: 76px; height: 76px; border-radius: 16px;
    background: #11151e; border: 1px solid var(--border); position: relative; text-align: center;
    padding: 6px; gap: 2px;
  }
  .tnode .e { font-size: 24px; }
  .tnode .nm { font-size: 10.5px; color: var(--dim); line-height: 1.1; }
  .tnode.unlocked { border-color: var(--green); box-shadow: 0 0 14px -5px var(--green); }
  .tnode.unlocked .nm { color: var(--text); }
  .tnode.locked { opacity: .4; filter: grayscale(1); }
  .tnode.newnode { border-color: var(--violet); animation: pop 2.4s ease-in-out infinite; }
  .tnode.newnode::after { content: 'NEW'; position: absolute; top: -8px; right: -8px; font-size: 8px; font-weight: 800; color: var(--violet); background: #1b1530; border: 1px solid var(--violet); border-radius: 6px; padding: 1px 4px; }
  @keyframes pop { 0%,100%{transform:scale(1);box-shadow:0 0 10px -4px var(--violet)} 50%{transform:scale(1.07);box-shadow:0 0 22px 0 var(--violet)} }

  /* report view */
  #report-view { display: none; }
  #report-body {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 12px; padding: 28px 32px;
  }
  #report-body h1, #report-body h2, #report-body h3 { margin: 22px 0 10px; line-height: 1.3; }
  #report-body h1:first-child { margin-top: 0; }
  #report-body p, #report-body ul, #report-body ol { margin-bottom: 14px; }
  #report-body li { margin: 4px 0 4px 8px; }
  #report-body code {
    background: #0c0e13; padding: 2px 7px; border-radius: 5px;
    font: 14.5px/1.5 'JetBrains Mono', Consolas, monospace; color: var(--accent);
  }
  #report-body pre { background: #0c0e13; padding: 16px; border-radius: 9px; overflow-x: auto; margin-bottom: 14px; }
  #report-body pre code { background: none; padding: 0; color: var(--text); }
  #report-body table { border-collapse: collapse; margin-bottom: 14px; width: 100%; }
  #report-body th, #report-body td { border: 1px solid var(--border); padding: 8px 12px; text-align: left; }
  #report-body th { background: #1d212c; }
  #back { color: var(--accent); text-decoration: none; display: inline-block; margin-bottom: 16px; font-size: 16px; }
</style>
</head>
<body>
<header>
  <h1>🦉 Elfie</h1>
  <span id="status">not connected</span>
  <button id="testwake" onclick="testWake()" style="display:none;margin-left:auto;padding:10px 16px;border-radius:10px;border:1px solid var(--border);background:var(--panel);color:var(--dim);font-size:15px;cursor:pointer">🔔 Test wake</button>
  <button id="connect" onclick="toggle()">🎙️ Connect</button>
</header>

<div id="orb-stage">
  <div id="orb-wrap" class="idle">
    <div class="ring r1"></div><div class="ring r2"></div><div class="ring r3"></div>
    <div id="orb">🦉</div>
    <div class="wave"><i></i><i></i><i></i><i></i><i></i><i></i><i></i></div>
    <div id="orb-label">not connected</div>
  </div>
</div>

<div id="chat"><div class="hint">Hit Connect and just talk — the conversation appears here,<br>along with links to any reports Elfie writes for you.</div></div>
<div id="composer">
  <input id="input" placeholder="…or type to Elfie" onkeydown="if(event.key==='Enter')sendText()">
  <button id="send" onclick="sendText()">Send</button>
</div>

<div id="list-view">
  <div class="tabs">
    <button class="tab active" id="tab-home" onclick="showTab('home')">🛰️ Home</button>
    <button class="tab" id="tab-skills" onclick="showTab('skills')">🌳 Skills</button>
    <button class="tab" id="tab-reports" onclick="showTab('reports')">Reports</button>
    <button class="tab" id="tab-tasks" onclick="showTab('tasks')">Tasks</button>
    <button class="tab" id="tab-activity" onclick="showTab('activity')">Activity</button>
    <button class="tab" id="tab-settings" onclick="showTab('settings')">⚙️ Settings</button>
  </div>
  <div id="list"></div>
</div>

<div id="report-view">
  <a id="back" href="javascript:closeReport()">&larr; Back</a>
  <div id="report-body"></div>
</div>

<script>
// PWA install support
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});

// ── Voice + chat ─────────────────────────────────────────────────────────────
const { Room, RoomEvent } = LivekitClient;
let room = null;
let userDisconnected = false;
let reconnectAttempts = 0;
let mode = 'alwayson';          // 'alwayson' | 'wakeword' — fetched from settings
let lastWakeTs = 0;             // newest wake event we've acted on

function scheduleReconnect() {
  // Only always-on mode auto-reconnects. In wake-word mode a disconnect is
  // intentional (she hung up) — we go back to waiting for the wake word.
  if (userDisconnected || mode === 'wakeword') return;
  reconnectAttempts++;
  const delay = Math.min(15000, 2000 * reconnectAttempts);
  setStatus(`reconnecting (attempt ${reconnectAttempts})…`, false);
  setTimeout(() => connect().catch(scheduleReconnect), delay);
}

// Wake-word mode: sit idle, poll the server for a wake event, connect on one.
async function pollWake() {
  if (mode !== 'wakeword' || room) return;
  try {
    const { ts } = await (await fetch('/api/wake')).json();
    if (ts > lastWakeTs) { lastWakeTs = ts; userDisconnected = false; connect(); }
  } catch (e) {}
}
setInterval(pollWake, 1500);

function enterWaiting() {
  setStatus('💤 waiting — say the wake word (or click Test wake)', false);
}

async function testWake() { await fetch('/api/wake', { method: 'POST' }); }

function addMsg(who, text, cls) {
  const hint = document.querySelector('#chat .hint');
  if (hint) hint.remove();
  const linked = text.replace(/(https?:\\/\\/\\S+)/g, '<a href="$1" target="_blank">$1</a>');
  const div = document.createElement('div');
  div.className = 'msg ' + cls;
  div.innerHTML = `<div class="who">${who}</div><div class="body">${linked}</div>`;
  const chat = document.getElementById('chat');
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
}

function setStatus(text, live) {
  const s = document.getElementById('status');
  s.textContent = text; s.className = live ? 'live' : '';
  const b = document.getElementById('connect');
  b.textContent = live ? '⏹ Disconnect' : '🎙️ Connect';
  b.className = live ? 'live' : '';
}

// ── Reactive orb ─────────────────────────────────────────────────────────────
// State is driven by live audio: a WebAudio analyser taps the mic (listening)
// and Elfie's track (speaking). Everything is wrapped in try/catch so a
// WebAudio failure degrades to transcription-driven state, never breaks voice.
let audioCtx = null, micAnalyser = null, agentAnalyser = null, thinkingUntil = 0;
const waveBars = () => document.querySelectorAll('.wave i');

function ensureCtx() {
  if (!audioCtx) { try { audioCtx = new (window.AudioContext || window.webkitAudioContext)(); } catch (e) {} }
  if (audioCtx && audioCtx.state === 'suspended') audioCtx.resume().catch(() => {});
  return audioCtx;
}
function makeAnalyser(mst) {
  try {
    const ctx = ensureCtx(); if (!ctx || !mst) return null;
    const src = ctx.createMediaStreamSource(new MediaStream([mst]));
    const an = ctx.createAnalyser(); an.fftSize = 256; src.connect(an); return an;
  } catch (e) { return null; }
}
function levelOf(an) {
  if (!an) return 0;
  const buf = new Uint8Array(an.frequencyBinCount); an.getByteTimeDomainData(buf);
  let s = 0; for (const v of buf) { const x = (v - 128) / 128; s += x * x; }
  return Math.sqrt(s / buf.length);
}
function setOrb(state, level) {
  const w = document.getElementById('orb-wrap');
  const orb = document.getElementById('orb');
  const label = document.getElementById('orb-label');
  w.className = state + (state === 'idle' ? '' : ' active');
  const labels = { idle: 'not connected', ready: '● live — just talk', listening: 'listening…', thinking: 'thinking…', speaking: 'speaking' };
  label.textContent = labels[state] || '';
  const scale = 1 + Math.min(0.5, level * 2.2);
  if (state === 'speaking' || state === 'listening') orb.style.transform = `scale(${scale.toFixed(3)})`;
  else orb.style.transform = '';
  const bars = waveBars();
  if (state === 'speaking' || state === 'listening') {
    bars.forEach((b, i) => {
      const h = 5 + Math.abs(Math.sin(Date.now() / 90 + i)) * level * 90;
      b.style.height = Math.min(26, h).toFixed(1) + 'px';
    });
  }
}
function orbLoop() {
  const u = levelOf(micAnalyser), e = levelOf(agentAnalyser);
  let state = 'idle';
  if (!room) state = 'idle';
  else if (e > 0.03) { state = 'speaking'; thinkingUntil = 0; }
  else if (Date.now() < thinkingUntil) state = 'thinking';
  else if (u > 0.045) state = 'listening';
  else state = 'ready';
  setOrb(state, Math.max(u, e));
  requestAnimationFrame(orbLoop);
}
requestAnimationFrame(orbLoop);

async function connect() {
  setStatus('connecting…', false);
  ensureCtx();
  const { url, token } = await (await fetch('/api/token')).json();
  room = new Room();

  room.on(RoomEvent.TrackSubscribed, (track) => {
    if (track.kind === 'audio') {
      document.body.appendChild(track.attach());
      try { agentAnalyser = makeAnalyser(track.mediaStreamTrack); } catch (e) {}
    }
  });
  room.on(RoomEvent.Disconnected, () => {
    room = null; micAnalyser = null; agentAnalyser = null;
    if (mode === 'wakeword') enterWaiting();   // wake word will bring her back
    else { setStatus('not connected', false); scheduleReconnect(); }
  });
  room.registerTextStreamHandler('lk.chat', async (reader, p) => {
    const text = await reader.readAll();
    if (p?.identity !== room.localParticipant.identity) addMsg('Elfie', text, 'elfie');
  });
  room.registerTextStreamHandler('lk.transcription', async (reader, p) => {
    const text = await reader.readAll();
    if (reader.info.attributes['lk.transcription_final'] !== 'true') return;
    const isAgent = p?.identity?.startsWith('agent');
    if (!isAgent) thinkingUntil = Date.now() + 5000;   // user finished → she's thinking
    addMsg(isAgent ? 'Elfie 🔊' : 'You 🎙️', text, isAgent ? 'elfie' : 'you');
  });

  await room.connect(url, token);
  await room.localParticipant.setMicrophoneEnabled(true);
  try {
    const pub = [...room.localParticipant.audioTrackPublications.values()][0];
    micAnalyser = makeAnalyser(pub?.track?.mediaStreamTrack);
  } catch (e) {}
  setStatus('● live — just talk', true);
  localStorage.setItem('elfie-autoconnect', '1');
  reconnectAttempts = 0;

  // If no agent shows up (e.g. the worker is restarting), this room is dead —
  // leave it, which triggers the reconnect loop into a fresh room.
  setTimeout(() => {
    if (room && room.remoteParticipants.size === 0) {
      setStatus('Elfie didn\\'t join — retrying…', false);
      room.disconnect();
    }
  }, 12000);
}

async function toggle() {
  if (room) {
    userDisconnected = true;
    localStorage.removeItem('elfie-autoconnect');
    await room.disconnect();
    return;
  }
  userDisconnected = false;
  await connect();
}

async function sendText() {
  const input = document.getElementById('input');
  const text = input.value.trim();
  if (!text) return;
  if (!room) { await toggle(); }
  input.value = '';
  addMsg('You ⌨️', text, 'you');
  await room.localParticipant.sendText(text, { topic: 'lk.chat' });
}

// On load, decide behavior from the saved mode:
//  - always-on: auto-connect if we were connected last time
//  - wake word: stay idle and wait for the wake word (never auto-connect)
async function initConnection() {
  if (location.pathname.startsWith('/report/')) return;  // deep-links never connect
  try {
    const s = await (await fetch('/api/settings')).json();
    mode = s.end_on_silence ? 'wakeword' : 'alwayson';
  } catch (e) {}
  if (mode === 'wakeword') {
    document.getElementById('testwake').style.display = 'inline-block';
    lastWakeTs = Date.now() / 1000;   // ignore any stale wake from before load
    enterWaiting();
  } else if (localStorage.getItem('elfie-autoconnect')) {
    connect().catch(() => setStatus('click Connect to start', false));
  }
}
initConnection();

// ── Tabs, capabilities, progress, skill tree, reports, tasks ─────────────────
let tab = 'home';
const TABS = ['home', 'skills', 'reports', 'tasks', 'activity', 'settings'];
const seenFresh = {};   // capability key -> last_used we already flashed

function showTab(t) {
  tab = t;
  for (const name of TABS) document.getElementById('tab-' + name).classList.toggle('active', t === name);
  refresh();
}

// Escape user/data-derived text before it goes into innerHTML. Report titles,
// task titles, learned-skill names (from filenames) and session transcripts are
// all attacker-influenceable; without this a title like `<img onerror>` would
// execute. URLs are linkified separately in addMsg.
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function timeAgo(iso) {
  const s = (Date.now() - new Date(iso)) / 1000;
  if (s < 60) return 'just now';
  if (s < 3600) return Math.floor(s / 60) + ' min ago';
  if (s < 86400) return Math.floor(s / 3600) + ' h ago';
  return Math.floor(s / 86400) + ' d ago';
}

function renderHome(caps, prog) {
  const list = document.getElementById('list');
  const dgm = prog.deepgram_balance != null ? prog.deepgram_balance : null;
  const stats = `
    <div class="stats">
      <div class="stat"><div class="n accent">${prog.reports}</div><div class="l">Reports written</div></div>
      <div class="stat"><div class="n green">${prog.tasks_done}</div><div class="l">Tasks completed</div></div>
      <div class="stat"><div class="n violet">${prog.skills_learned}</div><div class="l">Skills learned</div></div>
      <div class="stat"><div class="n">$${prog.spent_total.toFixed(2)}</div><div class="l">Spent all-time</div></div>
    </div>
    <div class="card" style="cursor:default">
      <div style="display:flex;justify-content:space-between;font-size:14px"><span>Capabilities unlocked</span><span class="meta">${prog.skills_unlocked} / ${prog.skills_total}</span></div>
      <div class="bar"><i style="width:${Math.round(100*prog.skills_unlocked/prog.skills_total)}%"></i></div>
      ${dgm != null ? `<div style="display:flex;justify-content:space-between;font-size:14px;margin-top:14px"><span>Deepgram credit remaining</span><span class="meta">$${dgm}</span></div>
      <div class="bar"><i style="width:${Math.min(100,Math.round(100*dgm/200))}%;animation-delay:.15s"></i></div>` : ''}
    </div>`;

  // group the live (non-locked) capabilities for the Mission Control grid
  const groups = {};
  for (const c of caps) { if (c.locked) continue; (groups[c.group] = groups[c.group] || []).push(c); }
  let grid = '';
  for (const [g, items] of Object.entries(groups)) {
    grid += `<div class="group-label">${g}</div><div class="cap-grid">`;
    for (const c of items) {
      const recent = c.last_used && (Date.now() - new Date(c.last_used)) < 30 * 60 * 1000;
      const fresh = c.last_used && seenFresh[c.key] && seenFresh[c.key] !== c.last_used;
      if (c.last_used) seenFresh[c.key] = c.last_used;
      const cls = ['cap', recent ? 'on' : '', c.learned ? 'learned' : '', fresh ? 'fresh' : ''].join(' ').trim();
      grid += `<div class="${cls}">
        ${recent ? '<span class="dot"></span>' : ''}
        <span class="ico">${c.icon}</span>
        <div><div class="name">${esc(c.name)}</div><div class="desc">${esc(c.learned ? 'learned skill' : c.desc)}</div></div>
      </div>`;
    }
    grid += `</div>`;
  }
  list.innerHTML = stats + grid;
}

function renderSkills(caps, prog) {
  const list = document.getElementById('list');
  const lvl = 1 + prog.skills_learned + Math.floor(prog.tasks_done / 3);
  const tiers = { 'Core': [], 'Learned skills': [], 'Locked': [] };
  for (const c of caps) {
    if (c.locked) tiers['Locked'].push(c);
    else if (c.learned) tiers['Learned skills'].push(c);
    else tiers['Core'].push(c);
  }
  let html = `
    <div class="tree-head">
      <span class="lvl">Level ${lvl}</span>
      <div class="xp-bar"><i style="width:${Math.round(100*prog.skills_unlocked/prog.skills_total)}%"></i></div>
      <span class="meta">${prog.skills_unlocked} of ${prog.skills_total} abilities</span>
    </div>`;
  for (const [name, items] of Object.entries(tiers)) {
    if (!items.length) continue;
    html += `<div class="tier-label">${name === 'Locked' ? 'Not yet learned' : name}</div><div class="tier">`;
    for (const c of items) {
      const cls = ['tnode', c.locked ? 'locked' : 'unlocked', c.is_new ? 'newnode' : ''].join(' ').trim();
      html += `<div class="${cls}"><span class="e">${c.icon}</span><span class="nm">${esc(c.name)}</span></div>`;
    }
    html += `</div>`;
  }
  html += `<div class="meta" style="margin-top:18px">New abilities arrive when you ask Elfie to <b>learn a skill</b> — she researches it, writes it, runs a safety check, and a fresh node lights up here.</div>`;
  list.innerHTML = html;
}

async function refresh() {
  const list = document.getElementById('list');
  if (tab === 'home' || tab === 'skills') {
    const [caps, prog] = await Promise.all([
      (await fetch('/api/capabilities')).json(),
      (await fetch('/api/progress')).json(),
    ]);
    if (tab === 'home') renderHome(caps, prog); else renderSkills(caps, prog);
    return;
  }
  if (tab === 'activity') {
    const u = await (await fetch('/api/usage')).json();
    const dg = u.deepgram_balance_usd > 0
      ? ` &nbsp;·&nbsp; Deepgram credit left: $${u.deepgram_balance_usd} (actual)` : '';
    let html = `
      <div class="card" style="cursor:default">
        <div class="title">💳 Today: $${u.today_usd.toFixed(4)} &nbsp;·&nbsp; All time: $${u.total_usd.toFixed(4)}${dg}</div>
        <div class="meta">Usage units (tokens, audio minutes) are measured; dollar figures use list prices. Groq/Cartesia exact billing lives in their consoles.</div>
      </div>`;
    const byDay = {};
    for (const s of u.recent) {
      const day = new Date(s.timestamp).toLocaleDateString();
      (byDay[day] = byDay[day] || []).push(s);
    }
    for (const [day, sessions] of Object.entries(byDay)) {
      const cost = sessions.reduce((a, s) => a + (s.cost_usd || 0), 0);
      const secs = sessions.reduce((a, s) => a + (s.duration_sec || 0), 0);
      html += `<div class="card" style="cursor:default;background:#1d212c">
        <div class="title" style="font-size:16px">${day} — $${cost.toFixed(4)} · ${Math.round(secs / 60)} min · ${sessions.length} sessions</div></div>`;
      const real = sessions.filter(s => (s.turns || []).some(t => t.user));
      const blips = sessions.length - real.length;
      for (const s of real) {
        const turns = s.turns.map(t =>
          (t.user ? `<div class="meta" style="color:var(--accent)">You: ${esc(t.user)}</div>` : '') +
          `<div class="meta">Elfie: ${esc(t.elfie)}</div>`).join('');
        html += `<details class="card" style="cursor:default">
          <summary style="cursor:pointer" class="title" style="font-size:16px">
            ${new Date(s.timestamp).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}
            · ${Math.round(s.duration_sec)}s · $${(s.cost_usd ?? 0).toFixed(4)} · ${s.turns.filter(t => t.user).length} exchanges
          </summary>
          <div style="margin-top:10px">${turns}</div>
        </details>`;
      }
      if (blips > 0) html += `<div class="meta" style="margin:0 0 12px 8px">+ ${blips} brief connections with no conversation</div>`;
    }
    list.innerHTML = html;
    return;
  }
  if (tab === 'settings') {
    const [s, voices] = await Promise.all([
      (await fetch('/api/settings')).json(),
      (await fetch('/api/voices')).json(),
    ]);
    const voiceOpts = voices.map(v =>
      `<option value="${v.id}" ${v.id === s.tts_voice ? 'selected' : ''}>${v.name} — ${v.description}</option>`).join('');
    const BRAINS = [
      ['openai/gpt-oss-120b', 'GPT-OSS 120B — smart + reliable tools (recommended)'],
      ['meta-llama/llama-4-scout-17b-16e-instruct', 'Llama 4 Scout 17B — fast, cheap, reliable tools'],
      ['openai/gpt-oss-20b', 'GPT-OSS 20B — cheapest, reliable tools'],
      ['llama-3.3-70b-versatile', 'Llama 3.3 70B — smart BUT unreliable tool calls (may hallucinate)'],
      ['llama-3.1-8b-instant', 'Llama 3.1 8B — fastest, simplest'],
    ];
    list.innerHTML = `
      <div class="card" style="cursor:default;border-color:var(--accent)">
        <div class="title" style="margin-bottom:6px">Mode — how Elfie listens</div>
        <label class="meta" style="display:block;margin-bottom:8px">
          <input type="radio" name="mode" value="alwayson" ${!s.end_on_silence?'checked':''}>
          <b>Always-on</b> — stay connected, just talk. No wake word needed.
          Keep the tab open. (Streams audio to Deepgram continuously, so about 35¢/hour even while idle.)
        </label>
        <label class="meta" style="display:block">
          <input type="radio" name="mode" value="wakeword" ${s.end_on_silence?'checked':''}>
          <b>Wake word</b> — hangs up after silence, so it's <b>free when idle</b>.
          Say the wake word to start again (run windows/wake_listener.py for hands-free).
        </label>
      </div>
      <div class="card" style="cursor:default">
        <div class="title" style="margin-bottom:12px">Brain (LLM)</div>
        <label class="meta">Model &nbsp;<select id="set-brain" style="max-width:460px">
          ${BRAINS.map(([id, label]) => `<option value="${id}" ${id === s.llm_model ? 'selected' : ''}>${label}</option>`).join('')}
        </select></label>
      </div>
      <div class="card" style="cursor:default">
        <div class="title" style="margin-bottom:12px">Voice</div>
        <label class="meta">Voice &nbsp;<select id="set-voice" style="max-width:420px">${voiceOpts || `<option value="${s.tts_voice}" selected>current voice</option>`}</select></label><br><br>
        <label class="meta">Speed &nbsp;<select id="set-speed">
          ${['slow','normal','fast'].map(x => `<option ${x===s.tts_speed?'selected':''}>${x}</option>`).join('')}
        </select></label>
      </div>
      <div class="card" style="cursor:default">
        <div class="title" style="margin-bottom:12px">Wake word <span class="meta">(runs on Windows: windows/wake_listener.py — restart it to apply)</span></div>
        <label class="meta">Phrase &nbsp;<select id="set-wake">
          ${[['hey_jarvis','Hey Jarvis (fastest, stock model)'],['alexa','Alexa (stock)'],['hey_mycroft','Hey Mycroft (stock)'],['hey_elfie','Custom phrase (no training — via local Vosk)']].map(([v,l])=>`<option value="${v}" ${v===s.wake_word?'selected':''}>${l}</option>`).join('')}
        </select></label><br><br>
        <label class="meta">Custom phrase (real words only — Vosk can't hear made-up names; "hey ellie" ≈ "hey elfie") &nbsp;
          <input id="set-wakephrase" type="text" value="${s.wake_phrase || 'hey ellie'}" style="width:160px"></label><br><br>
        <label class="meta">Sensitivity (lower = triggers more easily) &nbsp;
          <input id="set-wakethresh" type="number" step="0.05" min="0.1" max="0.95" value="${s.threshold}" style="width:70px"></label><br><br>
        <label class="meta">Cooldown after trigger (seconds) &nbsp;
          <input id="set-wakecooldown" type="number" step="1" min="2" max="60" value="${s.cooldown_sec}" style="width:70px"></label>
      </div>
      <div class="card" style="cursor:default">
        <div class="title" style="margin-bottom:12px">Conversation</div>
        <label class="meta">Patience before she answers (seconds of silence) &nbsp;
          <input id="set-endpoint" type="number" step="0.1" min="0.3" max="3" value="${s.min_endpointing_delay}" style="width:70px"></label><br><br>
        <label class="meta">Quiet time before "Still there?" (seconds) &nbsp;
          <input id="set-silence" type="number" step="5" min="5" max="600" value="${s.silence_timeout_sec}" style="width:70px"></label><br><br>
        <label class="meta"><input id="set-autoopen" type="checkbox" ${s.auto_open_reports ? 'checked' : ''}> Pop finished reports into the browser automatically</label>
      </div>
      <button class="tab" style="color:var(--text);border-color:var(--accent)" onclick="saveSettings()">💾 Save — applies on next connect</button>
      <span class="meta" id="save-note"></span>`;
    document.querySelectorAll('#list select, #list input').forEach(el => el.style.cssText += ';background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px;font-size:15px');
    return;
  }
  if (tab === 'reports') {
    const items = await (await fetch('/api/reports')).json();
    list.innerHTML = items.length ? items.map(r => `
      <div class="card" onclick="openReport('${r.id}')">
        <div class="title">📄 ${esc(r.title)}</div>
        <div class="meta">${esc(r.source)} · ${r.words} words · ${timeAgo(r.created)}</div>
      </div>`).join('') : '<div class="empty">No reports yet — ask Elfie something technical.</div>';
  } else {
    const items = await (await fetch('/api/tasks')).json();
    list.innerHTML = items.length ? items.map(t => `
      <div class="card" ${t.report_id ? `onclick="openReport('${t.report_id}')"` : ''}>
        <div class="title"><span class="badge ${t.status}">${t.status}</span>${esc(t.title)}</div>
        <div class="meta">${esc(t.kind)} · started ${timeAgo(t.created)}${t.error ? ' · ' + esc(t.error) : ''}</div>
      </div>`).join('') : '<div class="empty">No tasks yet — say "Elfie, can you research…"</div>';
  }
}

async function saveSettings() {
  const body = {
    llm_model: document.getElementById('set-brain').value,
    tts_voice: document.getElementById('set-voice').value,
    tts_speed: document.getElementById('set-speed').value,
    wake_word: document.getElementById('set-wake').value,
    wake_phrase: document.getElementById('set-wakephrase').value.toLowerCase().trim(),
    threshold: parseFloat(document.getElementById('set-wakethresh').value),
    cooldown_sec: parseInt(document.getElementById('set-wakecooldown').value),
    min_endpointing_delay: parseFloat(document.getElementById('set-endpoint').value),
    silence_timeout_sec: parseFloat(document.getElementById('set-silence').value),
    auto_open_reports: document.getElementById('set-autoopen').checked,
    end_on_silence: document.querySelector('input[name=mode]:checked').value === 'wakeword',
  };
  await fetch('/api/settings', { method: 'POST', body: JSON.stringify(body) });
  document.getElementById('save-note').textContent = ' saved ✓ — reconnect to apply';
}

async function showReport(id) {
  document.getElementById('list-view').style.display = 'none';
  document.getElementById('report-view').style.display = 'block';
  const r = await (await fetch('/api/report/' + id)).json();
  document.title = r.meta.title + ' — Elfie';
  document.getElementById('report-body').innerHTML = marked.parse(r.content);
}

// In-page navigation — the page never reloads, so the live voice session
// survives opening and closing reports.
function openReport(id) {
  history.pushState({report: id}, '', '/report/' + id);
  showReport(id);
}

function closeReport() {
  history.pushState({}, '', '/');
  document.title = 'Elfie';
  document.getElementById('report-view').style.display = 'none';
  document.getElementById('list-view').style.display = 'block';
  refresh();
}

window.onpopstate = () => {
  const m = location.pathname.match(/^\\/report\\/([a-z0-9-]+)$/);
  if (m) showReport(m[1]); else closeReport();
};

const m = location.pathname.match(/^\\/report\\/([a-z0-9-]+)$/);
if (m) { showReport(m[1]); }
else { refresh(); }
setInterval(() => { if (tab !== 'settings' && document.getElementById('list-view').style.display !== 'none') refresh(); }, 5000);
</script>
</body>
</html>
"""


def _mint_token() -> dict:
    """Short-lived join token for the local /talk page."""
    import datetime
    import time
    from livekit import api
    token = (
        api.AccessToken(CONFIG.livekit_api_key, CONFIG.livekit_api_secret)
        .with_identity(CONFIG.owner_name or "owner")
        .with_name(CONFIG.owner_name or "Owner")
        # Fresh room per connection — a new room dispatches a new agent job,
        # so reconnecting always gets a live, greeting Elfie (never a dead room).
        .with_grants(api.VideoGrants(room_join=True, room=f"elfie-{int(time.time())}"))
        .with_ttl(datetime.timedelta(hours=12))
        .to_jwt()
    )
    return {"url": CONFIG.livekit_url, "token": token}


# PWA bits — lets the browser "install" Elfie as an app window
MANIFEST = json.dumps({
    "name": "Elfie",
    "short_name": "Elfie",
    "description": "Always-on personal voice AI",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#0f1117",
    "theme_color": "#0f1117",
    "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}],
})

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
<rect width="100" height="100" rx="20" fill="#0f1117"/>
<text x="50" y="68" font-size="56" text-anchor="middle">🦉</text>
</svg>"""

SERVICE_WORKER = "self.addEventListener('fetch', () => {});"

# Latest wake-word event timestamp — POSTed by the Windows listener (or the
# Test-wake button), polled by the open page in wake-word mode.
_WAKE = {"ts": 0.0}

_voices_cache: list | None = None


def _list_voices() -> list:
    """English Cartesia voices for the settings dropdown (cached)."""
    global _voices_cache
    if _voices_cache is not None:
        return _voices_cache
    import httpx
    try:
        r = httpx.get(
            "https://api.cartesia.ai/voices",
            headers={"X-API-Key": CONFIG.cartesia_api_key, "Cartesia-Version": "2024-06-10"},
            timeout=10.0,
        )
        data = r.json()
        voices = data.get("data", data) if isinstance(data, dict) else data
        _voices_cache = [
            {"id": v["id"], "name": v["name"], "description": (v.get("description") or "")[:80]}
            for v in voices
            if v.get("language", "en") == "en"
        ][:30]
    except Exception as e:
        logger.warning(f"Voice list fetch failed: {e}")
        _voices_cache = []
    return _voices_cache


_deepgram_cache: dict = {"at": 0.0, "value": None}


def _deepgram_balance() -> float | None:
    """Actual remaining Deepgram credit (their billing API), cached 5 min."""
    import time

    import httpx
    if time.time() - _deepgram_cache["at"] < 300:
        return _deepgram_cache["value"]
    balance = None
    try:
        headers = {"Authorization": f"Token {CONFIG.deepgram_api_key}"}
        projects = httpx.get("https://api.deepgram.com/v1/projects", headers=headers, timeout=8.0).json()
        pid = projects["projects"][0]["project_id"]
        data = httpx.get(f"https://api.deepgram.com/v1/projects/{pid}/balances", headers=headers, timeout=8.0).json()
        balance = round(sum(b.get("amount", 0) for b in data.get("balances", [])), 2)
    except Exception as e:
        logger.warning(f"Deepgram balance fetch failed: {e}")
    _deepgram_cache.update(at=time.time(), value=balance)
    return balance


def _usage_summary() -> dict:
    """Aggregate session costs from data/sessions.jsonl for the Activity tab."""
    from datetime import datetime, timezone
    path = reports.REPORTS_DIR.parent / "sessions.jsonl"
    sessions = []
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    # Per-line tolerance: a single corrupt/partial line (e.g. an interrupted
    # append) must degrade to "skip that line", never crash the endpoint.
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            sessions.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    today = datetime.now(timezone.utc).date().isoformat()
    return {
        "today_usd": round(sum(s.get("cost_usd", 0) for s in sessions if s.get("timestamp", "")[:10] == today), 4),
        "total_usd": round(sum(s.get("cost_usd", 0) for s in sessions), 4),
        "session_count": len(sessions),
        "deepgram_balance_usd": _deepgram_balance(),
        "recent": list(reversed(sessions))[:60],
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quiet the default per-request logging
        pass

    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Never cache — this app changes often; a stale page silently runs old
        # logic (e.g. auto-connecting in wake-word mode). Always serve fresh.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status: int = 200) -> None:
        self._send(json.dumps(data).encode(), "application/json", status)

    def do_GET(self):
        path = self.path.split("?")[0]

        if path in ("/", "/talk") or path.startswith("/report/"):
            self._send(PAGE.encode(), "text/html; charset=utf-8")
        elif path == "/manifest.json":
            self._send(MANIFEST.encode(), "application/manifest+json")
        elif path == "/icon.svg":
            self._send(ICON_SVG.encode(), "image/svg+xml")
        elif path == "/sw.js":
            self._send(SERVICE_WORKER.encode(), "text/javascript")
        elif path == "/api/token":
            self._json(_mint_token())
        elif path == "/api/wake":
            self._json({"ts": _WAKE["ts"]})
        elif path == "/api/settings":
            from elfie.config import current_runtime_settings
            self._json(current_runtime_settings())
        elif path == "/api/voices":
            self._json(_list_voices())
        elif path == "/api/usage":
            self._json(_usage_summary())
        elif path == "/api/capabilities":
            self._json(_capabilities())
        elif path == "/api/progress":
            self._json(_progress_summary())
        elif path == "/api/reports":
            self._json(reports.list_reports())
        elif path == "/api/tasks":
            from elfie.delegate import _read_tasks
            self._json(list(reversed(_read_tasks()))[:50])
        elif path.startswith("/api/report/"):
            result = reports.get_report(path.removeprefix("/api/report/"))
            if result is None:
                self._json({"error": "not found"}, 404)
            else:
                meta, content = result
                self._json({"meta": meta, "content": content})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/wake":
            # A wake event — the open page polls /api/wake and connects on it.
            import time
            _WAKE["ts"] = time.time()
            logger.info("[Wake] wake event received")
            self._json({"ok": True, "ts": _WAKE["ts"]})
            return
        if path != "/api/settings":
            self._json({"error": "not found"}, 404)
            return
        from elfie.config import RUNTIME_SETTINGS_FILE, RUNTIME_TUNABLES, current_runtime_settings
        try:
            length = int(self.headers.get("Content-Length", 0))
            incoming = json.loads(self.rfile.read(length))
            assert isinstance(incoming, dict)
        except (ValueError, AssertionError):
            self._json({"error": "bad request"}, 400)
            return
        # Merge whitelisted keys into the settings overlay file
        try:
            saved = json.loads(RUNTIME_SETTINGS_FILE.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            saved = {}
        saved.update({k: v for k, v in incoming.items() if k in RUNTIME_TUNABLES})
        RUNTIME_SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        RUNTIME_SETTINGS_FILE.write_text(json.dumps(saved, indent=2), encoding="utf-8")
        logger.info(f"Settings updated: {list(incoming.keys())}")
        self._json(current_runtime_settings())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
    addr = (CONFIG.dashboard.host, CONFIG.dashboard.port)
    logger.info(f"Dashboard running at http://localhost:{addr[1]}")
    ThreadingHTTPServer(addr, Handler).serve_forever()


if __name__ == "__main__":
    main()
