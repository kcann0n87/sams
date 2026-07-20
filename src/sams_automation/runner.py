"""Orchestrate the run: iterate accounts, drive the flow, record results."""

from __future__ import annotations

import csv
import random
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

from .config import Account, Config
from .imap_client import ImapClient, VerificationTimeout
from .proxies import ProxyPool, load_proxies
from .sams_flow import FlowError, SamsFlow
from .stealth import STEALTH_INIT_JS

RESULTS_FILE = "results.csv"
RESULTS_HEADER = ["timestamp", "primary_email", "secondary_email", "status", "detail"]


def _is_profile_in_use(exc: Exception) -> bool:
    """True if a persistent-context launch failed because Chrome is already open."""
    msg = str(exc).lower()
    return (
        "opening in existing browser session" in msg
        or "already in use" in msg
        or "profile" in msg and "in use" in msg
    )


def _load_done(results_path: Path) -> set[str]:
    """Return secondary_emails already marked 'ok' so we can resume/skip."""
    done: set[str] = set()
    if not results_path.exists():
        return done
    with results_path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row.get("status") == "ok" and row.get("secondary_email"):
                done.add(row["secondary_email"])
    return done


def _append_result(
    results_path: Path, account: Account, status: str, detail: str
) -> None:
    exists = results_path.exists()
    with results_path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if not exists:
            writer.writerow(RESULTS_HEADER)
        writer.writerow(
            [
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                account.primary_email,
                account.secondary_email,
                status,
                detail,
            ]
        )


def run(
    cfg: Config,
    accounts: list[Account],
    *,
    limit: int | None = None,
    only: str | None = None,
    resume: bool = True,
) -> None:
    results_path = Path(RESULTS_FILE)
    done = _load_done(results_path) if resume else set()

    queue = accounts
    if only:
        queue = [a for a in queue if only in (a.primary_email, a.secondary_email)]
    if resume:
        skipped = [a for a in queue if a.secondary_email in done]
        queue = [a for a in queue if a.secondary_email not in done]
        for a in skipped:
            print(f"[skip] already done: {a.label}")
    if limit is not None:
        queue = queue[:limit]

    if not queue:
        print("Nothing to do. (All accounts done, or filters matched nothing.)")
        return

    print(f"Processing {len(queue)} account(s).\n")

    shared_mode = bool(cfg.browser.user_data_dir)

    proxy_pool: ProxyPool | None = None
    if cfg.proxies.enabled and not shared_mode:
        proxies = load_proxies(cfg.proxies.file)  # raises if the file is missing
        if not proxies:
            raise ValueError(
                f"proxies.enabled is true but no valid proxies were found in "
                f"{cfg.proxies.file}."
            )
        proxy_pool = ProxyPool(proxies, cfg.proxies.rotation)
        print(f"Using {len(proxies)} prox(ies), rotation={cfg.proxies.rotation}.\n")
    elif cfg.proxies.enabled and shared_mode:
        print(
            "Note: proxies are ignored while using a persistent Chrome profile "
            "(you want your trusted home IP here).\n"
        )

    imap = ImapClient(cfg.imap)
    imap.connect()
    ok = fail = 0
    try:
        with sync_playwright() as pw:
            shared_context = None
            if shared_mode:
                channel = cfg.browser.channel or None
                print(
                    f"Launching real Chrome (channel={channel or 'chromium'}) with "
                    f"profile '{cfg.browser.user_data_dir}'. Solve any press-and-hold "
                    "by hand; the profile stays warm for later accounts.\n"
                )
                try:
                    shared_context = pw.chromium.launch_persistent_context(
                        cfg.browser.user_data_dir,
                        headless=cfg.browser.headless,
                        slow_mo=cfg.browser.slow_mo_ms,
                        channel=channel,
                    )
                except Exception as e:
                    if _is_profile_in_use(e):
                        raise SystemExit(
                            "\n"
                            "Google Chrome is already running, so it couldn't open the\n"
                            "automation's own window (the profile can't be shared with a\n"
                            "Chrome you have open normally).\n\n"
                            "Fix it:\n"
                            "  1. Fully QUIT Google Chrome — click a Chrome window and press\n"
                            "     Cmd+Q (just closing the windows isn't enough). The Chrome\n"
                            "     icon in the Dock should have no dot under it.\n"
                            "  2. View this dashboard in Safari instead of Chrome.\n"
                            "  3. Run again.\n"
                        ) from e
                    raise
                shared_context.set_default_timeout(cfg.browser.timeout_ms)
                if cfg.browser.stealth:
                    shared_context.add_init_script(STEALTH_INIT_JS)

            flow = SamsFlow(
                cfg, pw, imap, proxy_pool, shared_context=shared_context
            )
            try:
                for i, account in enumerate(queue):
                    print(f"[{i + 1}/{len(queue)}] {account.label}")
                    try:
                        flow.process(account)
                        _append_result(results_path, account, "ok", "")
                        ok += 1
                        print("    -> ok")
                    except VerificationTimeout as e:
                        _append_result(results_path, account, "no_code", str(e))
                        fail += 1
                        print(f"    -> no verification email: {e}")
                    except FlowError as e:
                        _append_result(results_path, account, "flow_error", str(e))
                        fail += 1
                        print(f"    -> flow error: {e}")
                    except Exception as e:  # keep going on unexpected errors
                        _append_result(
                            results_path, account, "error", f"{type(e).__name__}: {e}"
                        )
                        fail += 1
                        print(f"    -> unexpected error: {e}")

                    if i < len(queue) - 1:
                        self_pace(cfg)
            finally:
                if shared_context is not None:
                    shared_context.close()
    finally:
        imap.close()

    print(f"\nDone. ok={ok} failed={fail}. See {RESULTS_FILE} and screenshots/.")


def self_pace(cfg: Config) -> None:
    lo = cfg.pacing.between_accounts_min
    hi = max(lo, cfg.pacing.between_accounts_max)
    wait = random.uniform(lo, hi)
    print(f"    ...waiting {wait:.0f}s before next account")
    time.sleep(wait)
