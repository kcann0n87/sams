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
    # Complimentary-membership form needs name + email (+ optional phone), no address
    assert a.primary_email and a.primary_password
    assert a.secondary_first and a.secondary_last and a.secondary_email
    assert a.phone  # example rows include a phone
    assert a.label == f"{a.primary_email} -> {a.secondary_email}"


def test_accounts_load_without_address_columns():
    # A CSV with only the required columns (no address at all) must load fine.
    import io

    from sams_automation import config as cfgmod

    csv_text = (
        "primary_email,primary_password,secondary_first,secondary_last,secondary_email\n"
        "p@x.com,pw,Jane,Doe,alias@icloud.com\n"
    )
    tmp = ROOT / "tests" / "_tmp_accounts.csv"
    tmp.write_text(csv_text)
    try:
        accts = cfgmod.load_accounts(tmp)
        assert len(accts) == 1 and accts[0].address1 == "" and accts[0].zip == ""
    finally:
        tmp.unlink()


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
