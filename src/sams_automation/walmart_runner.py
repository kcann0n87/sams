"""Run the add-phone flow across a list of Walmart accounts.

Holds the parts that need real dependencies — Playwright, a browser, live
provider APIs — so `walmart_flow` stays testable without any of them.
"""

from __future__ import annotations

import csv
import random
import time
from pathlib import Path
from typing import Any

from .config import WalmartAccount
from .sms_purchase import Budget, acquire_any, load_purchase_config
from .walmart_flow import AccountResult, WalmartFlow, add_phone_to_account, load_walmart_config

RESULTS_FILE = "walmart_results.csv"


def _providers_with_stock(config_path: str, us_only: bool = True):
    """Check every configured provider once, and keep the ones worth buying from."""
    from .config import load_sms_settings
    from .sms_providers import build_providers, check_all

    settings = load_sms_settings(config_path)
    providers, problems = build_providers(settings)
    for problem in problems:
        print(f"  Config: {problem}")
    reports = check_all(providers, us_only=us_only)
    return list(zip(providers, reports)), settings


def run_add_phone(
    config_path: str,
    accounts: list[WalmartAccount],
    *,
    limit: int | None = None,
    only: str | None = None,
    headless: bool = False,
    results_file: str = RESULTS_FILE,
) -> list[AccountResult]:
    """Log into each account and add a freshly bought number to it."""
    import yaml
    from playwright.sync_api import sync_playwright

    from .config import load_config
    from .proxies import ProxyPool, load_proxies

    raw = yaml.safe_load(Path(config_path).read_text()) or {}
    cfg = load_config(config_path)
    wcfg = load_walmart_config(raw.get("walmart"))
    pcfg = load_purchase_config(raw.get("purchasing"))
    rub_per_usd = (raw.get("sms_providers") or {}).get("rub_per_usd")

    if only:
        accounts = [a for a in accounts if a.email == only]
    if limit:
        accounts = accounts[:limit]
    if not accounts:
        print("No accounts to process.")
        return []

    print("Checking provider stock once for the whole run ...")
    pairs, _ = _providers_with_stock(config_path)
    live = sum(1 for _, r in pairs if r.ok and any(o.in_stock for o in r.offers))
    print(f"  {live} provider(s) reporting Walmart stock\n")

    budget = Budget(
        max_price_usd=pcfg.max_price_usd,
        max_total_usd=pcfg.max_total_usd,
    )

    pool = None
    if cfg.proxies.enabled and not cfg.browser.user_data_dir:
        proxies = load_proxies(cfg.proxies.file)
        if proxies:
            pool = ProxyPool(proxies, cfg.proxies.rotation)

    results: list[AccountResult] = []
    with sync_playwright() as pw:
        for index, account in enumerate(accounts):
            proxy = None
            if pool is not None:
                chosen = pool.for_account(account.email)
                proxy = chosen.to_playwright() if chosen else None
                if chosen:
                    print(f"  {account.email}: via {chosen.label}")

            browser = pw.chromium.launch(
                headless=headless,
                channel=cfg.browser.channel or None,
                slow_mo=cfg.browser.slow_mo_ms,
                proxy=proxy,
            )
            try:
                page = browser.new_context().new_page()
                page.set_default_timeout(cfg.browser.timeout_ms)
                flow = WalmartFlow(page, wcfg, cfg.browser.screenshot_dir)

                def acquire():
                    return acquire_any(
                        pairs, pcfg, budget,
                        rub_per_usd=rub_per_usd,
                        on_attempt=lambda a: print(
                            f"    {a.provider}: {'got a code' if a.ok else a.detail}"
                        ),
                    )

                result = add_phone_to_account(flow, account, acquire)
            except Exception as e:  # noqa: BLE001 - one account must not kill the run
                result = AccountResult(account.email, False, error=f"{type(e).__name__}: {e}")
            finally:
                browser.close()

            results.append(result)
            _record(results_file, result)
            status = "OK" if result.ok else "FAIL"
            print(f"  [{status}] {account.email} {result.phone} {result.error}".rstrip())
            print(f"    spent so far: ${budget.spent:.2f} of ${budget.max_total_usd:.2f}\n")

            if index + 1 < len(accounts):
                pause = random.uniform(
                    cfg.pacing.between_accounts_min, cfg.pacing.between_accounts_max
                )
                print(f"  pausing {pause:.0f}s before the next account ...\n")
                time.sleep(pause)

    ok = sum(1 for r in results if r.ok)
    print(f"Done: {ok}/{len(results)} account(s) got a number. Spent ${budget.spent:.2f}.")
    return results


def _record(path: str | Path, result: AccountResult) -> None:
    """Append to the results ledger, creating it with a header if needed."""
    path = Path(path)
    exists = path.exists()
    try:
        with path.open("a", newline="") as fh:
            writer = csv.writer(fh)
            if not exists:
                writer.writerow(["account", "status", "phone", "provider", "code", "error"])
            writer.writerow([
                result.account,
                "ok" if result.ok else "failed",
                result.phone,
                result.provider,
                result.code,
                result.error,
            ])
    except OSError:
        pass


def load_done(path: str | Path) -> set[str]:
    """Accounts already given a number, so a resumed run skips them."""
    path = Path(path)
    if not path.exists():
        return set()
    done: set[str] = set()
    try:
        with path.open(newline="") as fh:
            for row in csv.DictReader(fh):
                if (row.get("status") or "").strip() == "ok":
                    done.add((row.get("account") or "").strip())
    except OSError:
        return set()
    return done
