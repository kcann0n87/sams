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
from .sams_flow import FlowError, SamsFlow

RESULTS_FILE = "results.csv"
RESULTS_HEADER = ["timestamp", "primary_email", "secondary_email", "status", "detail"]


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

    imap = ImapClient(cfg.imap)
    imap.connect()
    ok = fail = 0
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=cfg.browser.headless,
                slow_mo=cfg.browser.slow_mo_ms,
            )
            flow = SamsFlow(cfg, browser, imap)
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
                browser.close()
    finally:
        imap.close()

    print(f"\nDone. ok={ok} failed={fail}. See {RESULTS_FILE} and screenshots/.")


def self_pace(cfg: Config) -> None:
    lo = cfg.pacing.between_accounts_min
    hi = max(lo, cfg.pacing.between_accounts_max)
    wait = random.uniform(lo, hi)
    print(f"    ...waiting {wait:.0f}s before next account")
    time.sleep(wait)
