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

    # Which browser binary to drive. "chromium" (or empty) uses Playwright's
    # own build, "chrome" the installed Google Chrome.
    #
    # Defaults to chromium rather than inheriting browser.channel: that setting
    # exists for the Sam's Club flow, which needs real Chrome, and silently
    # borrowing it launched Chrome here while the web UI reported chromium.
    # A config written before this key existed would have hit exactly that.
    channel = str((raw.get("walmart") or {}).get("channel") or "chromium").strip().lower()
    channel = None if channel in ("", "chromium") else channel
    print(f"Browser: {channel or 'chromium (Playwright build)'}")

    print("Checking provider stock once for the whole run ...")
    pairs, _ = _providers_with_stock(config_path)
    live = sum(1 for _, r in pairs if r.ok and any(o.in_stock for o in r.offers))
    print(f"  {live} provider(s) reporting Walmart stock\n")

    budget = Budget(
        max_price_usd=pcfg.max_price_usd,
        max_total_usd=pcfg.max_total_usd,
    )

    # Walmart gets its own proxy list, defaulting to walmart_proxies.txt: these
    # are residential IPs for a different site than the Sam's Club flow uses,
    # and "fresh" means a big pool never puts two accounts behind one IP.
    wproxy = (raw.get("walmart") or {}).get("proxies") or {}
    pool = None
    if wproxy.get("enabled", True):
        proxy_file = wproxy.get("file") or "walmart_proxies.txt"
        rotation = wproxy.get("rotation", "fresh")
        try:
            proxies = load_proxies(proxy_file, report=True)
        except FileNotFoundError as e:
            print(f"  {e}")
            proxies = []
        if proxies:
            pool = ProxyPool(proxies, rotation)
            print(f"  rotation: {rotation}\n")
        else:
            print("  running without proxies\n")

    results: list[AccountResult] = []
    with sync_playwright() as pw:
        for index, account in enumerate(accounts):
            proxy = None
            # An explicit proxy on the row wins; otherwise draw from the pool.
            if account.proxy:
                from .proxies import parse_proxy_line

                chosen = parse_proxy_line(account.proxy)
                proxy = chosen.to_playwright()
                print(f"  {account.email}: via {chosen.label} (from the CSV row)")
            elif pool is not None:
                chosen = pool.for_account(account.email)
                proxy = chosen.to_playwright() if chosen else None
                if chosen:
                    print(f"  {account.email}: via {chosen.label}")
                if pool.exhausted:
                    print("    (pool wrapped around — IPs are being reused)")

            browser = pw.chromium.launch(
                headless=headless,
                channel=channel,
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

                def fetch_login_code(_account=account):
                    return _read_signin_code(raw.get("walmart"), _account.email)

                result = add_phone_to_account(flow, account, acquire, fetch_login_code)
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


def walmart_imap_config(raw: dict[str, Any] | None):
    """Build the IMAP + code-extraction config for Walmart's sign-in email.

    Separate from the top-level `imap:` block, which is the iCloud catch-all the
    Sam's Club flow uses. Walmart's accounts sit on a different mailbox and the
    sender filter differs, so sharing one config would break both.
    """
    from .config import ImapConfig, VerificationConfig

    section = ((raw or {}).get("imap")) or {}
    if not section.get("username"):
        return None, None
    imap = ImapConfig(
        host=section.get("host", "imap.gmail.com"),
        port=int(section.get("port", 993)),
        ssl=bool(section.get("ssl", True)),
        username=section["username"],
        password=section.get("password", ""),
        mailbox=section.get("mailbox", "INBOX"),
        from_contains=[s.lower() for s in section.get("from_contains", ["walmart"])],
        timeout_seconds=int(section.get("timeout_seconds", 180)),
        poll_interval_seconds=int(section.get("poll_interval_seconds", 5)),
    )
    verification = VerificationConfig(
        mode="code",
        code_regex=section.get("code_regex", r"\b(\d{6})\b"),
        link_regex=section.get("link_regex", ""),
    )
    return imap, verification


def _read_signin_code(raw_walmart: dict[str, Any] | None, to_address: str) -> str:
    """Wait for Walmart's sign-in code to land, and return it.

    Only messages that arrive after the request was made are considered, so a
    code from a previous run can't be replayed into this one.
    """
    from datetime import datetime, timedelta, timezone

    from .imap_client import ImapClient, VerificationTimeout

    imap_cfg, verification = walmart_imap_config(raw_walmart)
    if imap_cfg is None:
        raise RuntimeError(
            "the emailed sign-in code is configured but walmart.imap has no "
            "username — fill in the Gmail catch-all in config.yaml"
        )

    # A small backdate absorbs clock skew without reaching back to older codes.
    since = datetime.now(timezone.utc) - timedelta(seconds=90)
    client = ImapClient(imap_cfg)
    client.connect()
    try:
        print(f"    waiting for the sign-in code emailed to {to_address} ...")
        result = client.wait_for_verification(
            to_address=to_address, since=since, verification=verification
        )
    except VerificationTimeout as e:
        raise RuntimeError(f"no sign-in code arrived: {e}") from None
    finally:
        client.close()
    return result.code or ""


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
