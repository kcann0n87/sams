"""Offline tests for the SMS-provider adapters.

Every network call is stubbed, so these run anywhere and pin down the response
shapes each adapter expects. When a provider changes its payload, fix the
fixture here first — that's the fastest way to see what the parser will do.

Run with:  python tests/test_sms_providers.py   (or)   python -m pytest
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sams_automation import sms_providers as sp  # noqa: E402


class FakeHttp:
    """Stand-in for _request that replays canned bodies by URL substring."""

    def __init__(self, routes: dict[str, object]):
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, url, **kw):
        full = url
        if kw.get("params"):
            import urllib.parse

            full = f"{url}?{urllib.parse.urlencode(kw['params'])}"
        self.calls.append(full)
        for key, body in self.routes.items():
            if key in full:
                return body if isinstance(body, str) else json.dumps(body)
        raise sp.ProviderError(f"no fixture for {full}")


def install(monkey_routes: dict[str, object]) -> FakeHttp:
    fake = FakeHttp(monkey_routes)
    sp._request = fake  # type: ignore[assignment]
    return fake


CLEARED_ENV = [spec["env"] for spec in sp.KNOWN_ACTIVATE_HOSTS.values()] + [
    "TEXTVERIFIED_API_KEY",
    "TEXTVERIFIED_USERNAME",
    "DAISYSMS_API_KEY",
    "HEROSMS_API_KEY",
    "SMSPOOL_API_KEY",
]


@contextlib.contextmanager
def env(**overrides: str):
    """Hermetic environment: every provider key cleared, then overrides applied.

    Written as a context manager rather than a pytest fixture so this file runs
    identically under pytest and as a plain script.
    """
    saved = {k: os.environ.get(k) for k in set(CLEARED_ENV) | set(overrides)}
    try:
        for key in CLEARED_ENV:
            os.environ.pop(key, None)
        os.environ.update(overrides)
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# --- name matching --------------------------------------------------------


def test_matches_handles_punctuation_and_case():
    assert sp._matches("Walmart", sp.DEFAULT_TERMS)
    assert sp._matches("WALMART Grocery", sp.DEFAULT_TERMS)
    assert sp._matches("walmart_us", sp.DEFAULT_TERMS)
    assert sp._matches("Wal-Mart", sp.DEFAULT_TERMS)
    assert not sp._matches("Walgreens", sp.DEFAULT_TERMS)
    assert not sp._matches("Target", sp.DEFAULT_TERMS)
    # Punctuation-insensitivity still works for a caller-supplied term.
    assert sp._matches("Sam's Club", ["sams club"])


def test_default_terms_are_walmart_only():
    # Sam's Club is a separate service on these providers and isn't wanted here;
    # anything else is opt-in per run via --term.
    assert sp.DEFAULT_TERMS == ("walmart",)
    assert not sp._matches("Sam's Club", sp.DEFAULT_TERMS)
    assert not sp._matches("samsclub", sp.DEFAULT_TERMS)


def test_offer_in_stock_semantics():
    # None means "provider didn't say", which must not read as out of stock.
    assert sp.Offer("p", "walmart", "wm", "USA", count=None).in_stock
    assert sp.Offer("p", "walmart", "wm", "USA", count=5).in_stock
    assert not sp.Offer("p", "walmart", "wm", "USA", count=0).in_stock


# --- 5sim -----------------------------------------------------------------


FIVESIM_CATALOG = {"walmart": {"Category": "activation", "Qty": 1200, "Price": 12}, "uber": {}}
FIVESIM_PRICES = {
    "walmart": {
        "usa": {
            "virtual21": {"cost": 12.5, "count": 1200, "rate": 95.5},
            "virtual4": {"cost": 9.0, "count": 0, "rate": 80.0},
        },
        "canada": {"virtual1": {"cost": 30.0, "count": 5, "rate": 99.0}},
    }
}


def test_fivesim_parses_us_offers():
    install({"guest/products": FIVESIM_CATALOG, "guest/prices": FIVESIM_PRICES})
    report = sp.FiveSim({}).check()
    assert report.ok, report.error
    assert {o.operator for o in report.offers} == {"virtual21", "virtual4"}
    top = next(o for o in report.offers if o.operator == "virtual21")
    assert top.price == 12.5 and top.count == 1200 and top.currency == "RUB"
    assert top.success_rate == 95.5
    assert report.total_stock == 1200


def test_fivesim_skips_rental_products():
    # "hosting" is 5sim's rented-number product. Pay-per-verification only, so
    # a rental must never appear next to a per-code price.
    install(
        {
            "guest/products": {
                "walmart": {"Category": "activation"},
                "walmart_hosting": {"Category": "hosting"},
            },
            "guest/prices": FIVESIM_PRICES,
        }
    )
    report = sp.FiveSim({}).check()
    assert report.ok, report.error
    assert {o.service for o in report.offers} == {"walmart"}
    assert any("not pay-per-code" in n for n in report.notes)


def test_fivesim_missing_category_is_treated_as_activation():
    # Older/partial catalog rows omit Category; don't silently drop a real
    # activation product over a missing field.
    install({"guest/products": {"walmart": {}}, "guest/prices": FIVESIM_PRICES})
    report = sp.FiveSim({}).check()
    assert report.ok and report.offers


def test_fivesim_all_countries_includes_canada():
    install({"guest/products": FIVESIM_CATALOG, "guest/prices": FIVESIM_PRICES})
    report = sp.FiveSim({}).check(us_only=False)
    assert "canada" in {o.country for o in report.offers}


def test_fivesim_falls_back_to_direct_probe():
    # Catalog endpoint down: the adapter should still find walmart by probing.
    install({"guest/prices": FIVESIM_PRICES})
    report = sp.FiveSim({}).check()
    assert report.ok and report.offers
    assert any("catalog lookup failed" in n for n in report.notes)


def test_fivesim_reports_missing_service_rather_than_zero():
    install({"guest/products": {"uber": {}}, "guest/prices": {}})
    report = sp.FiveSim({}).check()
    assert report.ok and report.offers == []
    assert any("no Walmart-like service" in n for n in report.notes)


# --- handler_api family (DaisySMS / HeroSMS) ------------------------------


DAISY_PRICES = {
    "187": {
        "wm": {"cost": "0.35", "count": 812, "name": "Walmart"},
        "go": {"cost": "0.25", "count": 5000, "name": "Google"},
    }
}
DAISY_SERVICES = {"services": [{"code": "wm", "name": "Walmart"}, {"code": "go", "name": "Google"}]}


def test_daisysms_matches_via_catalog_name():
    install({"getServicesList": DAISY_SERVICES, "getPricesVerification": DAISY_PRICES})
    report = sp.DaisySms({"api_key": "k"}).check()
    assert report.ok, report.error
    assert len(report.offers) == 1
    offer = report.offers[0]
    assert offer.service == "Walmart" and offer.service_code == "wm"
    assert offer.price == 0.35 and offer.count == 812 and offer.country == "USA"


def test_handler_api_matches_without_catalog():
    # getServicesList unavailable: fall back to the name inside the price rows.
    install({"getPricesVerification": DAISY_PRICES})
    report = sp.DaisySms({"api_key": "k"}).check()
    assert report.ok and len(report.offers) == 1
    assert report.offers[0].service_code == "wm"


def test_handler_api_surfaces_bad_key():
    install({"handler_api": "BAD_KEY"})
    report = sp.HeroSms({"api_key": "nope"}).check()
    assert not report.ok
    assert "BAD_KEY" in (report.error or "")


def test_handler_api_accepts_unnested_country_payload():
    # Some deployments drop the country key when `country` is pinned.
    install({"getPricesVerification": {"wm": {"cost": "0.40", "count": 3, "name": "Walmart"}}})
    report = sp.DaisySms({"api_key": "k"}).check()
    assert report.ok and len(report.offers) == 1
    assert report.offers[0].count == 3


def test_missing_key_is_reported_not_raised():
    with env():
        report = sp.DaisySms({}).check()
    assert not report.ok and "no API key" in (report.error or "")


# --- TextVerified ---------------------------------------------------------


TV_AUTH = {"token": "abc123", "expiresAt": "2030-01-01T00:00:00Z"}
TV_SERVICES = [
    {"serviceName": "walmart", "capability": "sms", "cost": 0.75},
    {"serviceName": "yahoo", "capability": "sms", "cost": 0.5},
]


def test_textverified_parses_services():
    install({"/auth": TV_AUTH, "/services": TV_SERVICES})
    report = sp.TextVerified({"api_key": "k", "username": "me@example.com"}).check()
    assert report.ok, report.error
    assert len(report.offers) == 1
    offer = report.offers[0]
    assert offer.service_code == "walmart" and offer.price == 0.75
    assert offer.country == "USA" and offer.currency == "USD"
    assert offer.in_stock  # no count reported == listed and orderable


def test_textverified_falls_back_to_pricing_endpoint():
    install(
        {
            "/auth": TV_AUTH,
            "/services": [{"serviceName": "walmart", "capability": "sms"}],
            "/pricing/verifications": {"price": 0.9},
        }
    )
    report = sp.TextVerified({"api_key": "k", "username": "me@example.com"}).check()
    assert report.ok and report.offers[0].price == 0.9


def test_textverified_requires_username():
    with env():
        report = sp.TextVerified({"api_key": "k"}).check()
    assert not report.ok and "username" in (report.error or "")


def test_textverified_wrapped_service_list():
    install({"/auth": TV_AUTH, "/services": {"data": TV_SERVICES}})
    report = sp.TextVerified({"api_key": "k", "username": "me@example.com"}).check()
    assert report.ok and len(report.offers) == 1


# --- SMSPool --------------------------------------------------------------


POOL_SERVICES = [{"ID": "1023", "name": "Walmart"}, {"ID": "7", "name": "Google"}]
POOL_COUNTRIES = [{"ID": "1", "name": "United States"}, {"ID": "2", "name": "Canada"}]
POOL_PRICE = {"price": "0.62", "amount": 44, "success_rate": 92}


def test_smspool_parses_price_and_stock():
    install(
        {
            "service/retrieve_all": POOL_SERVICES,
            "country/retrieve_all": POOL_COUNTRIES,
            "request/price": POOL_PRICE,
        }
    )
    report = sp.SmsPool({"api_key": "k"}).check()
    assert report.ok, report.error
    assert len(report.offers) == 1
    offer = report.offers[0]
    assert offer.service_code == "1023" and offer.price == 0.62
    assert offer.count == 44 and offer.country == "United States"


def test_smspool_no_walmart_service():
    install({"service/retrieve_all": [{"ID": "7", "name": "Google"}]})
    report = sp.SmsPool({"api_key": "k"}).check()
    assert report.ok and report.offers == []


# --- pvacodes / verifysms -------------------------------------------------


def test_pvacodes_unwraps_the_status_envelope():
    install({"api.php": {"status": {"code": "1000", "message": "ok"},
                         "data": {"Walmart": {"price": 0.60, "count": 8},
                                  "Facebook": {"price": 0.30}}}})
    report = sp.PvaCodes({"api_key": "k"}).check()
    assert report.ok, report.error
    assert [(o.service, o.price, o.count) for o in report.offers] == [("Walmart", 0.60, 8)]


def test_pvacodes_treats_a_non_1000_status_as_an_error():
    # These arrive as HTTP 200, so the envelope is the only signal.
    for code, fragment in [("1002", "invalid API key"), ("1003", "Insufficient balance"),
                           ("2000", "Out of stock")]:
        install({"api.php": {"status": {"code": code, "message": fragment}, "data": ""}})
        report = sp.PvaCodes({"api_key": "k"}).check()
        assert not report.ok, f"{code} should not read as success"
        assert code in (report.error or "")


def test_verifysms_finds_walmart_by_name_not_hardcoded_code():
    install({"api/services": {"fc": {"name": "Walmart", "cost": 0.5, "in_stock": True},
                              "ty": {"name": "DoorDash", "cost": 0.5, "in_stock": True}}})
    report = sp.VerifySms({"api_key": "k"}).check()
    assert report.ok, report.error
    assert {o.service_code for o in report.offers} == {"fc"}


def test_verifysms_offers_one_candidate_per_carrier():
    # Carriers are separate pools; each is its own failover candidate.
    install({"api/services": {"fc": {"name": "Walmart", "cost": 0.5, "in_stock": True}}})
    report = sp.VerifySms({"api_key": "k"}).check()
    assert [o.operator for o in report.offers] == ["verizon", "att", "tmobile"]


def test_verifysms_respects_the_in_stock_boolean():
    # No count is reported, so the boolean is the only stock signal.
    install({"api/services": {"fc": {"name": "Walmart", "cost": 0.5, "in_stock": False}}})
    report = sp.VerifySms({"api_key": "k"}).check()
    assert report.offers and not any(o.in_stock for o in report.offers)

    install({"api/services": {"fc": {"name": "Walmart", "cost": 0.5, "in_stock": True}}})
    report = sp.VerifySms({"api_key": "k"}).check()
    assert all(o.in_stock for o in report.offers)


def test_every_configured_site_now_has_an_adapter():
    assert sp.UNKNOWN_PROTOCOL_SITES == {}
    for expected in ("smspva", "secureiosms", "pvacodes", "verifysms",
                     "5sim", "textverified", "smspool"):
        assert expected in sp.PROVIDERS, expected


# --- handler path variants ------------------------------------------------


def test_activate_handler_path_is_configurable():
    # Not every clone serves the protocol from stubs/handler_api.php.
    prov = sp.SmsActivateCompat(
        {"name": "x", "base_url": "https://x.example", "api_key": "k",
         "api_path": "app/api.php"}
    )
    assert prov._endpoint() == "https://x.example/app/api.php"


def test_activate_default_path_is_unchanged():
    prov = sp.SmsActivateCompat({"name": "x", "base_url": "https://x.example", "api_key": "k"})
    assert prov._endpoint() == "https://x.example/stubs/handler_api.php"


def test_pvacodes_is_its_own_protocol_not_an_activate_clone():
    # app/api.php looks like a handler_api path but takes do= verbs, not
    # action=, so it needs its own adapter rather than a path override.
    with env(PVACODES_API_KEY="k"):
        built, _ = sp.build_providers({})
    pv = {p.name: p for p in built}["pvacodes"]
    assert isinstance(pv, sp.PvaCodes)
    assert not isinstance(pv, sp.SmsActivateCompat)
    assert "pvacodes" not in sp.KNOWN_ACTIVATE_HOSTS


def test_probe_finds_the_protocol_on_a_non_default_path():
    # Only app/api.php answers; the usual path 404s.
    def fake(url, **kw):
        if "app/api.php" not in url:
            raise sp.ProviderError("HTTP 404: Not Found")
        if (kw.get("params") or {}).get("action") == "getServicesList":
            return json.dumps(DAISY_SERVICES)
        return json.dumps(DAISY_PRICES)
    sp._request = fake

    results = sp.probe("https://newsite.example", "k", name="newsite")
    winner = results[0]
    assert winner.protocol == "sms-activate" and winner.sells_walmart


def test_probe_still_prefers_the_default_path_when_both_work():
    seen = []

    def fake(url, **kw):
        import urllib.parse

        params = kw.get("params") or {}
        seen.append(url + ("?" + urllib.parse.urlencode(params) if params else ""))
        if params.get("action") == "getServicesList":
            return json.dumps(DAISY_SERVICES)
        return json.dumps(DAISY_PRICES)
    sp._request = fake

    sp.probe("https://both.example", "k")
    assert "stubs/handler_api.php" in seen[0], "should try the common path first"
    # Scoped to the activate protocol: pvacodes probes app/api.php too, but
    # with do= verbs rather than action=.
    activate_calls = [u for u in seen if "action=" in u]
    assert activate_calls, "the activate probe should have run"
    assert not any("app/api.php" in u for u in activate_calls), (
        "activate probe kept trying paths after a hit"
    )


# --- smspva (its own protocol) --------------------------------------------


def test_smspva_needs_a_service_code():
    # Opaque optNN codes can't be name-matched, so a missing code must be
    # reported as configuration, not as an empty pool.
    with env():
        report = sp.SmsPva({"api_key": "k"}).check()
    assert report.ok and report.offers == []
    assert any("service_code" in n for n in report.notes)


def test_smspva_reads_price_and_stock():
    install({
        "get_service_price": {"price": 0.45},
        "get_count_new": {"count": 23},
    })
    report = sp.SmsPva({"api_key": "k", "service_code": "opt99"}).check()
    assert report.ok, report.error
    offer = report.offers[0]
    assert offer.price == 0.45 and offer.count == 23
    assert offer.service_code == "opt99" and offer.currency == "USD"


def test_smspva_survives_a_missing_stock_endpoint():
    install({"get_service_price": {"price": 0.45}})
    report = sp.SmsPva({"api_key": "k", "service_code": "opt99"}).check()
    assert report.ok and report.offers[0].price == 0.45
    # No count reported is "unknown", not "empty".
    assert report.offers[0].in_stock
    assert any("stock count unavailable" in n for n in report.notes)


def test_smspva_is_reachable_as_a_protocol():
    assert sp.PROTOCOLS["smspva"] is sp.SmsPva


def test_unknown_protocol_sites_are_named_not_hidden():
    # Real sites with real APIs we haven't identified — they must be listed so
    # they can be probed, not silently absent.
    # Sites graduate out of this list once their protocol is known; every one
    # currently in use now has an adapter.
    for name, url in sp.UNKNOWN_PROTOCOL_SITES.items():
        assert url.startswith("https://"), name
    for graduated in ("pvacodes", "secureiosms", "verifysms"):
        assert graduated not in sp.UNKNOWN_PROTOCOL_SITES
        assert graduated in sp.PROVIDERS


# --- protocol probing -----------------------------------------------------


def test_probe_identifies_handler_api_host():
    install({"getServicesList": DAISY_SERVICES, "getPrices": DAISY_PRICES})
    results = sp.probe("https://unknown.example", "k", name="unknown")
    winner = results[0]
    assert winner.protocol == "sms-activate", [r.protocol for r in results]
    assert winner.sells_walmart and winner.walmart[0].service_code == "wm"


def test_probe_identifies_smspool_host():
    install(
        {
            "service/retrieve_all": POOL_SERVICES,
            "country/retrieve_all": POOL_COUNTRIES,
            "request/price": POOL_PRICE,
        }
    )
    results = sp.probe("https://unknown.example", "k")
    assert results[0].protocol == "smspool"
    assert results[0].sells_walmart


def test_probe_ranks_walmart_match_above_bare_auth():
    # A host that answers on two protocols but only sells Walmart on one.
    install({"service/retrieve_all": [], "getPrices": DAISY_PRICES,
             "getServicesList": DAISY_SERVICES})
    results = sp.probe("https://unknown.example", "k")
    assert results[0].sells_walmart
    assert not results[0].detail.startswith("responded, but")


def test_probe_reports_total_failure_cleanly():
    install({})  # nothing answers
    results = sp.probe("https://unknown.example", "k")
    assert results and not any(r.ok for r in results)
    assert all(r.detail for r in results), "every failure needs a reason"


def test_probe_skips_protocols_it_cannot_detect():
    install({"getServicesList": DAISY_SERVICES, "getPrices": DAISY_PRICES})
    protocols = [r.protocol for r in sp.probe("https://unknown.example", "k")]
    assert protocols.count("sms-activate") == 1, "alias double-reported"
    # smspva answers without a network call when unconfigured, so probing it
    # would report a match against literally any host.
    for skipped in sp.UNPROBEABLE:
        assert skipped not in protocols


def test_probe_capture_never_records_the_api_key():
    # This output is meant to be pasted to a human for diagnosis, so the key
    # must not travel with it.
    install({"getServicesList": DAISY_SERVICES, "getPrices": DAISY_PRICES})
    calls: list = []
    sp.probe("https://unknown.example", "SUPERSECRETKEY", name="unknown", capture=calls)
    assert calls, "nothing captured"
    blob = repr(calls)
    assert "SUPERSECRETKEY" not in blob, "the API key leaked into the capture"


def test_redaction_covers_urls_error_text_and_bodies():
    key = "SUPERSECRETKEY"
    assert "SUPERSECRETKEY" not in sp._redact(
        f"HTTP 404: no route for /x?api_key={key}&action=getPrices", key
    )
    # Even without knowing the key, a key-bearing parameter is scrubbed.
    assert "hunter2" not in sp._redact("GET /x?apikey=hunter2&id=3")
    assert "hunter2" not in sp._redact("GET /x?api_key=hunter2")
    assert "hunter2" not in sp._redact('{"url": "/y?token=hunter2"}')
    # Non-secret params survive, or the output is useless.
    assert "getPrices" in sp._redact("/x?api_key=abc&action=getPrices", "abc")


def test_probe_capture_records_urls_and_bodies():
    install({"getServicesList": DAISY_SERVICES, "getPrices": DAISY_PRICES})
    calls: list = []
    sp.probe("https://unknown.example", "k", capture=calls)
    assert any("handler_api" in c["url"] for c in calls)
    assert all("detail" in c and "method" in c for c in calls)
    # Failures must be captured too — that's the diagnostic value.
    assert any(c["ok"] for c in calls) and any(not c["ok"] for c in calls)


def test_record_http_restores_the_original_transport():
    before = sp._request
    try:
        with sp.record_http([]):
            pass
    finally:
        pass
    assert sp._request is before, "record_http leaked its wrapper"


def test_record_http_restores_even_when_the_body_raises():
    install({})
    before = sp._request
    try:
        with sp.record_http([]):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert sp._request is before


def test_probe_snippet_is_valid_yaml_for_the_custom_block():
    import yaml

    snippet = sp.probe_config_snippet("newsite", "https://api.new.example", "sms-activate")
    parsed = yaml.safe_load(snippet)
    entry = parsed["sms_providers"]["custom"][0]
    assert entry["name"] == "newsite"
    assert entry["protocol"] == "sms-activate"
    assert entry["base_url"] == "https://api.new.example"
    # And the snippet must actually build a provider.
    built, problems = sp.build_providers(
        {"custom": [{**entry, "api_key": "k"}]}
    )
    assert problems == [] and "newsite" in {p.name for p in built}


# --- orchestration --------------------------------------------------------


def test_build_providers_respects_enabled_and_keys():
    with env():
        built, problems = sp.build_providers(
            {
                "5sim": {"enabled": True},
                "daisysms": {"enabled": True, "api_key": "k"},
                "herosms": {"enabled": False, "api_key": "k"},
                "smspool": {"api_key": ""},  # no key -> skipped
            }
        )
        assert {p.name for p in built} == {"5sim", "daisysms"}
        assert problems == []


def test_fivesim_enabled_without_any_config():
    with env():
        # The no-key provider must be usable with an empty config file.
        built, _ = sp.build_providers({})
        assert {p.name for p in built} == {"5sim"}


# --- provider registry / custom entries -----------------------------------


def test_known_activate_host_enabled_by_env():
    with env(TIGERSMS_API_KEY="abc"):
        built, _ = sp.build_providers({})
    tiger = {p.name: p for p in built}.get("tiger-sms")
    assert tiger is not None, "an exported key should be enough to enable a site"
    assert tiger.api_key == "abc"
    assert tiger.base_url == "https://api.tiger-sms.com"


def test_config_key_overrides_env():
    with env(TIGERSMS_API_KEY="from-env"):
        built, _ = sp.build_providers({"tiger-sms": {"api_key": "from-config"}})
    assert {p.name: p for p in built}["tiger-sms"].api_key == "from-config"


def test_known_host_can_be_disabled():
    with env(TIGERSMS_API_KEY="abc"):
        built, _ = sp.build_providers({"tiger-sms": {"enabled": False}})
    assert "tiger-sms" not in {p.name for p in built}


def test_builtin_supersedes_registry_alias():
    # DAISYSMS_API_KEY must yield ONE adapter — the built-in, which uses the
    # richer getPricesVerification action — not a duplicate generic one.
    with env(DAISYSMS_API_KEY="abc"):
        built, _ = sp.build_providers({})
    daisy = [p for p in built if "daisy" in p.name]
    assert len(daisy) == 1
    assert isinstance(daisy[0], sp.DaisySms)
    assert daisy[0].prices_action == "getPricesVerification"


def test_keyed_source_wins_over_public_duplicate():
    # 5sim's guest feed and its SMS-Activate-compatible endpoint read one pool,
    # so running both would double-count. The keyed one wins.
    with env(FIVESIM_ACTIVATE_API_KEY="k"):
        built, problems = sp.build_providers({})
    names = {p.name for p in built}
    assert "5sim-activate" in names
    assert "5sim" not in names, "the keyless duplicate should be skipped"
    assert any("5sim skipped" in p for p in problems), problems


def test_public_source_kept_when_no_keyed_duplicate():
    # Without the keyed variant, the keyless feed is the only way in and must
    # stay — it's the one provider that works with no account at all.
    with env():
        built, problems = sp.build_providers({})
    assert "5sim" in {p.name for p in built}
    assert not any("skipped" in p for p in problems)


def test_duplicate_resolution_survives_explicit_config():
    # Configuring the keyed one in config.yaml rather than the environment
    # must resolve the same way.
    with env():
        built, _ = sp.build_providers({"5sim-activate": {"api_key": "k"}})
    names = {p.name for p in built}
    assert "5sim-activate" in names and "5sim" not in names


def test_dead_provider_is_flagged():
    with env():
        _, problems = sp.build_providers({"sms-activate": {"api_key": "k"}})
        assert any("shut down" in p for p in problems)


def test_custom_provider_via_protocol():
    with env():
        built, problems = sp.build_providers(
            {
                "custom": [
                    {"name": "newsite", "protocol": "sms-activate",
                     "base_url": "https://new.example", "api_key": "k"}
                ]
            }
        )
        assert problems == []
        site = {p.name: p for p in built}["newsite"]
        assert isinstance(site, sp.SmsActivateCompat)
        assert site._endpoint() == "https://new.example/stubs/handler_api.php"


def test_custom_provider_bad_protocol_is_reported_not_fatal():
    with env():
        built, problems = sp.build_providers(
            {"custom": [{"name": "x", "protocol": "nonsense", "base_url": "https://x", "api_key": "k"}]}
        )
        assert any("unknown protocol" in p for p in problems)
        assert "x" not in {p.name for p in built}


def test_custom_provider_requires_base_url():
    with env():
        _, problems = sp.build_providers(
            {"custom": [{"name": "x", "protocol": "sms-activate", "api_key": "k"}]}
        )
        assert any("base_url" in p for p in problems)


def test_custom_entry_uses_handler_api_alias():
    with env():
        built, problems = sp.build_providers(
            {
                "custom": [
                    {"name": "y", "protocol": "handler_api",
                     "base_url": "https://y.example", "api_key": "k"}
                ]
            }
        )
        assert problems == [] and "y" in {p.name for p in built}


def test_every_registered_host_has_a_base_url_and_env():
    for name, spec in sp.KNOWN_ACTIVATE_HOSTS.items():
        assert spec["base_url"].startswith("https://"), name
        assert spec["env"].endswith("_API_KEY"), name


def test_all_registered_hosts_build():
    keys = {spec["env"]: "k" for spec in sp.KNOWN_ACTIVATE_HOSTS.values()}
    with env(**keys):
        built, problems = sp.build_providers({})
    names = {p.name for p in built}
    for name in sp.KNOWN_ACTIVATE_HOSTS:
        # aliases resolve to the built-in adapter under its own name
        expected = sp.ACTIVATE_ALIASES.get(name, name)
        assert expected in names, f"{name} did not build"
    # With every key set the keyed 5sim wins and the public feed is dropped;
    # that's the only message expected.
    assert all("skipped" in p for p in problems), problems
    assert "5sim" not in names


def test_to_usd_conversion():
    rub = sp.Offer("5sim", "walmart", "walmart", "usa", price=90.0, currency="RUB")
    usd = sp.Offer("tv", "walmart", "walmart", "USA", price=0.75, currency="USD")
    assert sp.to_usd(usd, None) == 0.75
    assert sp.to_usd(rub, None) is None  # no rate -> don't invent one
    assert abs(sp.to_usd(rub, 90.0) - 1.0) < 1e-9


def test_check_all_isolates_a_failing_provider():
    install({"guest/prices": FIVESIM_PRICES, "guest/products": FIVESIM_CATALOG})
    reports = sp.check_all([sp.FiveSim({}), sp.SmsPool({"api_key": "k"})])
    by_name = {r.provider: r for r in reports}
    assert by_name["5sim"].ok
    assert not by_name["smspool"].ok  # no fixture -> error, but no crash


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
