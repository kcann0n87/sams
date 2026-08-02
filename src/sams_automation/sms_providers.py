"""Check SMS-verification providers for Walmart number availability.

Every provider has a different API, but the question is always the same: "do
you have numbers that can receive a Walmart code right now, and what do they
cost?" So each adapter answers with the same `Offer` records and the CLI prints
one comparable table.

Service codes differ per provider ("walmart", "wm", "1023", ...) and get
renumbered over time, so nothing is hard-coded: each adapter pulls the
provider's own catalog and we keep the entries whose name looks like Walmart.
That way a provider re-labelling the service doesn't silently return "0 in
stock" — the honest answer of "not in the catalog" comes out instead.

Sam's Club is a Walmart subsidiary but is usually listed as its own service, so
the default search terms cover both.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

USER_AGENT = "sams-automation/0.1 (+sms availability check)"

# What counts as "the Walmart service" when scanning a provider's catalog.
DEFAULT_TERMS: tuple[str, ...] = ("walmart", "sams club", "sam's club", "samsclub")


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------


@dataclass
class Offer:
    """One buyable combination: a service, in a country, on an operator."""

    provider: str
    service: str  # human name as the provider spells it
    service_code: str  # what you'd pass to their order endpoint
    country: str
    operator: str = "any"
    price: float | None = None
    currency: str = "USD"
    count: int | None = None  # numbers in stock; None = provider doesn't say
    success_rate: float | None = None  # percent, when the provider reports it

    @property
    def in_stock(self) -> bool:
        """True unless the provider explicitly told us the pool is empty."""
        return self.count is None or self.count > 0


@dataclass
class ProviderReport:
    provider: str
    ok: bool = False
    offers: list[Offer] = field(default_factory=list)
    error: str | None = None
    notes: list[str] = field(default_factory=list)
    raw: Any = None  # populated only when the caller asks for it

    @property
    def total_stock(self) -> int | None:
        counts = [o.count for o in self.offers if o.count is not None]
        return sum(counts) if counts else None


class ProviderError(RuntimeError):
    """A provider answered, but not with something we can use."""


# --------------------------------------------------------------------------
# HTTP + matching helpers
# --------------------------------------------------------------------------


def _request(
    url: str,
    *,
    method: str = "GET",
    params: dict[str, Any] | None = None,
    form: dict[str, Any] | None = None,
    json_body: Any = None,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
) -> str:
    """Do one HTTP call and return the body as text.

    Kept on urllib so the package picks up no new dependencies. HTTP errors
    still have their body read, because several of these APIs report real
    problems ("BAD_KEY", "NO_BALANCE") with a non-2xx status.
    """
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"

    data: bytes | None = None
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    elif json_body is not None:
        data = json.dumps(json_body).encode()
        hdrs["Content-Type"] = "application/json"
    if headers:
        hdrs.update(headers)

    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace").strip()
        snippet = body[:200] if body else "(empty body)"
        raise ProviderError(f"HTTP {e.code}: {snippet}") from None
    except urllib.error.URLError as e:
        raise ProviderError(f"network error: {e.reason}") from None


def _request_json(url: str, **kw: Any) -> Any:
    """_request() plus JSON parsing, with the text kept in the error message.

    These APIs answer errors as bare strings (`BAD_KEY`) rather than JSON, so a
    parse failure is usually the actual error worth showing.
    """
    text = _request(url, **kw).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise ProviderError(f"expected JSON, got: {text[:200] or '(empty)'}") from None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _matches(name: str, terms: Sequence[str]) -> bool:
    """Loose name match: 'Sam's Club' and 'sams_club' both hit 'sams club'."""
    n = _norm(name)
    return any(_norm(t) in n for t in terms if t)


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _first(mapping: Any, *keys: str) -> Any:
    """First present key in a dict — these APIs disagree on casing/naming."""
    if not isinstance(mapping, dict):
        return None
    for k in keys:
        if k in mapping and mapping[k] not in (None, ""):
            return mapping[k]
    return None


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------


class Provider:
    """Base adapter. Subclasses implement `fetch`."""

    name = "provider"
    needs_key = True
    signup_url = ""
    currency = "USD"

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self.settings = settings or {}
        # Instance-level so one adapter class can back several sites: the
        # handler_api protocol alone is spoken by a dozen different providers.
        custom_name = str(self.settings.get("name") or "").strip()
        if custom_name:
            self.name = custom_name

    # Environment variable this provider's key is conventionally exported as,
    # so an existing shell setup works without re-entering keys in config.
    env_key = ""
    env_username = ""

    # -- config helpers ----------------------------------------------------
    @property
    def api_key(self) -> str:
        import os

        configured = str(self.settings.get("api_key") or "").strip()
        if configured:
            return configured
        return os.environ.get(self.env_key, "").strip() if self.env_key else ""

    @property
    def base_url(self) -> str:
        return str(self.settings.get("base_url") or self.DEFAULT_BASE).rstrip("/")

    DEFAULT_BASE = ""

    def enabled(self) -> bool:
        if self.settings.get("enabled") is False:
            return False
        return bool(self.api_key) or not self.needs_key

    # -- the actual work ---------------------------------------------------
    def fetch(self, terms: Sequence[str], us_only: bool) -> tuple[list[Offer], list[str]]:
        raise NotImplementedError

    def check(
        self, terms: Sequence[str] = DEFAULT_TERMS, *, us_only: bool = True
    ) -> ProviderReport:
        report = ProviderReport(provider=self.name)
        if self.needs_key and not self.api_key:
            report.error = f"no API key configured (sign up: {self.signup_url})"
            return report
        try:
            offers, notes = self.fetch(terms, us_only)
        except ProviderError as e:
            report.error = str(e)
            return report
        except Exception as e:  # adapter bug or a shape we didn't expect
            report.error = f"{type(e).__name__}: {e}"
            return report
        report.ok = True
        report.offers = offers
        report.notes = notes
        if not offers:
            report.notes.append("no Walmart-like service found in this catalog")
        return report


class FiveSim(Provider):
    """5sim.net — the only one here whose price/stock feed needs no account.

    `/v1/guest/prices?product=<name>` returns
    `{product: {country: {operator: {cost, count, rate}}}}`. Prices are in RUB.
    """

    name = "5sim"
    needs_key = False
    signup_url = "https://5sim.net/"
    currency = "RUB"
    DEFAULT_BASE = "https://5sim.net"

    def fetch(self, terms: Sequence[str], us_only: bool) -> tuple[list[Offer], list[str]]:
        notes: list[str] = []
        products = self._discover_products(terms, notes)
        if not products:
            return [], notes

        offers: list[Offer] = []
        for product in sorted(products):
            data = _request_json(
                f"{self.base_url}/v1/guest/prices", params={"product": product}
            )
            # Answers are keyed by product even though we asked for one.
            for country, operators in (data.get(product) or {}).items():
                if us_only and _norm(country) not in ("usa", "unitedstates", "us"):
                    continue
                if not isinstance(operators, dict):
                    continue
                for operator, info in operators.items():
                    if not isinstance(info, dict):
                        continue
                    offers.append(
                        Offer(
                            provider=self.name,
                            service=product,
                            service_code=product,
                            country=country,
                            operator=operator,
                            price=_as_float(_first(info, "cost", "price")),
                            currency=self.currency,
                            count=_as_int(_first(info, "count", "qty")),
                            success_rate=_as_float(info.get("rate")),
                        )
                    )
        return offers, notes

    def _discover_products(self, terms: Sequence[str], notes: list[str]) -> set[str]:
        """Find the product slug(s) that look like Walmart.

        The guest catalog is per country/operator, so ask the US list first and
        fall back to a direct probe if that endpoint is unavailable.
        """
        found: set[str] = set()
        try:
            catalog = _request_json(f"{self.base_url}/v1/guest/products/usa/any")
            if isinstance(catalog, dict):
                found = {p for p in catalog if _matches(p, terms)}
        except ProviderError as e:
            notes.append(f"catalog lookup failed ({e}); probing 'walmart' directly")

        if not found:
            # Probe the obvious slug — an unknown product returns {} not an error.
            probe = _request_json(
                f"{self.base_url}/v1/guest/prices", params={"product": "walmart"}
            )
            if isinstance(probe, dict) and probe.get("walmart"):
                found = {"walmart"}
        return found


class TextVerified(Provider):
    """textverified.com — US non-VoIP numbers, API v2.

    Auth is a bearer token minted from your API key + account username; the
    token is short-lived, so we mint one per run.
    """

    name = "textverified"
    signup_url = "https://www.textverified.com/"
    env_key = "TEXTVERIFIED_API_KEY"
    env_username = "TEXTVERIFIED_USERNAME"
    DEFAULT_BASE = "https://www.textverified.com/api/pub/v2"

    @property
    def username(self) -> str:
        import os

        configured = str(self.settings.get("username") or "").strip()
        return configured or os.environ.get(self.env_username, "").strip()

    def fetch(self, terms: Sequence[str], us_only: bool) -> tuple[list[Offer], list[str]]:
        if not self.username:
            raise ProviderError("username required (the email you log in with)")

        token = self._auth()
        auth = {"Authorization": f"Bearer {token}"}
        notes = ["US non-VoIP numbers only"]

        services = _request_json(
            f"{self.base_url}/services",
            params={"numberType": "mobile", "reservationType": "verification"},
            headers=auth,
        )
        if isinstance(services, dict):  # some responses wrap the list
            services = services.get("data") or services.get("services") or []

        offers: list[Offer] = []
        for svc in services or []:
            name = _first(svc, "serviceName", "normalizedName", "name") or ""
            if not _matches(name, terms):
                continue
            price = _as_float(_first(svc, "cost", "price"))
            if price is None:
                price = self._price(name, auth, notes)
            offers.append(
                Offer(
                    provider=self.name,
                    service=str(name),
                    service_code=str(_first(svc, "serviceName", "name") or name),
                    country="USA",
                    operator=str(_first(svc, "capability") or "sms"),
                    price=price,
                    currency="USD",
                    # v2 exposes no stock count; a listed service is orderable.
                    count=None,
                )
            )
        return offers, notes

    def _auth(self) -> str:
        data = _request_json(
            f"{self.base_url}/auth",
            method="POST",
            headers={"X-API-KEY": self.api_key, "X-API-USERNAME": self.username},
        )
        token = _first(data, "token", "bearer_token", "bearerToken")
        if not token:
            raise ProviderError(f"auth returned no token: {str(data)[:200]}")
        return str(token)

    def _price(self, service_name: str, auth: dict[str, str], notes: list[str]) -> float | None:
        """Per-service price lookup, for when the catalog omits `cost`."""
        try:
            data = _request_json(
                f"{self.base_url}/pricing/verifications",
                params={
                    "serviceName": service_name,
                    "areaCode": "false",
                    "carrier": "false",
                    "numberType": "mobile",
                    "capability": "sms",
                },
                headers=auth,
            )
        except ProviderError as e:
            notes.append(f"price lookup for {service_name} failed: {e}")
            return None
        return _as_float(_first(data, "price", "cost"))


class SmsActivateCompat(Provider):
    """Shared adapter for the `stubs/handler_api.php` protocol.

    SMS-Activate defined it; after that service shut down (2025-12-29) the same
    protocol is what DaisySMS, HeroSMS and several others speak. Two calls:
    `getServicesList` for names, `getPrices` for cost + stock, both keyed by a
    numeric country id (187 = USA).
    """

    us_country_id = "187"
    prices_action = "getPrices"

    def fetch(self, terms: Sequence[str], us_only: bool) -> tuple[list[Offer], list[str]]:
        notes: list[str] = []
        names = self._service_names(notes)
        prices = self._prices(us_only)

        offers: list[Offer] = []
        for country, services in prices.items():
            if not isinstance(services, dict):
                continue
            if us_only and str(country) != str(self.us_country_id):
                continue
            for code, info in services.items():
                if not isinstance(info, dict):
                    continue
                # Match on the catalog name when we have one, else the code
                # itself (some deployments use "walmart" as the code).
                label = names.get(str(code)) or _first(info, "name", "title") or str(code)
                if not _matches(label, terms) and not _matches(str(code), terms):
                    continue
                offers.append(
                    Offer(
                        provider=self.name,
                        service=str(label),
                        service_code=str(code),
                        country=self._country_label(country),
                        price=_as_float(_first(info, "cost", "price", "retail_price")),
                        currency=self.currency,
                        count=_as_int(_first(info, "count", "quantity", "physicalCount")),
                    )
                )
        if not offers and not names:
            notes.append("service-name catalog unavailable; matched on codes only")
        return offers, notes

    def _endpoint(self) -> str:
        return f"{self.base_url}/stubs/handler_api.php"

    def _prices(self, us_only: bool) -> dict[str, Any]:
        params = {"api_key": self.api_key, "action": self.prices_action}
        if us_only:
            params["country"] = self.us_country_id
        data = _request_json(self._endpoint(), params=params)
        if not isinstance(data, dict):
            raise ProviderError(f"unexpected prices payload: {str(data)[:200]}")
        # Some deployments answer un-nested when a country is pinned.
        if data and all(not isinstance(v, dict) or "cost" in v for v in data.values()):
            return {self.us_country_id: data}
        return data

    def _service_names(self, notes: list[str]) -> dict[str, str]:
        """code -> display name. Best-effort: pricing still works without it."""
        try:
            data = _request_json(
                self._endpoint(),
                params={
                    "api_key": self.api_key,
                    "action": "getServicesList",
                    "country": self.us_country_id,
                },
            )
        except ProviderError as e:
            notes.append(f"getServicesList unavailable ({e})")
            return {}
        services = data.get("services") if isinstance(data, dict) else data
        names: dict[str, str] = {}
        for svc in services or []:
            if isinstance(svc, dict):
                code = _first(svc, "code", "service", "id")
                name = _first(svc, "name", "title", "rus", "eng")
                if code and name:
                    names[str(code)] = str(name)
        return names

    def _country_label(self, country: Any) -> str:
        return "USA" if str(country) == str(self.us_country_id) else str(country)


class DaisySms(SmsActivateCompat):
    """daisysms.com — US-only, non-VoIP, handler_api protocol.

    Uses `getPricesVerification`, which unlike plain `getPrices` includes the
    service display name in the payload.
    """

    name = "daisysms"
    signup_url = "https://daisysms.com/"
    env_key = "DAISYSMS_API_KEY"
    DEFAULT_BASE = "https://daisysms.com"
    prices_action = "getPricesVerification"

    def fetch(self, terms: Sequence[str], us_only: bool) -> tuple[list[Offer], list[str]]:
        offers, notes = super().fetch(terms, us_only)
        notes.append("US non-VoIP; charged only when a code actually arrives")
        return offers, notes


class HeroSms(SmsActivateCompat):
    """hero-sms.com — inherited SMS-Activate's stack after its 2025 shutdown."""

    name = "herosms"
    signup_url = "https://hero-sms.com/"
    env_key = "HEROSMS_API_KEY"
    DEFAULT_BASE = "https://hero-sms.com"


class SmsPool(Provider):
    """smspool.net — form-encoded JSON API, numeric service + country ids."""

    name = "smspool"
    signup_url = "https://www.smspool.net/"
    env_key = "SMSPOOL_API_KEY"
    DEFAULT_BASE = "https://api.smspool.net"

    def fetch(self, terms: Sequence[str], us_only: bool) -> tuple[list[Offer], list[str]]:
        notes: list[str] = []
        services = _request_json(
            f"{self.base_url}/service/retrieve_all", method="POST", form={"key": self.api_key}
        )
        matches = [
            s
            for s in (services or [])
            if isinstance(s, dict) and _matches(str(_first(s, "name", "service") or ""), terms)
        ]
        if not matches:
            return [], notes

        countries = self._countries(us_only, notes)
        offers: list[Offer] = []
        for svc in matches:
            svc_id = _first(svc, "ID", "id", "service_id")
            svc_name = str(_first(svc, "name", "service") or svc_id)
            for country_id, country_name in countries:
                price_info = self._price(svc_id, country_id, notes)
                if price_info is None:
                    continue
                offers.append(
                    Offer(
                        provider=self.name,
                        service=svc_name,
                        service_code=str(svc_id),
                        country=country_name,
                        price=_as_float(_first(price_info, "price", "cost")),
                        currency="USD",
                        count=_as_int(_first(price_info, "amount", "stock", "count", "pool")),
                        success_rate=_as_float(_first(price_info, "success_rate", "rate")),
                    )
                )
        return offers, notes

    def _countries(self, us_only: bool, notes: list[str]) -> list[tuple[Any, str]]:
        try:
            data = _request_json(
                f"{self.base_url}/country/retrieve_all",
                method="POST",
                form={"key": self.api_key},
            )
        except ProviderError as e:
            notes.append(f"country list unavailable ({e}); assuming US id=1")
            return [("1", "United States")]

        out: list[tuple[Any, str]] = []
        for c in data or []:
            if not isinstance(c, dict):
                continue
            cid = _first(c, "ID", "id", "country_id")
            cname = str(_first(c, "name", "country") or cid)
            if us_only and not _matches(cname, ("united states", "usa", "us")):
                continue
            out.append((cid, cname))
        return out or [("1", "United States")]

    def _price(self, service_id: Any, country_id: Any, notes: list[str]) -> dict[str, Any] | None:
        try:
            data = _request_json(
                f"{self.base_url}/request/price",
                method="POST",
                form={"key": self.api_key, "service": service_id, "country": country_id},
            )
        except ProviderError as e:
            notes.append(f"price lookup failed for service={service_id}: {e}")
            return None
        return data if isinstance(data, dict) else None


PROVIDERS: dict[str, type[Provider]] = {
    cls.name: cls for cls in (FiveSim, TextVerified, DaisySms, HeroSms, SmsPool)
}

# Adding a site usually needs no code — most resellers are clones of one of
# these four APIs, so naming the protocol and base URL in config is enough.
# `sms-activate` is by far the most common: TigerSMS, SMS-Man, Grizzly, SMSHub
# and others all speak `stubs/handler_api.php`.
PROTOCOLS: dict[str, type[Provider]] = {
    "sms-activate": SmsActivateCompat,
    "handler_api": SmsActivateCompat,  # alias — same thing, commoner name
    "5sim": FiveSim,
    "smspool": SmsPool,
    "textverified": TextVerified,
}


class UnknownProtocol(ValueError):
    pass


# Sites known to speak the handler_api ("activate") protocol, with the env var
# each one's key is conventionally exported as. Registering them here means a
# key is all you need — no base_url, no code.
#
# Base URLs are the documented API hosts but these resellers do move domains;
# every one is overridable via `base_url` in config if a call starts 404ing.
KNOWN_ACTIVATE_HOSTS: dict[str, dict[str, str]] = {
    "smsbower": {"base_url": "https://smsbower.online", "env": "SMSBOWER_API_KEY"},
    "sms-activation": {
        "base_url": "https://sms-activation-service.com",
        "env": "SMS_ACTIVATION_SERVICE_API_KEY",
    },
    "5sim-activate": {"base_url": "https://api1.5sim.net", "env": "FIVESIM_ACTIVATE_API_KEY"},
    "tiger-sms": {"base_url": "https://api.tiger-sms.com", "env": "TIGERSMS_API_KEY"},
    "hero-sms": {"base_url": "https://hero-sms.com", "env": "HEROSMS_API_KEY"},
    "grizzly-sms": {"base_url": "https://api.grizzlysms.com", "env": "GRIZZLYSMS_API_KEY"},
    "daisy-sms": {"base_url": "https://daisysms.com", "env": "DAISYSMS_API_KEY"},
    "sms-man": {"base_url": "https://api.sms-man.com", "env": "SMSMAN_API_KEY"},
    "smshub": {"base_url": "https://smshub.org", "env": "SMSHUB_API_KEY"},
    "sms-acktiv": {"base_url": "https://sms-acktiv.ru", "env": "SMS_ACKTIV_API_KEY"},
    "simsms": {"base_url": "https://simsms.org", "env": "SIMSMS_API_KEY"},
}

# Registry names already covered by a richer built-in adapter above (the
# built-ins use better endpoints, e.g. DaisySMS's getPricesVerification, which
# unlike plain getPrices carries the service display name).
ACTIVATE_ALIASES: dict[str, str] = {"daisy-sms": "daisysms", "hero-sms": "herosms"}

# Gone, but still the first hit in most search results and tutorials.
DEAD_PROVIDERS = {"sms-activate": "shut down 2025-12-29; infrastructure moved to hero-sms"}


def build_known_activate(name: str, overrides: dict[str, Any] | None = None) -> Provider | None:
    """Build a registered handler_api site, taking its key from env or config.

    Returns None when no key is available anywhere, so callers can just skip it.
    """
    import os

    spec = KNOWN_ACTIVATE_HOSTS[name]
    overrides = overrides or {}
    if overrides.get("enabled") is False:
        return None
    api_key = str(overrides.get("api_key") or os.environ.get(spec["env"], "")).strip()
    if not api_key:
        return None
    return SmsActivateCompat(
        {
            "name": name,
            "api_key": api_key,
            "base_url": overrides.get("base_url") or spec["base_url"],
        }
    )


def build_custom(entry: dict[str, Any]) -> Provider:
    """Build one adapter from a `custom:` config entry."""
    name = str(entry.get("name") or "").strip()
    protocol = str(entry.get("protocol") or "sms-activate").strip().lower()
    if not name:
        raise UnknownProtocol("each custom provider needs a `name`")
    cls = PROTOCOLS.get(protocol)
    if cls is None:
        raise UnknownProtocol(
            f"{name}: unknown protocol '{protocol}' "
            f"(known: {', '.join(sorted(PROTOCOLS))})"
        )
    if not entry.get("base_url") and not cls.DEFAULT_BASE:
        raise UnknownProtocol(f"{name}: `base_url` is required for protocol '{protocol}'")
    return cls(entry)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


@dataclass
class ProbeResult:
    """What one protocol guess did when pointed at an unknown host."""

    protocol: str
    ok: bool
    detail: str
    walmart: list[Offer] = field(default_factory=list)
    service_count: int | None = None

    @property
    def sells_walmart(self) -> bool:
        return bool(self.walmart)


def probe(
    base_url: str,
    api_key: str = "",
    name: str = "probe",
    terms: Sequence[str] = DEFAULT_TERMS,
) -> list[ProbeResult]:
    """Work out which API a host speaks by trying each protocol against it.

    Resellers rarely say "we're SMS-Activate compatible" — you're expected to
    already know. With a dozen keys across a dozen sites that's a lot of
    guessing, so try each one and report what actually parsed.

    Results come back ordered best-first: protocols that found a Walmart
    service, then ones that authenticated but didn't, then failures.
    """
    results: list[ProbeResult] = []
    for protocol, cls in PROTOCOLS.items():
        if protocol == "handler_api":
            continue  # alias of sms-activate; probing both is just noise
        provider = cls({"name": name, "base_url": base_url, "api_key": api_key})
        report = provider.check(terms)
        if report.ok:
            results.append(
                ProbeResult(
                    protocol=protocol,
                    ok=True,
                    detail=(
                        f"{len(report.offers)} Walmart offer(s)"
                        if report.offers
                        else "responded, but lists no Walmart service"
                    ),
                    walmart=report.offers,
                )
            )
        else:
            results.append(
                ProbeResult(protocol=protocol, ok=False, detail=report.error or "failed")
            )

    results.sort(key=lambda r: (not r.sells_walmart, not r.ok, r.protocol))
    return results


def probe_config_snippet(name: str, base_url: str, protocol: str) -> str:
    """The exact YAML to paste once a probe identifies a host."""
    return (
        "sms_providers:\n"
        "  custom:\n"
        f"    - name: \"{name}\"\n"
        f"      protocol: \"{protocol}\"\n"
        f"      base_url: \"{base_url}\"\n"
        "      api_key: \"...\"\n"
    )


def build_providers(settings: dict[str, Any] | None) -> tuple[list[Provider], list[str]]:
    """Instantiate every provider that's configured and switched on.

    Returns the adapters plus any config problems, so one bad `custom:` entry
    reports itself instead of silently dropping out of the run.
    """
    settings = settings or {}
    out: list[Provider] = []
    problems: list[str] = []

    for name, cls in PROVIDERS.items():
        provider = cls(settings.get(name) or {})
        if provider.enabled():
            out.append(provider)

    seen = {p.name for p in out}

    # Registered handler_api sites: enabled by the mere presence of a key,
    # in config or in the environment.
    for name in KNOWN_ACTIVATE_HOSTS:
        if name in seen or ACTIVATE_ALIASES.get(name) in seen:
            continue
        provider = build_known_activate(name, settings.get(name) or {})
        if provider is not None:
            seen.add(name)
            out.append(provider)

    for name, reason in DEAD_PROVIDERS.items():
        if settings.get(name):
            problems.append(f"{name}: {reason}")

    for entry in settings.get("custom") or []:
        if not isinstance(entry, dict):
            problems.append(f"custom entry must be a mapping, got: {entry!r}")
            continue
        try:
            provider = build_custom(entry)
        except UnknownProtocol as e:
            problems.append(str(e))
            continue
        if provider.name in seen:
            problems.append(f"duplicate provider name '{provider.name}' — skipped")
            continue
        if provider.enabled():
            seen.add(provider.name)
            out.append(provider)
    return out, problems


def check_all(
    providers: Iterable[Provider],
    terms: Sequence[str] = DEFAULT_TERMS,
    *,
    us_only: bool = True,
    max_workers: int = 5,
) -> list[ProviderReport]:
    """Query every provider concurrently — they're independent and slow."""
    providers = list(providers)
    if not providers:
        return []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(p.check, terms, us_only=us_only) for p in providers]
        return [f.result() for f in futures]


def to_usd(offer: Offer, rub_per_usd: float | None) -> float | None:
    """Best-effort USD figure so a mixed-currency table can be sorted."""
    if offer.price is None:
        return None
    if offer.currency == "USD":
        return offer.price
    if offer.currency == "RUB" and rub_per_usd:
        return offer.price / rub_per_usd
    return None
