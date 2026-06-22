# Elfie — Overnight Test & Audit Plan

You are an autonomous agent running overnight via `gnhf`. Your job is to
**test, audit, and harden** the Elfie dashboard revamp that was just shipped
(reactive voice orb + Mission Control capability grid + skill tree, wired to
new `/api/capabilities` and `/api/progress` endpoints, plus per-tool usage
logging in `elfie/tools.py`). Then write a clear morning report.

## Operating rules (READ FIRST)

1. **Never push. Never touch `main`'s live services.** You are in an isolated
   git worktree. Do not run `git push`. Do not `systemctl restart` the live
   `elfie-agent` / `elfie-dashboard` / `elfie-wake` units.
2. **Test against your own instances.** Spin up the dashboard on a **non-default
   port** (e.g. 8799) from your worktree for HTTP/E2E checks. Never bind 8765.
3. **Safe fixes only.** Apply a fix ONLY if it is low-risk, you added/ran a test
   that proves it, and it cannot change voice behavior. Each fix = its own commit
   with a clear message prefixed `overnight-fix:`. Anything ambiguous, risky, or
   touching the live voice loop → **do NOT change it; write it up as a finding.**
4. **Reproduce before you fix.** Start every bug from a failing check, not a
   hypothesis. If you cannot reproduce it, log it as "suspected, unverified."
5. **Secrets:** if `.env` exists in the main checkout and an integration test
   needs keys, you may copy it into the worktree to run the test, but **never
   commit `.env`** and never print key values into the report or logs.
6. Treat suspiciously-clean passes as a reason to look harder, not to celebrate.

## Environment notes (you are in an isolated git worktree)

- The **main checkout** is `/mnt/c/Users/zilan/tools/elfie-ai`. It holds `.venv`,
  `data/`, and `.env` — all gitignored, so they are **NOT in your worktree**.
- Activate dependencies from the main venv by absolute path:
  `source /mnt/c/Users/zilan/tools/elfie-ai/.venv/bin/activate` (it has every
  package). Do not pip-install into the worktree unless that venv is unusable.
- For real-data tests, symlink the main data into your worktree root so the
  package resolves it: `ln -s /mnt/c/Users/zilan/tools/elfie-ai/data data`.
  The dashboard reads `data/` relative to the `elfie/` package dir. With no
  symlink, `data/` is absent — which is exactly the missing-data edge case in
  section B, so test BOTH states deliberately.
- If an integration test needs API keys, symlink `.env` the same way:
  `ln -s /mnt/c/Users/zilan/tools/elfie-ai/.env .env`. Never commit it.
- `python -m elfie.dashboard` serves the page; override the port for tests.
- `elfie.dashboard.Handler` can be served on any port (e.g. 8799) for endpoint
  tests without the agent worker running. Never bind 8765.
- Real data sources (in the main checkout): `data/reports/`, `data/sessions.jsonl`,
  `data/tasks*`, `data/tool_usage.jsonl`, `elfie/skills/*.py`.

---

## TEST CHECKLIST — work top to bottom, log every result

### A. Endpoints & data correctness (automated — write pytest where you can)
- [ ] `/api/capabilities`: returns 12 built-ins + every `elfie/skills/*.py` (minus `__init__`) + 3 locked nodes. Shapes have all keys. `last_used` reflects `data/tool_usage.jsonl`.
- [ ] `/api/progress`: `reports`, `tasks_done`, `skills_learned`, `spent_total` match the underlying files exactly (compute independently and diff).
- [ ] `/api/reports`, `/api/tasks`, `/api/usage`, `/api/settings`, `/api/voices` still return correct shapes (regression).
- [ ] `/report/<id>` and `/` and `/talk` all serve the page (200).
- [ ] 404 path returns JSON 404, not a crash.

### B. Resilience / edge cases (this is where bugs hide)
- [ ] Empty or missing `data/tool_usage.jsonl` → `/api/capabilities` still 200, no `last_used`.
- [ ] Missing `elfie/skills/` dir → no crash, zero learned skills.
- [ ] Missing `data/sessions.jsonl` → `/api/progress` and `/api/usage` still 200.
- [ ] Corrupt line in `tool_usage.jsonl` / `sessions.jsonl` → handled, not 500.
- [ ] `_progress_summary` when `_read_tasks()` throws → degrades (already wrapped — verify).
- [ ] Concurrent requests (ThreadingHTTPServer) to `/api/capabilities` don't interleave-corrupt.

### C. Tool usage logging (`elfie/tools.py`)
- [ ] All 13 `record_use(...)` names exactly match the `ids` in `dashboard.CAPABILITIES` (a typo means a tile never lights). Cross-check programmatically.
- [ ] `record_use` failure (e.g. unwritable dir) is silent and does not break the tool.
- [ ] No `function_tool` schema/signature regression: import `ElfieTools`, confirm all tools still expose correct params (the inserted line must not have broken introspection).
- [ ] Timestamp format: `record_use` writes UTC ISO with offset; the JS `Date.now() - new Date(iso)` "recent within 30 min" comparison is timezone-correct. Verify a freshly-logged tool shows as `on`.

### D. Frontend logic & "does it confuse the user?"
- [ ] All 6 tabs (Home, Skills, Reports, Tasks, Activity, Settings) switch and render.
- [ ] Home: capability groups render in order; learned tiles labeled; progress numbers come from `/api/progress` (no hardcoded/mock numbers left anywhere — grep the PAGE for fake figures like `23`, `$1.84`, `47%`).
- [ ] Skills: Level/XP math is sane and non-misleading; locked vs unlocked vs NEW states correct; "NEW" only for skills learned <24h.
- [ ] Empty states: no reports / no tasks / no skills / no usage all read sensibly, not broken or alarming.
- [ ] No dev jargon or internal IDs leaking into user-facing copy.
- [ ] Report deep-link → Back returns to the right tab without reloading (voice session must survive). Verify `closeReport()` calls `refresh()` and nav uses `pushState`.
- [ ] **XSS audit:** report titles and capability/skill names are injected via `innerHTML`. A report titled `<img onerror>` or a skill file named with markup could execute. Determine if this is exploitable and, if so, escape it (this IS a safe fix — add it with a test).
- [ ] Responsive: confirm grids use `minmax(min(...),1fr)` and `min-width:0` so there's no horizontal overflow on a narrow (≤380px) viewport. Note any overflow.

### E. Voice loop regression (audit by reading — DO NOT change)
- [ ] `connect` / `toggle` / `scheduleReconnect` / wake-word `pollWake` logic is byte-for-byte intact vs intent (compare against the pre-revamp behavior described in `CLAUDE.md`).
- [ ] WebAudio orb degrades gracefully: if `AudioContext` construction or `makeAnalyser` fails, `orbLoop` still runs and the voice session is unaffected (trace the try/catch paths).
- [ ] Orb state thresholds (`e>0.03`, `u>0.045`, 5s thinking window) are plausible; flag if likely to misfire, but do not retune without a way to test.
- [ ] `orbLoop` runs `requestAnimationFrame` forever even when disconnected — confirm it's cheap (no leaks, analysers nulled on disconnect).

### F. What's MISSING (gap analysis — report, don't build)
- [ ] Reminders (`set_reminder`) are stored but nothing ever fires them — confirm and flag as a real gap.
- [ ] No error UI: if an `/api/*` fetch fails, the tab silently shows nothing. Worth a small "couldn't load" state? Recommend.
- [ ] Skill-tree "NEW" via file mtime: reinstalling/editing a skill resets it — note the limitation.
- [ ] No accessibility pass (contrast, keyboard, aria). Quick heuristic check; list issues.
- [ ] Anything in `CLAUDE.md` "Next things to work on" that the revamp now makes easy or that it broke.
- [ ] Does `tool_usage.jsonl` grow unbounded? Note if it needs rotation.

### G. Cross-checks
- [ ] `grep` the codebase for other importers of `elfie.dashboard` symbols — confirm nothing broke.
- [ ] `python -m py_compile` on every changed file; run any existing test suite (`pytest`) and report pass/fail.
- [ ] Confirm `.gitignore` covers `.lavish/`, `.agents/`, `.claude/`, `.serena/`, `skills-lock.json`, `data/tool_usage.jsonl` — if not, note it (do not commit those dirs).

---

## DELIVERABLE — write `MORNING_REPORT.md` at the repo root

Structure it for a 2-minute read by a busy, technical reader:

1. **TL;DR (one line):** is the revamp sound and non-confusing? Ship-as-is, or fix-first?
2. **What I tested** — checklist coverage: passed / failed / couldn't-test (and why).
3. **Bugs found** — table: severity (blocker/high/med/low) · area · what · repro · status (fixed `overnight-fix:` <sha> / left for Felix).
4. **Fixes applied** — each commit, what it changed, the test that proves it.
5. **What's missing** — prioritized gaps with a one-line recommendation each.
6. **Can't be tested headless** — the explicit list Felix must check by hand (live mic → orb reactivity, real voice barge-in, TTS, wake word).
7. **Open questions / judgment calls I deliberately did not make.**

Keep claims calibrated. "I couldn't verify X" is a valid and expected result.
When the full checklist is done and `MORNING_REPORT.md` is written, report the
condition string **MORNING_REPORT_COMPLETE** and stop.
