"""Offline tests for verification extraction + message matching.

These don't touch the network — they exercise the pure parsing helpers so you
can trust the code/link extraction before pointing it at the live mailbox.

Run with:  python -m pytest   (or)   python tests/test_imap_extraction.py
"""

from __future__ import annotations

import email
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sams_automation.imap_client import (  # noqa: E402
    _from_matches,
    _message_text,
    _received_time,
    _to_matches,
)

CODE_RE = re.compile(r"\b(\d{6})\b")
LINK_RE = re.compile(r"(https?://[^\s\"'<>]*samsclub\.com[^\s\"'<>]*)")


def _msg(to: str, frm: str, body: str, subject: str = "Verify your email") -> email.message.Message:
    raw = (
        f"From: {frm}\r\n"
        f"To: {to}\r\n"
        f"Subject: {subject}\r\n"
        f"Date: Tue, 15 Jul 2025 10:00:00 +0000\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n"
        f"\r\n"
        f"{body}\r\n"
    )
    return email.message_from_string(raw)


def test_code_extraction():
    m = _msg("jane@d.com", "no-reply@samsclub.com", "Your code is 483920. Expires soon.")
    text = _message_text(m)
    assert CODE_RE.search(text).group(1) == "483920"


def test_link_extraction():
    body = "Activate here: https://www.samsclub.com/activate?token=abc123 thanks"
    m = _msg("jane@d.com", "no-reply@samsclub.com", body)
    text = _message_text(m)
    assert LINK_RE.search(text).group(1) == "https://www.samsclub.com/activate?token=abc123"


def test_to_matches_various_headers():
    m = _msg("Jane Doe <jane.doe@d.com>", "no-reply@samsclub.com", "code 111111")
    assert _to_matches(m, "jane.doe@d.com")
    assert not _to_matches(m, "someone.else@d.com")


def test_from_matches():
    m = _msg("jane@d.com", "Sam's Club <no-reply@samsclub.com>", "code 222222")
    assert _from_matches(m, ["samsclub.com"])
    assert not _from_matches(m, ["costco.com"])


def test_received_time_parsed_utc():
    m = _msg("jane@d.com", "no-reply@samsclub.com", "code 333333")
    dt = _received_time(m)
    assert dt.tzinfo is not None
    assert dt == datetime(2025, 7, 15, 10, 0, 0, tzinfo=timezone.utc)


def test_html_body_code():
    raw = (
        "From: no-reply@samsclub.com\r\n"
        "To: jane@d.com\r\n"
        "Subject: Verify\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        "\r\n"
        "<html><body><p>Code: <b>654321</b></p></body></html>\r\n"
    )
    m = email.message_from_string(raw)
    text = _message_text(m)
    assert CODE_RE.search(text).group(1) == "654321"


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
