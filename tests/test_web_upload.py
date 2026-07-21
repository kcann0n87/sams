"""Offline tests for the web UI's CSV upload / template / results endpoints.

Uses Flask's test client — no real browser or network. Skips cleanly if Flask
isn't installed so `python tests/test_web_upload.py` still works in a bare env.

Run with:  python tests/test_web_upload.py   (or)   python -m pytest
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

try:
    import flask  # noqa: F401

    HAVE_FLASK = True
except Exception:  # pragma: no cover - env without flask
    HAVE_FLASK = False

GOOD_CSV = (
    b"primary_email,primary_password,secondary_first,secondary_last,"
    b"secondary_email,phone,secondary_password\n"
    b"dad@example.com,pw1,Jane,Doe,jane@my.com,3125550101,newpw1\n"
    b"mom@example.com,pw2,John,Smith,john@my.com,,\n"
)


def _client(workdir: Path):
    """A Flask test client whose cwd is an isolated temp dir with a config."""
    from sams_automation.web import create_app

    (workdir / "config.yaml").write_text(
        (ROOT / "config.example.yaml").read_text()
    )
    os.chdir(workdir)
    return create_app("config.yaml", "accounts.csv").test_client()


def _in_tempdir(fn):
    if not HAVE_FLASK:
        print("SKIP (flask not installed)")
        return
    prev = os.getcwd()
    with tempfile.TemporaryDirectory() as d:
        try:
            fn(_client(Path(d)))
        finally:
            os.chdir(prev)


def test_template_download():
    def body(c):
        r = c.get("/api/template.csv")
        assert r.status_code == 200
        header = r.data.decode().splitlines()[0]
        assert header.startswith("primary_email,primary_password")
        assert "secondary_password" in header

    _in_tempdir(body)


def test_upload_valid_csv_masks_and_saves():
    def body(c):
        r = c.post(
            "/api/upload-csv",
            data={"file": (io.BytesIO(GOOD_CSV), "accounts.csv")},
            content_type="multipart/form-data",
        )
        j = r.get_json()
        assert j["ok"] is True
        assert j["count"] == 2
        # passwords never echoed back in the clear
        assert j["rows"][0]["primary_password"] == "••••••"
        assert j["rows"][0]["secondary_password"] == "••••••"
        # row with a missing secondary_password is flagged, not rejected
        assert j["warning"]
        # the list is now the active accounts.csv
        assert Path("accounts.csv").exists()
        assert c.get("/api/accounts").get_json()["count"] == 2

    _in_tempdir(body)


def test_upload_missing_columns_rejected_cleanly():
    def body(c):
        bad = b"primary_email,primary_password\nx@y.com,pw\n"
        r = c.post(
            "/api/upload-csv",
            data={"file": (io.BytesIO(bad), "bad.csv")},
            content_type="multipart/form-data",
        )
        j = r.get_json()
        assert j["ok"] is False
        assert "missing required column" in j["error"]
        # error must not leak the internal temp path
        assert tempfile.gettempdir() not in j["error"]
        assert "bad.csv" in j["error"]
        # a rejected upload must not create/replace accounts.csv
        assert not Path("accounts.csv").exists()

    _in_tempdir(body)


def test_upload_header_only_rejected():
    def body(c):
        empty = (
            b"primary_email,primary_password,secondary_first,secondary_last,"
            b"secondary_email\n"
        )
        r = c.post(
            "/api/upload-csv",
            data={"file": (io.BytesIO(empty), "e.csv")},
            content_type="multipart/form-data",
        )
        assert r.get_json()["ok"] is False

    _in_tempdir(body)


def test_upload_overwrite_backs_up_previous():
    def body(c):
        for _ in range(2):
            c.post(
                "/api/upload-csv",
                data={"file": (io.BytesIO(GOOD_CSV), "a.csv")},
                content_type="multipart/form-data",
            )
        assert Path("accounts.csv.bak").exists()

    _in_tempdir(body)


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
