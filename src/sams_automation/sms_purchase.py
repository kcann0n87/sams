"""Buy a verification number and wait for its code.

The checker in `sms_providers` is read-only. This is the half that spends
money, so it is deliberately harder to fire:

- disabled unless `purchasing.enabled` is true in config
- `dry_run` is the default, and reports what it *would* buy
- a per-number price ceiling and a per-run spend cap, both enforced before any
  order is placed, not after
- every order is recorded to `purchases.csv` so a crashed run can't lose track
  of a number you've already paid for

Cancelling an unused number refunds it on most providers, so anything bought
but un-coded is released on the way out rather than left to expire.
"""

from __future__ import annotations

import csv
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from . import sms_providers as _sms
from .sms_providers import (
    DaisySms,
    FiveSim,
    Offer,
    Provider,
    ProviderError,
    SmsActivateCompat,
    SmsPool,
    TextVerified,
)

# Reached through the module, never imported by value: tests stub
# sms_providers._request, and record_http() swaps it to capture traffic. A
# by-value import would silently keep the original and make real calls.
_as_float = _sms._as_float
_first = _sms._first


def _request(*a, **kw):
    return _sms._request(*a, **kw)


def _request_json(*a, **kw):
    return _sms._request_json(*a, **kw)

PURCHASE_LOG = "purchases.csv"


class PurchaseRefused(RuntimeError):
    """A guard rail stopped the order. Never raised by a provider."""


@dataclass
class Purchase:
    """One bought number, before or after its code arrives."""

    provider: str
    order_id: str
    phone: str
    price: float | None
    currency: str
    service_code: str
    code: str | None = None
    cancelled: bool = False

    @property
    def digits(self) -> str:
        """Bare digits — providers return +1XXXXXXXXXX, 1XXXXXXXXXX or raw."""
        return "".join(c for c in self.phone if c.isdigit())

    @property
    def national(self) -> str:
        """10-digit US form, which is what a signup form usually wants."""
        d = self.digits
        return d[-10:] if len(d) >= 10 else d


@dataclass
class Budget:
    """Spend limits, checked before every order."""

    max_price_usd: float = 1.00
    max_total_usd: float = 5.00
    spent: float = 0.0

    def check(self, price_usd: float | None, provider: str) -> None:
        if price_usd is None:
            raise PurchaseRefused(
                f"{provider}: no price reported — refusing to buy blind"
            )
        if price_usd > self.max_price_usd:
            raise PurchaseRefused(
                f"{provider}: {price_usd:.2f} USD exceeds the per-number cap "
                f"({self.max_price_usd:.2f})"
            )
        if self.spent + price_usd > self.max_total_usd:
            raise PurchaseRefused(
                f"{provider}: {price_usd:.2f} USD would take this run to "
                f"{self.spent + price_usd:.2f}, over the cap ({self.max_total_usd:.2f})"
            )

    def record(self, price_usd: float) -> None:
        self.spent += price_usd

    def refund(self, price_usd: float) -> None:
        """Give budget back for a number that was cancelled.

        Most of these providers only charge once a code actually lands, so a
        cancelled attempt costs nothing. Without this, cycling through five
        failing providers would exhaust the run cap having spent nothing.
        """
        self.spent = max(0.0, self.spent - price_usd)


# --------------------------------------------------------------------------
# Per-protocol buy / poll
# --------------------------------------------------------------------------


class Buyer:
    """Order a number and read its code, for one provider protocol."""

    def __init__(self, provider: Provider) -> None:
        self.provider = provider

    @property
    def name(self) -> str:
        return self.provider.name

    def buy(self, offer: Offer) -> Purchase:
        raise NotImplementedError

    def poll(self, purchase: Purchase) -> str | None:
        """Return the code if it has arrived, else None."""
        raise NotImplementedError

    def cancel(self, purchase: Purchase) -> None:
        """Release an unused number. Best effort — never raises."""


class ActivateBuyer(Buyer):
    """The handler_api family: DaisySMS, HeroSMS, tiger-sms, smsbower, ..."""

    def _endpoint(self) -> str:
        return f"{self.provider.base_url}/stubs/handler_api.php"

    def _call(self, action: str, **params: Any) -> str:
        return _request(
            self._endpoint(),
            params={"api_key": self.provider.api_key, "action": action, **params},
        ).strip()

    def buy(self, offer: Offer) -> Purchase:
        # ACCESS_NUMBER:<id>:<phone>
        body = self._call("getNumber", service=offer.service_code, country="187")
        parts = body.split(":")
        if parts[0] != "ACCESS_NUMBER" or len(parts) < 3:
            raise ProviderError(f"{self.name}: unexpected getNumber reply: {body[:120]}")
        return Purchase(
            provider=self.name,
            order_id=parts[1],
            phone=parts[2],
            price=offer.price,
            currency=offer.currency,
            service_code=offer.service_code,
        )

    def poll(self, purchase: Purchase) -> str | None:
        body = self._call("getStatus", id=purchase.order_id)
        if body.startswith("STATUS_OK:"):
            return body.split(":", 1)[1].strip()
        if body in ("STATUS_WAIT_CODE", "STATUS_WAIT_RETRY"):
            return None
        if body == "STATUS_CANCEL":
            raise ProviderError(f"{self.name}: order {purchase.order_id} was cancelled")
        return None

    def cancel(self, purchase: Purchase) -> None:
        try:
            self._call("setStatus", id=purchase.order_id, status="8")
            purchase.cancelled = True
        except ProviderError:
            pass


class FiveSimBuyer(Buyer):
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.provider.api_key}"}

    def buy(self, offer: Offer) -> Purchase:
        data = _request_json(
            f"{self.provider.base_url}/v1/user/buy/activation/"
            f"{offer.country}/{offer.operator}/{offer.service_code}",
            headers=self._headers(),
        )
        order_id = _first(data, "id")
        phone = _first(data, "phone")
        if not order_id or not phone:
            raise ProviderError(f"{self.name}: unexpected buy reply: {str(data)[:120]}")
        return Purchase(
            provider=self.name,
            order_id=str(order_id),
            phone=str(phone),
            price=_as_float(_first(data, "price")) or offer.price,
            currency=offer.currency,
            service_code=offer.service_code,
        )

    def poll(self, purchase: Purchase) -> str | None:
        data = _request_json(
            f"{self.provider.base_url}/v1/user/check/{purchase.order_id}",
            headers=self._headers(),
        )
        for sms in (data or {}).get("sms") or []:
            code = _first(sms, "code", "text")
            if code:
                return str(code)
        return None

    def cancel(self, purchase: Purchase) -> None:
        try:
            _request(
                f"{self.provider.base_url}/v1/user/cancel/{purchase.order_id}",
                headers=self._headers(),
            )
            purchase.cancelled = True
        except ProviderError:
            pass


class SmsPoolBuyer(Buyer):
    def buy(self, offer: Offer) -> Purchase:
        data = _request_json(
            f"{self.provider.base_url}/purchase/sms",
            method="POST",
            form={
                "key": self.provider.api_key,
                "country": offer.country,
                "service": offer.service_code,
            },
        )
        order_id = _first(data, "order_id", "orderid", "id")
        phone = _first(data, "number", "phonenumber", "phone")
        if not order_id or not phone:
            raise ProviderError(f"{self.name}: unexpected purchase reply: {str(data)[:120]}")
        return Purchase(
            provider=self.name,
            order_id=str(order_id),
            phone=str(phone),
            price=_as_float(_first(data, "cost", "price")) or offer.price,
            currency="USD",
            service_code=offer.service_code,
        )

    def poll(self, purchase: Purchase) -> str | None:
        data = _request_json(
            f"{self.provider.base_url}/sms/check",
            method="POST",
            form={"key": self.provider.api_key, "orderid": purchase.order_id},
        )
        # status 3 == completed on SMSPool; the code lands in `sms`.
        code = _first(data, "sms", "code")
        return str(code) if code else None

    def cancel(self, purchase: Purchase) -> None:
        try:
            _request(
                f"{self.provider.base_url}/sms/cancel",
                method="POST",
                form={"key": self.provider.api_key, "orderid": purchase.order_id},
            )
            purchase.cancelled = True
        except ProviderError:
            pass


class TextVerifiedBuyer(Buyer):
    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.provider._auth()}"}  # type: ignore[attr-defined]

    def buy(self, offer: Offer) -> Purchase:
        auth = self._auth()
        data = _request_json(
            f"{self.provider.base_url}/verifications",
            method="POST",
            json_body={"serviceName": offer.service_code, "capability": "sms"},
            headers=auth,
        )
        order_id = _first(data, "id", "href")
        if not order_id:
            raise ProviderError(f"{self.name}: unexpected reply: {str(data)[:120]}")
        detail = _request_json(
            f"{self.provider.base_url}/verifications/{order_id}", headers=auth
        )
        return Purchase(
            provider=self.name,
            order_id=str(order_id),
            phone=str(_first(detail, "number", "phoneNumber") or ""),
            price=offer.price,
            currency="USD",
            service_code=offer.service_code,
        )

    def poll(self, purchase: Purchase) -> str | None:
        data = _request_json(
            f"{self.provider.base_url}/verifications/{purchase.order_id}",
            headers=self._auth(),
        )
        code = _first(data, "code", "smsCode")
        return str(code) if code else None

    def cancel(self, purchase: Purchase) -> None:
        try:
            _request(
                f"{self.provider.base_url}/verifications/{purchase.order_id}/cancel",
                method="POST",
                headers=self._auth(),
            )
            purchase.cancelled = True
        except ProviderError:
            pass


def buyer_for(provider: Provider) -> Buyer:
    """Pick the buy/poll implementation matching a provider's protocol."""
    if isinstance(provider, FiveSim):
        return FiveSimBuyer(provider)
    if isinstance(provider, SmsPool):
        return SmsPoolBuyer(provider)
    if isinstance(provider, TextVerified):
        return TextVerifiedBuyer(provider)
    if isinstance(provider, (DaisySms, SmsActivateCompat)):
        return ActivateBuyer(provider)
    raise PurchaseRefused(f"{provider.name}: no purchase support for this protocol")


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


@dataclass
class PurchaseConfig:
    enabled: bool = False
    dry_run: bool = True
    max_price_usd: float = 1.00
    max_total_usd: float = 5.00
    code_timeout_seconds: int = 180
    poll_interval_seconds: int = 5
    max_attempts: int = 4
    log_file: str = PURCHASE_LOG


def acquire(
    provider: Provider,
    offer: Offer,
    cfg: PurchaseConfig,
    budget: Budget,
    *,
    price_usd: float | None = None,
    sleep=time.sleep,
    now=time.monotonic,
) -> Purchase:
    """Buy `offer` and wait for its code. Raises rather than overspending.

    An unused number is cancelled on timeout — most providers refund it, and
    leaving it to expire wastes the money either way.
    """
    if not cfg.enabled:
        raise PurchaseRefused(
            "purchasing is off — set purchasing.enabled: true in config to allow it"
        )

    price_usd = price_usd if price_usd is not None else (
        offer.price if offer.currency == "USD" else None
    )
    budget.check(price_usd, provider.name)

    if cfg.dry_run:
        raise PurchaseRefused(
            f"dry run — would buy {offer.service} from {provider.name} "
            f"at {price_usd:.2f} USD. Set purchasing.dry_run: false to do it."
        )

    buyer = buyer_for(provider)
    purchase = buyer.buy(offer)
    budget.record(price_usd or 0.0)
    _log_purchase(cfg.log_file, purchase)

    deadline = now() + cfg.code_timeout_seconds
    while now() < deadline:
        code = buyer.poll(purchase)
        if code:
            purchase.code = code
            _log_purchase(cfg.log_file, purchase)
            return purchase
        sleep(cfg.poll_interval_seconds)

    buyer.cancel(purchase)
    _log_purchase(cfg.log_file, purchase)
    raise ProviderError(
        f"{provider.name}: no code within {cfg.code_timeout_seconds}s "
        f"for {purchase.phone} (order {purchase.order_id}, cancelled)"
    )


def _log_purchase(path: str | Path, purchase: Purchase) -> None:
    """Append to the purchase ledger. A number you paid for must not be lost."""
    path = Path(path)
    exists = path.exists()
    try:
        with path.open("a", newline="") as fh:
            writer = csv.writer(fh)
            if not exists:
                writer.writerow(
                    ["provider", "order_id", "phone", "price", "currency",
                     "service_code", "code", "cancelled"]
                )
            writer.writerow(
                [purchase.provider, purchase.order_id, purchase.phone,
                 purchase.price, purchase.currency, purchase.service_code,
                 purchase.code or "", "yes" if purchase.cancelled else ""]
            )
    except OSError:
        pass  # never lose a bought number to a logging failure


def load_purchase_config(raw: dict[str, Any] | None) -> PurchaseConfig:
    raw = raw or {}
    return PurchaseConfig(
        enabled=bool(raw.get("enabled", False)),
        # Defaults to a dry run even when enabled: turning purchasing on and
        # spending money should be two separate decisions.
        dry_run=bool(raw.get("dry_run", True)),
        max_price_usd=float(raw.get("max_price_usd", 1.00)),
        max_total_usd=float(raw.get("max_total_usd", 5.00)),
        code_timeout_seconds=int(raw.get("code_timeout_seconds", 180)),
        poll_interval_seconds=int(raw.get("poll_interval_seconds", 5)),
        max_attempts=int(raw.get("max_attempts", 4)),
        log_file=str(raw.get("log_file", PURCHASE_LOG)),
    )


@dataclass
class Attempt:
    """One provider tried, and how it went. Kept for the run summary."""

    provider: str
    offer: Offer
    price_usd: float
    ok: bool
    detail: str


def rank_offers(
    pairs: Sequence[tuple[Provider, Any]],
    max_price_usd: float,
    rub_per_usd: float | None,
) -> list[tuple[float, Provider, Offer]]:
    """Every in-stock, affordable, USD-priceable offer, cheapest first.

    Every pool, not one per provider: a site often lists several Walmart pools
    (different operators, or separate service entries), and they don't share
    stock or delivery rates — one can be dead while the next works. Skipping
    them would throw away most of the available options.
    """
    ranked: list[tuple[float, Provider, Offer]] = []
    for provider, report in pairs:
        if not getattr(report, "ok", False):
            continue
        for offer in report.offers:
            if not offer.in_stock:
                continue
            usd = _sms.to_usd(offer, rub_per_usd)
            if usd is None or usd > max_price_usd:
                continue
            ranked.append((usd, provider, offer))
    # Cheapest first; ties broken by the provider's own success rate when it
    # reports one, so a 90%-delivery pool is tried before a 40% one at the
    # same price.
    return sorted(ranked, key=lambda t: (t[0], -(t[2].success_rate or 0)))


# Provider-wide failures: no other pool at that site will work either, so the
# rest of its offers are skipped rather than burning attempts one by one.
FATAL_MARKERS = ("BAD_KEY", "NO_BALANCE", "HTTP 401", "HTTP 403", "auth")


def _is_provider_wide(detail: str) -> bool:
    return any(m.lower() in detail.lower() for m in FATAL_MARKERS)


def acquire_any(
    pairs: Sequence[tuple[Provider, Any]],
    cfg: PurchaseConfig,
    budget: Budget,
    *,
    rub_per_usd: float | None = None,
    max_attempts: int | None = None,
    on_attempt=None,
    sleep=time.sleep,
    now=time.monotonic,
) -> tuple[Purchase, list[Attempt]]:
    """Try providers cheapest-first until one actually delivers a code.

    Stock is not delivery: a provider can report numbers available and still
    never send the SMS, which is the common failure here. So a timeout isn't
    fatal — cancel that number and move to the next provider.

    Returns the successful purchase plus the log of what was tried.
    """
    max_attempts = cfg.max_attempts if max_attempts is None else max_attempts
    candidates = rank_offers(pairs, budget.max_price_usd, rub_per_usd)
    if not candidates:
        raise PurchaseRefused(
            "no in-stock offer under the price cap that can be priced in USD"
        )

    attempts: list[Attempt] = []
    dead_providers: set[str] = set()
    for usd, provider, offer in candidates:
        if len(attempts) >= max_attempts:
            break
        if provider.name in dead_providers:
            continue
        try:
            purchase = acquire(
                provider, offer, cfg, budget,
                price_usd=usd, sleep=sleep, now=now,
            )
        except PurchaseRefused as e:
            # Budget or config, not the provider. Record and keep going: a
            # cheaper provider further down the list may still fit.
            attempts.append(Attempt(provider.name, offer, usd, False, str(e)))
            if on_attempt:
                on_attempt(attempts[-1])
            continue
        except ProviderError as e:
            # The number was bought and cancelled, so the charge is reversed on
            # most providers — hand the budget back before trying the next.
            budget.refund(usd)
            if _is_provider_wide(str(e)):
                # A bad key or empty balance kills every pool at this site.
                dead_providers.add(provider.name)
            attempts.append(Attempt(provider.name, offer, usd, False, str(e)))
            if on_attempt:
                on_attempt(attempts[-1])
            continue
        attempts.append(
            Attempt(provider.name, offer, usd, True, f"code {purchase.code}")
        )
        if on_attempt:
            on_attempt(attempts[-1])
        return purchase, attempts

    tried = ", ".join(f"{a.provider} ({a.detail})" for a in attempts) or "nothing"
    raise ProviderError(f"no provider delivered a code — tried: {tried}")


def pick_offer(reports: Sequence[Any], max_price_usd: float, rub_per_usd: float | None):
    """Cheapest in-stock offer we can price in USD, or None.

    Anything unpriceable is skipped rather than guessed at — buying blind is
    exactly what the budget guard exists to prevent.
    """
    from .sms_providers import to_usd

    best: tuple[float, Any, Offer] | None = None
    for report in reports:
        if not getattr(report, "ok", False):
            continue
        for offer in report.offers:
            if not offer.in_stock:
                continue
            usd = to_usd(offer, rub_per_usd)
            if usd is None or usd > max_price_usd:
                continue
            if best is None or usd < best[0]:
                best = (usd, report, offer)
    return best
