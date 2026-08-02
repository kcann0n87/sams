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


def test_secureiosms_buy_poll_and_cancel():
    calls = []

    def fake(url, **kw):
        calls.append((kw.get("method", "GET"), url, kw.get("form")))
        if "getnumber" in url:
            return json.dumps({"id": "ORD7", "number": "+13055550123", "price": 0.55})
        if "checkstatus" in url:
            return json.dumps({"code": "552211"})
        return json.dumps({"ok": True})
    sp._request = fake

    offer = sp.Offer("secureiosms", "walmart", "walmart", "US", price=0.55, currency="USD")
    p = pur.acquire(sp.SecureIoSms({"api_key": "k"}), offer, _cfg(), pur.Budget(),
                    sleep=lambda s: None)
    assert p.order_id == "ORD7" and p.code == "552211" and p.national == "3055550123"
    # The key travels as a query param, never in the body.
    assert all(f is None or "api_key" not in f for _, _, f in calls)


def test_secureiosms_cancel_uses_a_post_with_the_documented_body():
    posted = []

    def fake(url, **kw):
        if "getnumber" in url:
            return json.dumps({"id": "ORD8", "number": "+13055550123"})
        if "changestatus" in url:
            posted.append((kw.get("method"), kw.get("form")))
            return json.dumps({"ok": True})
        return json.dumps({})          # checkstatus: never returns a code
    sp._request = fake

    now, sleep = _clock()
    offer = sp.Offer("secureiosms", "walmart", "walmart", "US", price=0.55, currency="USD")
    try:
        pur.acquire(sp.SecureIoSms({"api_key": "k"}), offer, _cfg(), pur.Budget(),
                    sleep=sleep, now=now)
        assert False, "should have timed out"
    except sp.ProviderError:
        pass
    assert posted == [("POST", {"id": "ORD8", "action": "cancel"})], posted


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


# --- failover across providers --------------------------------------------


def _pair(name, price, count=5, code_ok=True):
    prov = sp.DaisySms({"api_key": "k", "name": name, "base_url": f"https://{name}"})
    offer = sp.Offer(name, "Walmart", "wm", "USA", price=price, currency="USD", count=count)
    return prov, sp.ProviderReport(name, ok=True, offers=[offer])


def _router(behaviour: dict):
    """behaviour: host -> 'code' | 'timeout' | 'nobalance'."""
    def fake(url, **kw):
        host = url.split("//")[1].split("/")[0]
        mode = behaviour.get(host, "timeout")
        action = (kw.get("params") or {}).get("action")
        if action == "getNumber":
            if mode == "nobalance":
                return "NO_BALANCE"
            return f"ACCESS_NUMBER:1:+1305555{abs(hash(host)) % 10000:04d}"
        if action == "setStatus":
            return "ACCESS_CANCEL"
        return "STATUS_OK:123456" if mode == "code" else "STATUS_WAIT_CODE"
    return fake


def _clock():
    t = {"v": 0.0}
    return (lambda: t["v"]), (lambda s: t.__setitem__("v", t["v"] + 5))


def test_failover_moves_on_when_a_provider_never_sends_the_code():
    sp._request = _router({"a": "timeout", "b": "timeout", "c": "code"})
    now, sleep = _clock()
    pairs = [_pair("a", 0.30), _pair("b", 0.40), _pair("c", 0.50)]
    purchase, attempts = pur.acquire_any(
        pairs, _cfg(), pur.Budget(), sleep=sleep, now=now
    )
    assert purchase.code == "123456"
    assert [a.provider for a in attempts] == ["a", "b", "c"]
    assert [a.ok for a in attempts] == [False, False, True]


def test_failover_tries_cheapest_first():
    sp._request = _router({"a": "code", "b": "code", "c": "code"})
    now, sleep = _clock()
    pairs = [_pair("a", 0.90), _pair("b", 0.20), _pair("c", 0.50)]
    _, attempts = pur.acquire_any(pairs, _cfg(), pur.Budget(), sleep=sleep, now=now)
    assert [a.provider for a in attempts] == ["b"], "should have stopped at the cheapest"


def test_cancelled_attempts_do_not_burn_the_run_budget():
    # Two failures then a success, under a cap that only fits one purchase.
    # Cancelled numbers are refunded, so the run must still complete.
    sp._request = _router({"a": "timeout", "b": "timeout", "c": "code"})
    now, sleep = _clock()
    budget = pur.Budget(max_price_usd=1.0, max_total_usd=0.60)
    pairs = [_pair("a", 0.50), _pair("b", 0.50), _pair("c", 0.50)]
    purchase, attempts = pur.acquire_any(pairs, _cfg(), budget, sleep=sleep, now=now)
    assert purchase.code == "123456"
    assert len(attempts) == 3
    assert abs(budget.spent - 0.50) < 1e-9, "only the successful buy should count"


def test_failover_stops_at_max_attempts():
    sp._request = _router({h: "timeout" for h in "abcdef"})
    now, sleep = _clock()
    pairs = [_pair(h, 0.10 * (i + 1)) for i, h in enumerate("abcdef")]
    seen = []
    try:
        pur.acquire_any(pairs, _cfg(), pur.Budget(max_total_usd=99),
                        max_attempts=3, on_attempt=seen.append, sleep=sleep, now=now)
        assert False, "should have given up"
    except sp.ProviderError as e:
        assert "no provider delivered a code" in str(e)
    # Six providers available, but max_attempts caps the spend of time and money.
    assert [a.provider for a in seen] == ["a", "b", "c"], seen


def test_failover_survives_a_provider_erroring_on_purchase():
    # NO_BALANCE on the first is not fatal to the run.
    sp._request = _router({"a": "nobalance", "b": "code"})
    now, sleep = _clock()
    purchase, attempts = pur.acquire_any(
        [_pair("a", 0.30), _pair("b", 0.40)], _cfg(), pur.Budget(), sleep=sleep, now=now
    )
    assert purchase.code == "123456"
    assert not attempts[0].ok and "NO_BALANCE" in attempts[0].detail


def test_failover_reports_when_nothing_is_in_stock():
    empty = _pair("a", 0.30, count=0)
    try:
        pur.acquire_any([empty], _cfg(), pur.Budget())
        assert False, "should have refused"
    except pur.PurchaseRefused as e:
        assert "no in-stock offer" in str(e)


def test_rank_offers_keeps_every_pool_at_a_provider():
    # A site often lists several Walmart pools that don't share stock; all of
    # them are candidates.
    prov = sp.DaisySms({"api_key": "k", "name": "a", "base_url": "https://a"})
    report = sp.ProviderReport("a", ok=True, offers=[
        sp.Offer("a", "Walmart", "wm", "USA", "op1", price=0.50, currency="USD", count=3),
        sp.Offer("a", "Walmart", "wm", "USA", "op2", price=0.20, currency="USD", count=3),
        sp.Offer("a", "Walmart Grocery", "wmg", "USA", "op1", price=0.30,
                 currency="USD", count=3),
    ])
    ranked = pur.rank_offers([(prov, report)], max_price_usd=1.0, rub_per_usd=None)
    assert [r[0] for r in ranked] == [0.20, 0.30, 0.50]


def test_rank_offers_breaks_price_ties_on_success_rate():
    prov = sp.DaisySms({"api_key": "k", "name": "a", "base_url": "https://a"})
    report = sp.ProviderReport("a", ok=True, offers=[
        sp.Offer("a", "Walmart", "wm", "USA", "bad", price=0.30, currency="USD",
                 count=3, success_rate=40.0),
        sp.Offer("a", "Walmart", "wm", "USA", "good", price=0.30, currency="USD",
                 count=3, success_rate=95.0),
    ])
    ranked = pur.rank_offers([(prov, report)], max_price_usd=1.0, rub_per_usd=None)
    assert [r[2].operator for r in ranked] == ["good", "bad"]


def test_bad_key_skips_that_providers_remaining_pools():
    # A dead key kills every pool at the site, so don't spend attempts on them.
    sp._request = _router({"a": "nobalance", "b": "code"})
    now, sleep = _clock()
    prov_a = sp.DaisySms({"api_key": "k", "name": "a", "base_url": "https://a"})
    report_a = sp.ProviderReport("a", ok=True, offers=[
        sp.Offer("a", "Walmart", "wm", "USA", "op1", price=0.10, currency="USD", count=3),
        sp.Offer("a", "Walmart", "wm", "USA", "op2", price=0.20, currency="USD", count=3),
        sp.Offer("a", "Walmart", "wm", "USA", "op3", price=0.30, currency="USD", count=3),
    ])
    seen = []
    purchase, _ = pur.acquire_any(
        [(prov_a, report_a), _pair("b", 0.90)], _cfg(), pur.Budget(),
        on_attempt=seen.append, sleep=sleep, now=now,
    )
    assert purchase.code == "123456"
    # One attempt at 'a', then straight to 'b' — not three at 'a'.
    assert [s.provider for s in seen] == ["a", "b"], seen


def test_timeout_still_tries_the_next_pool_at_the_same_provider():
    # Unlike a bad key, a pool that doesn't deliver says nothing about the
    # other pools at that site.
    state = {"n": 0}

    def fake(url, **kw):
        action = (kw.get("params") or {}).get("action")
        if action == "getNumber":
            state["n"] += 1
            return f"ACCESS_NUMBER:{state['n']}:+1305555000{state['n']}"
        if action == "setStatus":
            return "ACCESS_CANCEL"
        # First bought number never gets a code; the second does.
        return "STATUS_OK:246810" if state["n"] >= 2 else "STATUS_WAIT_CODE"
    sp._request = fake

    now, sleep = _clock()
    prov = sp.DaisySms({"api_key": "k", "name": "a", "base_url": "https://a"})
    report = sp.ProviderReport("a", ok=True, offers=[
        sp.Offer("a", "Walmart", "wm", "USA", "op1", price=0.10, currency="USD", count=3),
        sp.Offer("a", "Walmart", "wm", "USA", "op2", price=0.20, currency="USD", count=3),
    ])
    seen = []
    purchase, _ = pur.acquire_any([(prov, report)], _cfg(), pur.Budget(),
                                  on_attempt=seen.append, sleep=sleep, now=now)
    assert purchase.code == "246810"
    assert [s.offer.operator for s in seen] == ["op1", "op2"]


def test_failover_progress_is_reported_as_it_happens():
    sp._request = _router({"a": "timeout", "b": "code"})
    now, sleep = _clock()
    seen = []
    pur.acquire_any([_pair("a", 0.30), _pair("b", 0.40)], _cfg(), pur.Budget(),
                    on_attempt=seen.append, sleep=sleep, now=now)
    # Callback fires per attempt, so a long run isn't silent.
    assert [s.provider for s in seen] == ["a", "b"]


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
