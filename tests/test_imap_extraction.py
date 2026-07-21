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
    _extract_link,
    _from_matches,
    _hme_alias,
    _message_text,
    _primary_recipient,
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


# Real headers from an actual Sam's Club code email delivered via iCloud
# Hide My Email (sender rewritten, addressed to a HME alias, code in subject).
HME_RAW = (
    "Delivered-To: kdotcannon87@gmail.com\r\n"
    "From: Sam's Club "
    "<No-reply_at_transaction_samsclub_com_ysntf8b7kgdz64_28g48899@icloud.com>\r\n"
    "To: Hide My Email <59clawed-trade@icloud.com>\r\n"
    "X-ICLOUD-HME: p=59clawed-trade@icloud.com; d=; f=kcannonpoker@icloud.com; "
    "r=to; s=No-reply@transaction.samsclub.com\r\n"
    "Subject: Your Sam's Club verification code is 255502\r\n"
    "Date: Sun, 19 Jul 2026 13:15:05 +0000\r\n"
    "Content-Type: text/plain; charset=utf-8\r\n"
    "\r\n"
    "Your code: 255502. It expires in 15 minutes.\r\n"
)


def test_hme_sender_matches_samsclub_token():
    m = email.message_from_string(HME_RAW)
    # The literal "samsclub.com" would NOT match the rewritten sender...
    assert not _from_matches(m, ["samsclub.com"])
    # ...but the bare token does (this is why the config default is "samsclub").
    assert _from_matches(m, ["samsclub"])


def test_hme_alias_extracted():
    m = email.message_from_string(HME_RAW)
    assert _hme_alias(m) == "59clawed-trade@icloud.com"
    assert _primary_recipient(m) == "59clawed-trade@icloud.com"


def test_hme_to_matches_alias():
    m = email.message_from_string(HME_RAW)
    assert _to_matches(m, "59clawed-trade@icloud.com")
    assert not _to_matches(m, "someone-else@icloud.com")


# Shape of the real "Welcome! Time to set up your account." activation email:
# a logo link FIRST, then the register link, plus the membership number inline.
WELCOME_BODY = """
<a href="https://click.em.samsclub.com/?qs=LOGO123"><img alt="Sam's Club"></a>
<td>Copy your membership number: 10142210501657745</td>
<td><a href="https://click.em.samsclub.com/?qs=REGISTER456">Register your membership</a></td>
<a href="https://click.em.samsclub.com/?qs=BTN789">Register Now</a>
"""


def test_extract_link_prefers_register_anchor_over_logo():
    # Without a text hint, we'd grab the logo link (first URL); with the hint we
    # grab the real "Register your membership" button.
    assert _extract_link(WELCOME_BODY, LINK_RE, "").endswith("LOGO123")
    assert _extract_link(WELCOME_BODY, LINK_RE, "register").endswith("REGISTER456")


def test_membership_number_regex_from_email_body():
    num_re = re.compile(r"(?i)membership number:?\s*(\d{8,})")
    assert num_re.search(WELCOME_BODY).group(1) == "10142210501657745"


def test_hme_code_from_subject_and_body():
    m = email.message_from_string(HME_RAW)
    assert CODE_RE.search(m.get("Subject", "")).group(1) == "255502"
    assert CODE_RE.search(_message_text(m)).group(1) == "255502"


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
