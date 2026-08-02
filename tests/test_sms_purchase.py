"""Offline tests for buying a number and reading its code.

This is the code path that spends money, so the guard rails get the most
attention here: nothing should be orderable without an explicit opt-in, a
known price, and room under both caps.

Run with:  python tests/test_sms_purchase.py   (or)   python -m pytest
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sams_automation import sms_providers as sp  # noqa: E402
from sams_automation import sms_purchase as pur  # noqa: E402

OFFER = sp.Offer(
    provider="daisysms", service="Walmart", service_code="wm",
    country="USA", price=0.35, currency="USD", count=9,
)


def _cfg(**kw):
    base = dict(enabled=True, dry_run=False, code_timeout_seconds=10,
                poll_interval_seconds=0, log_file=os.devnull)
    base.update(kw)
    return pur.PurchaseConfig(**base)


def _provider():
    return sp.DaisySms({"api_key": "k"})


# --- guard rails ----------------------------------------------------------


def test_purchasing_is_off_by_default():
    cfg = pur.load_purchase_config({})
    assert cfg.enabled is False
    assert cfg.dry_run is True


def test_enabling_still_defaults_to_dry_run():
    # Turning purchasing on and actually spending must be two decisions.
    cfg = pur.load_purchase_config({"enabled": True})
    assert cfg.enabled and cfg.dry_run


def test_disabled_refuses_before_any_request():
    calls = []
    sp._request = lambda *a, **k: calls.append(a) or ""
    try:
        pur.acquire(_provider(), OFFER, pur.PurchaseConfig(), pur.Budget())
        assert False, "should have refused"
    except pur.PurchaseRefused as e:
        assert "purchasing is off" in str(e)
    assert not calls, "made a network call while disabled"


def test_dry_run_refuses_before_any_request():
    calls = []
    sp._request = lambda *a, **k: calls.append(a) or ""
    try:
        pur.acquire(_provider(), OFFER, _cfg(dry_run=True), pur.Budget())
        assert False, "should have refused"
    except pur.PurchaseRefused as e:
        assert "dry run" in str(e) and "0.35" in str(e)
    assert not calls, "dry run must not touch the network"


def test_unpriced_offer_is_never_bought():
    blind = sp.Offer("p", "Walmart", "wm", "USA", price=None, currency="USD")
    try:
        pur.acquire(_provider(), blind, _cfg(), pur.Budget())
        assert False, "should have refused"
    except pur.PurchaseRefused as e:
        assert "refusing to buy blind" in str(e)


def test_rub_offer_without_a_rate_is_not_bought():
    # Price is known but not in USD, so the cap can't be applied. Refuse.
    rub = sp.Offer("5sim", "walmart", "walmart", "usa", price=12.5, currency="RUB")
    try:
        pur.acquire(sp.FiveSim({}), rub, _cfg(), pur.Budget())
        assert False, "should have refused"
    except pur.PurchaseRefused as e:
        assert "buy blind" in str(e)


def test_per_number_cap_is_enforced():
    budget = pur.Budget(max_price_usd=0.20, max_total_usd=100)
    try:
        pur.acquire(_provider(), OFFER, _cfg(), budget)
        assert False, "should have refused"
    except pur.PurchaseRefused as e:
        assert "per-number cap" in str(e)


def test_run_total_cap_is_enforced():
    budget = pur.Budget(max_price_usd=1.0, max_total_usd=1.0, spent=0.80)
    try:
        pur.acquire(_provider(), OFFER, _cfg(), budget)
        assert False, "should have refused"
    except pur.PurchaseRefused as e:
        assert "over the cap" in str(e)


def test_budget_only_counts_completed_orders():
    budget = pur.Budget()
    try:
        pur.acquire(_provider(), OFFER, _cfg(dry_run=True), budget)
    except pur.PurchaseRefused:
        pass
    assert budget.spent == 0.0, "a refused order must not consume budget"


# --- the happy path -------------------------------------------------------


def test_activate_buy_and_poll():
    state = {"polls": 0}

    def fake(url, **kw):
        action = (kw.get("params") or {}).get("action")
        if action == "getNumber":
            return "ACCESS_NUMBER:12345:+13055550123"
        if action == "getStatus":
            state["polls"] += 1
            return "STATUS_WAIT_CODE" if state["polls"] < 3 else "STATUS_OK:456789"
        return ""
    sp._request = fake

    budget = pur.Budget()
    p = pur.acquire(_provider(), OFFER, _cfg(), budget, sleep=lambda s: None)
    assert p.code == "456789"
    assert p.phone == "+13055550123"
    assert p.national == "3055550123", "the form wants 10 digits"
    assert budget.spent == 0.35


def test_timeout_cancels_the_number():
    cancelled = []

    def fake(url, **kw):
        action = (kw.get("params") or {}).get("action")
        if action == "getNumber":
            return "ACCESS_NUMBER:1:+13055550123"
        if action == "setStatus":
            cancelled.append((kw.get("params") or {}).get("status"))
            return "ACCESS_CANCEL"
        return "STATUS_WAIT_CODE"
    sp._request = fake

    clock = {"t": 0.0}

    def now():
        return clock["t"]

    def sleep(s):
        clock["t"] += 5

    try:
        pur.acquire(_provider(), OFFER, _cfg(), pur.Budget(), sleep=sleep, now=now)
        assert False, "should have timed out"
    except sp.ProviderError as e:
        assert "no code within" in str(e)
    # An unused number must be released — most providers refund it.
    assert cancelled == ["8"], cancelled


def test_provider_cancelling_the_order_is_reported():
    def fake(url, **kw):
        action = (kw.get("params") or {}).get("action")
        if action == "getNumber":
            return "ACCESS_NUMBER:1:+13055550123"
        return "STATUS_CANCEL"
    sp._request = fake
    try:
        pur.acquire(_provider(), OFFER, _cfg(), pur.Budget(), sleep=lambda s: None)
        assert False, "should have raised"
    except sp.ProviderError as e:
        assert "cancelled" in str(e)


def test_unexpected_getnumber_reply_is_surfaced():
    sp._request = lambda url, **kw: "NO_BALANCE"
    try:
        pur.acquire(_provider(), OFFER, _cfg(), pur.Budget(), sleep=lambda s: None)
        assert False, "should have raised"
    except sp.ProviderError as e:
        assert "NO_BALANCE" in str(e)


def test_purchase_is_logged_before_the_code_arrives():
    # A crash mid-poll must still leave a record of money spent.
    def fake(url, **kw):
        action = (kw.get("params") or {}).get("action")
        if action == "getNumber":
            return "ACCESS_NUMBER:77:+13055550123"
        return "STATUS_OK:111222"
    sp._request = fake

    with tempfile.TemporaryDirectory() as d:
        log = Path(d) / "purchases.csv"
        pur.acquire(_provider(), OFFER, _cfg(log_file=str(log)), pur.Budget(),
                    sleep=lambda s: None)
        rows = log.read_text().strip().splitlines()
        assert len(rows) == 3, rows          # header + pre-code + post-code
        assert "77" in rows[1] and "111222" in rows[2]


# --- provider routing -----------------------------------------------------


def test_buyer_routing_covers_every_protocol():
    assert isinstance(pur.buyer_for(sp.FiveSim({})), pur.FiveSimBuyer)
    assert isinstance(pur.buyer_for(sp.SmsPool({"api_key": "k"})), pur.SmsPoolBuyer)
    assert isinstance(pur.buyer_for(sp.TextVerified({"api_key": "k"})), pur.TextVerifiedBuyer)
    assert isinstance(pur.buyer_for(sp.DaisySms({"api_key": "k"})), pur.ActivateBuyer)
    assert isinstance(pur.buyer_for(sp.HeroSms({"api_key": "k"})), pur.ActivateBuyer)
    # A custom activate-protocol site registered by probing.
    custom = sp.SmsActivateCompat({"name": "x", "base_url": "https://x", "api_key": "k"})
    assert isinstance(pur.buyer_for(custom), pur.ActivateBuyer)


def test_smspool_buy_parses_order():
    def fake(url, **kw):
        if "purchase/sms" in url:
            return json.dumps({"order_id": "A1", "number": "13055550123", "cost": "0.62"})
        return json.dumps({"sms": "998877"})
    sp._request = fake
    offer = sp.Offer("smspool", "Walmart", "1023", "1", price=0.62, currency="USD")
    p = pur.acquire(sp.SmsPool({"api_key": "k"}), offer, _cfg(), pur.Budget(),
                    sleep=lambda s: None)
    assert p.order_id == "A1" and p.code == "998877" and p.national == "3055550123"


def test_fivesim_buy_parses_order():
    def fake(url, **kw):
        if "/buy/activation/" in url:
            return json.dumps({"id": 991, "phone": "+13055550123", "price": 12.5})
        return json.dumps({"sms": [{"code": "334455"}]})
    sp._request = fake
    offer = sp.Offer("5sim", "walmart", "walmart", "usa", "virtual21",
                     price=0.30, currency="USD")
    p = pur.acquire(sp.FiveSim({"api_key": "k"}), offer, _cfg(), pur.Budget(),
                    sleep=lambda s: None)
    assert p.order_id == "991" and p.code == "334455"


# --- offer selection ------------------------------------------------------


def test_pick_offer_takes_the_cheapest_in_stock():
    cheap = sp.Offer("a", "Walmart", "wm", "USA", price=0.35, currency="USD", count=5)
    dear = sp.Offer("b", "Walmart", "wm", "USA", price=0.90, currency="USD", count=5)
    empty = sp.Offer("c", "Walmart", "wm", "USA", price=0.10, currency="USD", count=0)
    reports = [
        sp.ProviderReport("a", ok=True, offers=[cheap]),
        sp.ProviderReport("b", ok=True, offers=[dear]),
        sp.ProviderReport("c", ok=True, offers=[empty]),
    ]
    usd, _, offer = pur.pick_offer(reports, max_price_usd=1.0, rub_per_usd=None)
    assert offer is cheap and usd == 0.35


def test_pick_offer_skips_over_cap_and_unpriceable():
    dear = sp.Offer("a", "Walmart", "wm", "USA", price=5.00, currency="USD", count=5)
    rub = sp.Offer("b", "walmart", "walmart", "usa", price=12.5, currency="RUB", count=5)
    reports = [
        sp.ProviderReport("a", ok=True, offers=[dear]),
        sp.ProviderReport("b", ok=True, offers=[rub]),
    ]
    assert pur.pick_offer(reports, max_price_usd=1.0, rub_per_usd=None) is None
    # With a rate the RUB offer becomes comparable and affordable.
    picked = pur.pick_offer(reports, max_price_usd=1.0, rub_per_usd=90.0)
    assert picked and picked[2] is rub


def test_pick_offer_ignores_failed_providers():
    reports = [sp.ProviderReport("a", ok=False, error="boom")]
    assert pur.pick_offer(reports, max_price_usd=1.0, rub_per_usd=None) is None


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
