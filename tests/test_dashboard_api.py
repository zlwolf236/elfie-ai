"""
Dashboard API + capability/usage-logging tests (OVERNIGHT_PLAN sections A & C).

Boots elfie.dashboard.Handler on a NON-DEFAULT port (8799) in a thread and hits
it over HTTP, plus calls the pure helpers directly with patched paths so no real
data/ files are written and no network is touched.

Stdlib unittest only (the shared venv has no pytest):
    python -m unittest tests.test_dashboard_api -v
    python tests/test_dashboard_api.py
"""
import ast
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from elfie import dashboard

TEST_PORT = 8799  # never 8765 (the live dashboard port)
BASE = f"http://127.0.0.1:{TEST_PORT}"
TOOLS_PY = Path(dashboard.__file__).parent / "tools.py"

_httpd = None


def setUpModule():
    # Pre-seed the Deepgram cache so /api/progress + /api/usage never call out.
    dashboard._deepgram_cache.update(at=time.time(), value=None)
    global _httpd
    _httpd = ThreadingHTTPServer(("127.0.0.1", TEST_PORT), dashboard.Handler)
    threading.Thread(target=_httpd.serve_forever, daemon=True).start()


def tearDownModule():
    if _httpd:
        _httpd.shutdown()
        _httpd.server_close()


def _get(path):
    """Return (status, body-bytes, content-type). 4xx/5xx don't raise."""
    try:
        r = urllib.request.urlopen(BASE + path, timeout=10)
        return r.status, r.read(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("Content-Type", "")


def _get_json(path):
    status, body, ctype = _get(path)
    return status, json.loads(body), ctype


CAP_KEYS = {"key", "name", "desc", "icon", "group", "ids",
            "last_used", "learned", "locked", "is_new"}


def _record_use_names():
    """Every literal name passed to record_use(...) in tools.py, via AST."""
    tree = ast.parse(TOOLS_PY.read_text(encoding="utf-8"))
    names = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "record_use"
                and node.args
                and isinstance(node.args[0], ast.Constant)):
            names.append(node.args[0].value)
    return names


def _capability_ids():
    ids = set()
    for c in dashboard.CAPABILITIES:
        ids.update(c["ids"])
    return ids


# ── Section A: page routes ────────────────────────────────────────────────────

class PageRoutes(unittest.TestCase):
    def test_page_routes_serve_html_200(self):
        for path in ("/", "/talk", "/report/anything"):
            with self.subTest(path=path):
                status, body, ctype = _get(path)
                self.assertEqual(status, 200)
                self.assertIn("text/html", ctype)
                self.assertIn(b"<!DOCTYPE html>", body)

    def test_unknown_path_returns_json_404_not_crash(self):
        status, data, ctype = _get_json("/does-not-exist")
        self.assertEqual(status, 404)
        self.assertIn("application/json", ctype)
        self.assertEqual(data, {"error": "not found"})

    def test_unknown_api_report_returns_json_404(self):
        status, data, _ = _get_json("/api/report/no-such-report-id")
        self.assertEqual(status, 404)
        self.assertEqual(data, {"error": "not found"})


# ── Section A: /api/capabilities ──────────────────────────────────────────────

class Capabilities(unittest.TestCase):
    def test_capabilities_endpoint_shape_and_counts(self):
        status, caps, ctype = _get_json("/api/capabilities")
        self.assertEqual(status, 200)
        self.assertIn("application/json", ctype)
        self.assertIsInstance(caps, list)

        builtins = [c for c in caps if not c["learned"] and not c["locked"]]
        learned = [c for c in caps if c["learned"]]
        locked = [c for c in caps if c["locked"]]

        self.assertEqual(len(builtins), 12, "expected 12 built-in capabilities")
        self.assertEqual(len(locked), len(dashboard.LOCKED_NODES))
        self.assertEqual(len(locked), 3)

        skill_files = {p.stem for p in dashboard.SKILLS_DIR.glob("*.py") if p.stem != "__init__"}
        self.assertEqual({c["key"] for c in learned}, skill_files)

        for c in caps:
            self.assertLessEqual(CAP_KEYS, set(c), f"missing keys on {c['key']}")

    def test_last_used_reflects_usage_log(self):
        with tempfile.TemporaryDirectory() as d:
            usage = Path(d) / "tool_usage.jsonl"
            ts = "2026-06-22T10:00:00+00:00"
            usage.write_text(json.dumps({"tool": "search_web", "ts": ts}) + "\n", encoding="utf-8")
            original = dashboard.TOOL_USAGE_FILE
            dashboard.TOOL_USAGE_FILE = usage
            try:
                caps = {c["key"]: c for c in dashboard._capabilities()}
            finally:
                dashboard.TOOL_USAGE_FILE = original
            self.assertEqual(caps["search_web"]["last_used"], ts)
            self.assertIsNone(caps["time"]["last_used"])


# ── Section A: /api/progress matches the underlying files ──────────────────────

class Progress(unittest.TestCase):
    def test_progress_matches_independent_computation(self):
        from elfie import reports
        from elfie.delegate import _read_tasks

        status, prog, _ = _get_json("/api/progress")
        self.assertEqual(status, 200)

        self.assertEqual(prog["reports"], len(reports.list_reports()))
        tasks = _read_tasks()
        self.assertEqual(prog["tasks_done"], sum(1 for t in tasks if t.get("status") == "done"))
        self.assertEqual(prog["tasks_total"], len(tasks))

        skill_files = [p for p in dashboard.SKILLS_DIR.glob("*.py") if p.stem != "__init__"]
        self.assertEqual(prog["skills_learned"], len(skill_files))
        self.assertEqual(
            prog["skills_total"],
            len(dashboard.CAPABILITIES) + len(skill_files) + len(dashboard.LOCKED_NODES),
        )
        self.assertEqual(
            prog["skills_unlocked"], len(dashboard.CAPABILITIES) + len(skill_files)
        )

        sessions_path = reports.REPORTS_DIR.parent / "sessions.jsonl"
        total = 0.0
        if sessions_path.exists():
            for line in sessions_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    total += json.loads(line).get("cost_usd", 0)
        self.assertEqual(prog["spent_total"], round(total, 4))


# ── Section A: regression shapes for the other endpoints ──────────────────────

class RegressionShapes(unittest.TestCase):
    def test_reports_endpoint_shape(self):
        status, items, _ = _get_json("/api/reports")
        self.assertEqual(status, 200)
        self.assertIsInstance(items, list)
        if items:
            self.assertLessEqual({"id", "title"}, set(items[0]))

    def test_tasks_endpoint_shape(self):
        status, items, _ = _get_json("/api/tasks")
        self.assertEqual(status, 200)
        self.assertIsInstance(items, list)

    def test_settings_endpoint_shape(self):
        from elfie.config import RUNTIME_TUNABLES
        status, s, _ = _get_json("/api/settings")
        self.assertEqual(status, 200)
        self.assertEqual(set(s), set(RUNTIME_TUNABLES))


# ── Section C: tool usage logging matches the capability catalogue ─────────────

class ToolUsageLogging(unittest.TestCase):
    def test_record_use_names_match_capability_ids(self):
        recorded = _record_use_names()
        self.assertEqual(len(recorded), len(set(recorded)), "duplicate record_use names")
        self.assertEqual(set(recorded), _capability_ids())

    def test_record_use_is_silent_on_failure(self):
        from elfie import tools
        with tempfile.TemporaryDirectory() as d:
            blocker = Path(d) / "blocker"
            blocker.write_text("x")  # a file where a dir is expected
            original = tools.TOOL_USAGE_FILE
            tools.TOOL_USAGE_FILE = blocker / "nested" / "tool_usage.jsonl"
            try:
                tools.record_use("search_web")  # must not raise
            finally:
                tools.TOOL_USAGE_FILE = original

    def test_elfie_tools_expose_function_tools(self):
        import inspect

        from livekit.agents import FunctionTool

        from elfie.tools import ElfieTools
        attrs = [getattr(ElfieTools, n) for n in dir(ElfieTools) if not n.startswith("_")]
        tools = [t for t in attrs if isinstance(t, FunctionTool)]
        names = {t.info.name for t in tools}
        self.assertLessEqual(_capability_ids(), names, f"missing tools: {_capability_ids() - names}")

        # Introspection still works: signatures expose their declared params,
        # so the inserted record_use line didn't corrupt the @function_tool wiring.
        by_name = {t.info.name: t for t in tools}
        params = set(inspect.signature(by_name["search_web"]._func).parameters)
        self.assertIn("query", params)
        self.assertIn("context", params)


if __name__ == "__main__":
    unittest.main(verbosity=2)
