"""
XSS audit for the dashboard page (OVERNIGHT_PLAN section D).

The dashboard injects report titles, task titles, learned-skill names (derived
from filenames) and session transcripts into the DOM via innerHTML. All of these
are attacker-influenceable, so a value like `<img src=x onerror=alert(1)>` would
execute. This module:

  1. Reproduces that the raw payload reaches the client (data layer never
     escapes — the precondition for the XSS).
  2. Behaviourally proves the JS `esc()` helper neutralises the payload, by
     extracting it from the PAGE template and running it under Node.
  3. Asserts the vulnerable innerHTML interpolations are now wrapped in esc(...),
     so the helper is actually applied at every flagged sink.

Stdlib unittest only (the shared venv has no pytest). Node is used for the
behavioural check; if Node is absent that one test is skipped, not failed.

    python -m unittest tests.test_xss -v
"""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from elfie import dashboard

PAGE = dashboard.PAGE
PAYLOAD = '<img src=x onerror=alert(1)>'


def _extract_esc() -> str:
    """Pull the `function esc(s) { ... }` definition out of the PAGE template."""
    start = PAGE.index("function esc(")
    # Walk braces from the first '{' to find the matching close.
    i = PAGE.index("{", start)
    depth = 0
    for j in range(i, len(PAGE)):
        if PAGE[j] == "{":
            depth += 1
        elif PAGE[j] == "}":
            depth -= 1
            if depth == 0:
                return PAGE[start:j + 1]
    raise AssertionError("esc() definition not found / unbalanced braces")


class XssDataLayerRepro(unittest.TestCase):
    """The malicious payload reaches the JSON the page consumes (vuln reachable)."""

    def test_report_title_payload_survives_to_api(self):
        from elfie import reports
        with tempfile.TemporaryDirectory() as d:
            orig = reports.REPORTS_DIR
            orig_index = reports.INDEX_FILE
            reports.REPORTS_DIR = Path(d) / "reports"
            reports.INDEX_FILE = reports.REPORTS_DIR / "index.json"
            try:
                reports.create_report(title=PAYLOAD, content="body")
                listed = reports.list_reports()
            finally:
                reports.REPORTS_DIR = orig
                reports.INDEX_FILE = orig_index
        # The store keeps the title verbatim — escaping is the page's job.
        self.assertEqual(listed[0]["title"], PAYLOAD)


class EscBehaviour(unittest.TestCase):
    """Run the real esc() under Node and confirm it neutralises the payload."""

    def test_esc_escapes_html_metacharacters(self):
        node = shutil.which("node") or shutil.which("nodejs")
        if not node:
            self.skipTest("node not available for behavioural JS check")
        esc_src = _extract_esc()
        script = (
            esc_src
            + "\nconst cases = "
            + json.dumps([PAYLOAD, '"\'&<>', 'plain text', "skill</span><script>x"])
            + ";\nconsole.log(JSON.stringify(cases.map(esc)));\n"
        )
        out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=20)
        self.assertEqual(out.returncode, 0, out.stderr)
        escaped = json.loads(out.stdout.strip())
        # Payload fully defanged: no live '<' or '>' survive.
        self.assertNotIn("<", escaped[0])
        self.assertNotIn(">", escaped[0])
        self.assertEqual(escaped[0], "&lt;img src=x onerror=alert(1)&gt;")
        self.assertEqual(escaped[1], "&quot;&#39;&amp;&lt;&gt;")
        self.assertEqual(escaped[2], "plain text")          # plain text untouched
        self.assertNotIn("<script>", escaped[3])


class EscApplied(unittest.TestCase):
    """Every flagged innerHTML sink now wraps its data-derived value in esc()."""

    def _assert_wrapped(self, substr, field):
        self.assertIn(substr, PAGE, f"expected esc-wrapped sink for {field}: {substr!r}")

    def test_report_title_and_source_escaped(self):
        self._assert_wrapped("${esc(r.title)}", "report title")
        self._assert_wrapped("${esc(r.source)}", "report source")

    def test_task_fields_escaped(self):
        self._assert_wrapped("${esc(t.title)}", "task title")
        self._assert_wrapped("${esc(t.kind)}", "task kind")
        self._assert_wrapped("esc(t.error)", "task error")

    def test_capability_and_skill_names_escaped(self):
        self._assert_wrapped("${esc(c.name)}", "capability/skill name")

    def test_transcripts_escaped(self):
        self._assert_wrapped("${esc(t.user)}", "transcript user line")
        self._assert_wrapped("${esc(t.elfie)}", "transcript elfie line")

    def test_no_raw_title_interpolation_remains(self):
        # Guard against regressions: the exact raw sinks must be gone.
        for raw in ("${r.title}", "${t.title}", "${t.user}", "${t.elfie}"):
            self.assertNotIn(raw, PAGE, f"raw unescaped sink still present: {raw}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
