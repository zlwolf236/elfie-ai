"""
Resilience / edge-case tests (OVERNIGHT_PLAN section B).

These exercise the dashboard's behaviour when its data files are missing,
empty, or corrupt — the cases where a single bad line should degrade, never
500. Pure helpers are called directly with patched module paths so no real
data/ files are written and no network is touched.

Stdlib unittest only (the shared venv has no pytest):
    python -m unittest tests.test_resilience -v
"""
import json
import tempfile
import threading
import time
import unittest
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from elfie import dashboard, reports


def _seed_no_network():
    # Pre-seed the Deepgram cache so _usage_summary never calls out.
    dashboard._deepgram_cache.update(at=time.time(), value=None)


@contextmanager
def _tool_usage(content: str | None):
    """Point dashboard.TOOL_USAGE_FILE at a temp file (or a missing path)."""
    original = dashboard.TOOL_USAGE_FILE
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "tool_usage.jsonl"
        if content is not None:
            path.write_text(content, encoding="utf-8")
        dashboard.TOOL_USAGE_FILE = path
        try:
            yield path
        finally:
            dashboard.TOOL_USAGE_FILE = original


@contextmanager
def _sessions(content: str | None):
    """Point reports.REPORTS_DIR so its sibling sessions.jsonl is our temp file."""
    original = reports.REPORTS_DIR
    with tempfile.TemporaryDirectory() as d:
        if content is not None:
            (Path(d) / "sessions.jsonl").write_text(content, encoding="utf-8")
        reports.REPORTS_DIR = Path(d) / "reports"
        try:
            yield
        finally:
            reports.REPORTS_DIR = original


# ── B: tool_usage.jsonl missing / empty / corrupt ─────────────────────────────

class ToolUsageResilience(unittest.TestCase):
    def test_missing_file_yields_empty_last_used(self):
        with _tool_usage(None):
            caps = dashboard._capabilities()
        self.assertTrue(all(c["last_used"] is None for c in caps))

    def test_empty_file_yields_empty_last_used(self):
        with _tool_usage(""):
            caps = dashboard._capabilities()
        self.assertTrue(all(c["last_used"] is None for c in caps))

    def test_corrupt_line_does_not_raise(self):
        good = json.dumps({"tool": "search_web", "ts": "2026-06-22T10:00:00+00:00"})
        with _tool_usage("{not json\n" + good + "\n"):
            # Must not raise; the well-formed entry should still be readable
            # (per-line tolerance, not abort-on-first-bad-line).
            last = dashboard._tool_last_used()
        self.assertEqual(last.get("search_web"), "2026-06-22T10:00:00+00:00")


# ── B: skills/ directory missing ──────────────────────────────────────────────

class SkillsDirResilience(unittest.TestCase):
    def test_missing_skills_dir_zero_learned(self):
        original = dashboard.SKILLS_DIR
        dashboard.SKILLS_DIR = Path(tempfile.gettempdir()) / "elfie-no-such-skills-dir"
        try:
            self.assertEqual(dashboard._learned_skills(), [])
            caps = dashboard._capabilities()
        finally:
            dashboard.SKILLS_DIR = original
        self.assertFalse(any(c["learned"] for c in caps))


# ── B: sessions.jsonl missing / corrupt → usage + progress stay alive ─────────

class SessionsResilience(unittest.TestCase):
    def setUp(self):
        _seed_no_network()

    def test_missing_sessions_usage_ok(self):
        with _sessions(None):
            u = dashboard._usage_summary()
            p = dashboard._progress_summary()
        self.assertEqual(u["total_usd"], 0)
        self.assertEqual(u["session_count"], 0)
        self.assertEqual(p["spent_total"], 0)

    def test_corrupt_line_in_sessions_does_not_raise(self):
        good = json.dumps({"timestamp": "2026-06-22T10:00:00+00:00", "cost_usd": 0.5})
        with _sessions(good + "\n{corrupt not json\n"):
            u = dashboard._usage_summary()      # must not raise
            p = dashboard._progress_summary()   # calls _usage_summary internally
        # The one well-formed session must still be counted.
        self.assertEqual(u["total_usd"], 0.5)
        self.assertEqual(u["session_count"], 1)
        self.assertEqual(p["spent_total"], 0.5)


# ── B: _progress_summary degrades when _read_tasks throws ─────────────────────

class ProgressDegrades(unittest.TestCase):
    def setUp(self):
        _seed_no_network()

    def test_progress_survives_read_tasks_error(self):
        import elfie.delegate as delegate
        original = delegate._read_tasks

        def boom():
            raise RuntimeError("tasks store unreadable")

        delegate._read_tasks = boom
        try:
            with _sessions(None):
                p = dashboard._progress_summary()
        finally:
            delegate._read_tasks = original
        self.assertEqual(p["tasks_total"], 0)
        self.assertEqual(p["tasks_done"], 0)


# ── B: concurrent /api/capabilities requests don't interleave-corrupt ─────────

class Concurrency(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _seed_no_network()
        from http.server import ThreadingHTTPServer
        cls.port = 8801  # never 8765
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", cls.port), dashboard.Handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_parallel_capabilities_requests_all_valid(self):
        url = f"http://127.0.0.1:{self.port}/api/capabilities"
        results = {}

        def hit(i):
            with urllib.request.urlopen(url, timeout=10) as r:
                results[i] = json.loads(r.read())

        threads = [threading.Thread(target=hit, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 12)
        first = results[0]
        for caps in results.values():
            self.assertIsInstance(caps, list)
            self.assertEqual([c["key"] for c in caps], [c["key"] for c in first])


if __name__ == "__main__":
    unittest.main(verbosity=2)
