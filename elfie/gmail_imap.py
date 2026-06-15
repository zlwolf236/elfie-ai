"""Check unread Gmail via IMAP + an app password.

Simplest approach — stdlib only, no Google Cloud project needed.

Setup (one time):
  1. Enable 2-Step Verification on the Google account.
  2. Create an app password at https://myaccount.google.com/apppasswords
  3. Set env vars: GMAIL_ADDRESS, GMAIL_APP_PASSWORD

Usage:
  python gmail_unread_imap.py            # count + latest 5 unread subjects
"""

import email
import email.header
import imaplib
import os

IMAP_HOST = "imap.gmail.com"


def _decode(value: str) -> str:
    """Decode RFC 2047 encoded headers like =?UTF-8?B?...?=."""
    parts = email.header.decode_header(value or "")
    return "".join(
        p.decode(enc or "utf-8", errors="replace") if isinstance(p, bytes) else p
        for p, enc in parts
    )


def check_unread(address: str, app_password: str, limit: int = 5) -> dict:
    """Return {'count': int, 'messages': [{'from','subject','date'}, ...]}."""
    with imaplib.IMAP4_SSL(IMAP_HOST) as imap:
        imap.login(address, app_password)
        # readonly=True so peeking does not mark anything as read
        imap.select("INBOX", readonly=True)

        status, data = imap.search(None, "UNSEEN")
        if status != "OK":
            raise RuntimeError(f"IMAP search failed: {status}")
        ids = data[0].split()

        messages = []
        # newest first; headers only (BODY.PEEK keeps the unread flag intact)
        for msg_id in reversed(ids[-limit:]):
            status, msg_data = imap.fetch(
                msg_id, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])"
            )
            if status != "OK" or not msg_data:
                continue
            first = msg_data[0]
            if not (isinstance(first, tuple) and isinstance(first[1], bytes)):
                continue
            headers = email.message_from_bytes(first[1])
            messages.append(
                {
                    "from": _decode(headers.get("From", "")),
                    "subject": _decode(headers.get("Subject", "(no subject)")),
                    "date": headers.get("Date", ""),
                }
            )

        return {"count": len(ids), "messages": messages}


if __name__ == "__main__":
    address = os.environ["GMAIL_ADDRESS"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    result = check_unread(address, password)
    print(f"{result['count']} unread message(s) in INBOX")
    for m in result["messages"]:
        print(f"  - {m['subject']}  — {m['from']}")
