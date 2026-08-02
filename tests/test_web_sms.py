"""Offline tests for the SMS key store and the web page's routes.

Network is stubbed and every test runs in a temp cwd, so nothing here touches
a real provider or your real sms_keys.json.

Run with:  python tests/test_web_sms.py   (or)   python -m pytest
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sams_automation import sms_providers as sp  # noqa: E402
from sams_automation.config import (  # noqa: E402
    load_sms_settings,
    save_sms_key,
)

try:
    import flask  # noqa: F401

    HAVE_FLASK = True
except ImportError:  # pragma: no cover - depends on the env
    HAVE_FLASK = False


@contextlib.contextmanager
def workdir():
    """Run in a throwaway cwd with no provider keys leaking in from the shell."""
    saved_cwd = os.getcwd()
    cleared = [s["env"] for s in sp.KNOWN_ACTIVATE_HOSTS.values()] + [
        "TEXTVERIFIED_API_KEY", "TEXTVERIFIED_USERNAME",
        "DAISYSMS_API_KEY", "HEROSMS_API_KEY", "SMSPOOL_API_KEY",
    ]
    saved_env = {k: os.environ.pop(k, None) for k in cleared}
    tmp = tempfile.mkdtemp()
    os.chdir(tmp)
    try:
        yield Path(tmp)
    finally:
        os.chdir(saved_cwd)
        for k, v in saved_env.items():
            if v is not None:
                os.environ[k] = v


# --- key store ------------------------------------------------------------


def test_save_and_load_key_roundtrip():
    with workdir():
        save_sms_key("tiger-sms", api_key="abc123")
        settings = load_sms_settings("config.yaml")
        assert settings["tiger-sms"]["api_key"] == "abc123"


def test_save_key_is_owner_only():
    with workdir() as d:
        save_sms_key("tiger-sms", api_key="abc123")
        mode = (d / "sms_keys.json").stat().st_mode & 0o777
        # Secrets must not be group/world readable.
        assert mode == 0o600, oct(mode)


def test_partial_save_preserves_other_fields():
    with workdir():
        save_sms_key("textverified", api_key="k1", username="me@example.com")
        save_sms_key("textverified", username="new@example.com")  # key untouched
        entry = load_sms_settings("config.yaml")["textverified"]
        assert entry["api_key"] == "k1"
        assert entry["username"] == "new@example.com"


def test_keys_file_overrides_config_yaml():
    with workdir() as d:
        (d / "config.yaml").write_text(
            "sms_providers:\n  tiger-sms:\n    api_key: from-yaml\n"
        )
        assert load_sms_settings("config.yaml")["tiger-sms"]["api_key"] == "from-yaml"
        save_sms_key("tiger-sms", api_key="from-ui")
        assert load_sms_settings("config.yaml")["tiger-sms"]["api_key"] == "from-ui"


def test_blank_value_falls_back_instead_of_blanking():
    with workdir() as d:
        (d / "config.yaml").write_text(
            "sms_providers:\n  tiger-sms:\n    api_key: from-yaml\n"
        )
        save_sms_key("tiger-sms", api_key="")  # cleared in the UI
        # Should fall back to config, not end up with an empty key.
        assert load_sms_settings("config.yaml")["tiger-sms"]["api_key"] == "from-yaml"


def test_corrupt_keys_file_is_ignored_not_fatal():
    with workdir() as d:
        (d / "sms_keys.json").write_text("{not json")
        assert load_sms_settings("config.yaml") == {}


def test_missing_files_give_empty_settings():
    with workdir():
        assert load_sms_settings("config.yaml") == {}


# --- restock detection ----------------------------------------------------


def test_first_sweep_does_not_fire_a_restock_alert():
    if not HAVE_FLASK:
        print("SKIP: flask not installed in this env")
        return
    from sams_automation.web_sms import SmsCheckJob

    _stub_five_sim(count=47)
    with workdir():
        job = SmsCheckJob()
        job.start("config.yaml", us_only=True, terms=None)
        _wait(job)
        snap = job.snapshot()
        # A pool that was full the first time we ever looked is not a restock.
        assert snap["last_restock"] is None
        assert snap["poll_count"] == 1


def test_restock_fires_when_stock_returns():
    if not HAVE_FLASK:
        print("SKIP: flask not installed in this env")
        return
    from sams_automation.web_sms import SmsCheckJob

    with workdir():
        job = SmsCheckJob()
        _stub_five_sim(count=0)
        job.start("config.yaml", us_only=True, terms=None)
        _wait(job)
        assert job.snapshot()["last_restock"] is None

        _stub_five_sim(count=47)
        job.start("config.yaml", us_only=True, terms=None)
        _wait(job)
        snap = job.snapshot()
        assert snap["last_restock"] is not None
        assert snap["restock_offers"], "the new offer should be named in the alert"


def test_unchanged_stock_does_not_refire():
    if not HAVE_FLASK:
        print("SKIP: flask not installed in this env")
        return
    from sams_automation.web_sms import SmsCheckJob

    _stub_five_sim(count=47)
    with workdir():
        job = SmsCheckJob()
        for _ in range(3):
            job.start("config.yaml", us_only=True, terms=None)
            _wait(job)
        assert job.snapshot()["last_restock"] is None


# --- routes ---------------------------------------------------------------


def test_routes_and_key_masking():
    if not HAVE_FLASK:
        print("SKIP: flask not installed in this env")
        return
    from sams_automation.web import create_app

    _stub_five_sim(count=47)
    with workdir():
        # Neither config.yaml nor accounts.csv exists — must still serve.
        client = create_app("config.yaml", "accounts.csv").test_client()
        assert client.get("/sms").status_code == 200

        payload = client.get("/api/sms/providers").get_json()
        assert payload["providers"], "no providers listed"
        assert any(p["name"] == "5sim" and p["has_key"] for p in payload["providers"])

        assert client.post(
            "/api/sms/key", json={"provider": "tiger-sms", "api_key": "SUPERSECRET1234"}
        ).get_json()["ok"]

        body = client.get("/api/sms/providers").get_data(as_text=True)
        assert "SUPERSECRET1234" not in body, "raw key leaked to the browser"
        row = next(
            p for p in json.loads(body)["providers"] if p["name"] == "tiger-sms"
        )
        assert row["masked"] == "••••1234" and row["source"] == "saved"


def test_key_route_rejects_missing_provider():
    if not HAVE_FLASK:
        print("SKIP: flask not installed in this env")
        return
    from sams_automation.web import create_app

    with workdir():
        client = create_app("config.yaml", "accounts.csv").test_client()
        assert not client.post("/api/sms/key", json={"api_key": "x"}).get_json()["ok"]


def test_dead_provider_listed_but_not_counted_ready():
    if not HAVE_FLASK:
        print("SKIP: flask not installed in this env")
        return
    from sams_automation.web_sms import provider_rows

    with workdir():
        rows = provider_rows("config.yaml")
        dead = [r for r in rows if r["dead"]]
        assert dead and all(not r["has_key"] for r in dead)
        assert any("shut down" in r["note"] for r in dead)


# --- helpers --------------------------------------------------------------


def _stub_five_sim(count: int) -> None:
    def fake(url, **kw):
        if "guest/products" in url:
            return json.dumps({"walmart": {}})
        return json.dumps(
            {"walmart": {"usa": {"virtual21": {"cost": 12.5, "count": count, "rate": 95.0}}}}
        )

    sp._request = fake


def _wait(job, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while job.running and time.time() < deadline:
        time.sleep(0.02)
    assert not job.running, "check job did not finish"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok    {name}")
            except AssertionError as e:
                failures += 1
                print(f"  FAIL  {name}: {e}")
    print(f"\n{'FAILED' if failures else 'All tests passed'} ({failures} failure(s))")
    raise SystemExit(1 if failures else 0)
