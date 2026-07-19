"""Offline test that the example config + accounts load and validate.

Run with:  python tests/test_config.py   (or)   python -m pytest
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sams_automation.config import load_accounts, load_config  # noqa: E402


def test_example_config_loads():
    c = load_config(ROOT / "config.example.yaml")
    # iCloud IMAP defaults
    assert c.imap.host == "imap.mail.me.com"
    assert c.imap.port == 993 and c.imap.ssl is True
    # verification
    assert c.verification.mode in ("code", "link")
    # real-Chrome / stealth fields wired through
    assert c.browser.channel == "chrome"
    assert c.browser.user_data_dir == "chrome-profile"
    assert c.browser.stealth is True
    # sams urls
    assert c.sams.login_url.startswith("http")
    assert c.sams.logout_url.startswith("http")
    # proxies section
    assert c.proxies.rotation in ("sticky", "round_robin", "random")


def test_example_accounts_load():
    accts = load_accounts(ROOT / "accounts.example.csv")
    assert len(accts) == 2
    a = accts[0]
    assert a.primary_email and a.secondary_email
    assert a.address1 and a.city and a.state and a.zip
    assert a.label == f"{a.primary_email} -> {a.secondary_email}"


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
