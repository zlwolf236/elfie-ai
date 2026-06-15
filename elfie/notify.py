"""
Elfie delivery layer — how reports and updates reach the user.

Three channels, used together:

  1. Voice    — one spoken sentence ("Your report on X is ready").
  2. Room chat — a text message with the report title + dashboard link,
                 visible in the chat panel of the LiveKit room page.
  3. Telegram — optional; if TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID are set,
                the report markdown is sent as a file to your phone.

The agent registers itself as the "active" notifier while a session is
live; background tasks that finish between sessions still reach Telegram.
"""
import logging
import shutil
import subprocess
from pathlib import Path

import httpx

from elfie.config import CONFIG
from elfie.reports import report_path, report_url

logger = logging.getLogger("elfie.notify")


def open_in_browser(url: str) -> bool:
    """
    Open a URL in the user's default browser. On WSL2 this reaches the
    Windows browser via explorer.exe — no Playwright/Chromium needed.
    """
    for cmd in (["wslview", url], ["explorer.exe", url], ["xdg-open", url]):
        if shutil.which(cmd[0]):
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                logger.info(f"[Notify] Opened in browser: {url}")
                return True
            except OSError:
                continue
    logger.warning("[Notify] No browser opener available")
    return False


class Notifier:
    """Bound to one live session's room. Use module functions to find the active one."""

    def __init__(self, room=None, session=None) -> None:
        self._room = room
        self._session = session

    async def send_chat(self, text: str) -> None:
        """Post a text message into the room's chat panel."""
        if self._room is None:
            return
        try:
            await self._room.local_participant.send_text(text, topic="lk.chat")
        except Exception as e:
            logger.warning(f"[Notify] Chat send failed: {e}")

    async def speak(self, text: str) -> None:
        """Say one sentence — used to announce a finished report mid-session."""
        if self._session is None:
            return
        try:
            await self._session.say(text, allow_interruptions=True)
        except Exception as e:
            logger.warning(f"[Notify] Speak failed: {e}")

    async def announce_report(self, report_meta: dict, spoken_line: str) -> None:
        """Deliver a finished report on every available channel."""
        url = report_url(report_meta["id"])
        await self.send_chat(f"📄 {report_meta['title']}\n{url}")
        await send_telegram_report(report_meta)
        if CONFIG.dashboard.auto_open_reports:
            open_in_browser(url)
        await self.speak(spoken_line)


# ── Active-notifier registry ─────────────────────────────────────────────────

_active: Notifier | None = None


def set_active(notifier: Notifier) -> None:
    global _active
    _active = notifier


def clear_active() -> None:
    global _active
    _active = None


def get_active() -> Notifier | None:
    return _active


# ── Telegram (optional) ──────────────────────────────────────────────────────

async def send_telegram_report(report_meta: dict) -> None:
    """Send the report file to Telegram, if configured. Silently skips otherwise."""
    token, chat_id = CONFIG.telegram_bot_token, CONFIG.telegram_chat_id
    if not token or not chat_id:
        return

    path: Path = report_path(report_meta["id"])
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendDocument",
                data={
                    "chat_id": chat_id,
                    "caption": f"📄 {report_meta['title']}",
                },
                files={"document": (f"{report_meta['id']}.md", path.read_bytes())},
            )
        logger.info(f"[Notify] Report {report_meta['id']} sent to Telegram")
    except Exception as e:
        logger.warning(f"[Notify] Telegram send failed: {e}")
