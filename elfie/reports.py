"""
Elfie report store — where long-form answers live.

Voice is for short answers. Anything technical or detailed becomes a
markdown report saved here, viewable in the dashboard (elfie/dashboard.py)
and linked in the room chat / Telegram.

Layout:
  data/reports/index.json   — metadata for every report (newest last)
  data/reports/<id>.md      — the report content
"""
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("elfie.reports")

REPORTS_DIR = Path(__file__).parent.parent / "data" / "reports"
INDEX_FILE = REPORTS_DIR / "index.json"


def _slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len] or "report"


def _read_index() -> list[dict]:
    try:
        return json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _write_index(index: list[dict]) -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_FILE.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")


def create_report(
    title: str,
    content: str,
    source: str = "elfie",
    task_id: str | None = None,
    files: list[str] | None = None,
) -> dict:
    """
    Save a markdown report and return its metadata.

    source: "elfie" (quick explainer), "claude-code" (delegated task), etc.
    files:  paths to extra artifacts the task produced (kept in the task workspace).
    """
    now = datetime.now(timezone.utc)
    report_id = f"{now.strftime('%Y%m%d-%H%M%S')}-{_slugify(title)}"

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    (REPORTS_DIR / f"{report_id}.md").write_text(content, encoding="utf-8")

    meta = {
        "id": report_id,
        "title": title,
        "source": source,
        "task_id": task_id,
        "files": files or [],
        "created": now.isoformat(),
        "words": len(content.split()),
    }
    index = _read_index()
    index.append(meta)
    _write_index(index)
    logger.info(f"[Reports] Saved {report_id!r} ({meta['words']} words)")
    return meta


def list_reports(limit: int = 50) -> list[dict]:
    """Newest first."""
    return list(reversed(_read_index()))[:limit]


def get_report(report_id: str) -> tuple[dict, str] | None:
    """Return (metadata, markdown content) or None."""
    # Sanitize — ids are filenames
    if not re.fullmatch(r"[a-z0-9-]+", report_id):
        return None
    meta = next((m for m in _read_index() if m["id"] == report_id), None)
    if meta is None:
        return None
    try:
        content = (REPORTS_DIR / f"{report_id}.md").read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    return meta, content


def report_path(report_id: str) -> Path:
    return REPORTS_DIR / f"{report_id}.md"


def report_url(report_id: str) -> str:
    """Dashboard link for a report — what we put in chat messages."""
    from elfie.config import CONFIG
    return f"http://localhost:{CONFIG.dashboard.port}/report/{report_id}"
