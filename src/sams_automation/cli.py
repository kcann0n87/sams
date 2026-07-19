"""Command-line entry point.

Usage:
    python -m sams_automation run [--config config.yaml] [--accounts accounts.csv]
                                  [--limit N] [--only EMAIL] [--no-resume]
                                  [--headless]
    python -m sams_automation test-imap --to someone@yourdomain.com
                                  [--config config.yaml]
    python -m sams_automation test-proxy [--config config.yaml]
    python -m sams_automation check [--config config.yaml] [--accounts accounts.csv]
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

from .config import load_accounts, load_config
from .imap_client import ImapClient, VerificationTimeout
from .runner import run


def _cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if args.headless:
        cfg.browser.headless = True
    accounts = load_accounts(args.accounts)
    run(
        cfg,
        accounts,
        limit=args.limit,
        only=args.only,
        resume=not args.no_resume,
    )
    return 0


def _cmd_test_proxy(args: argparse.Namespace) -> int:
    """Launch a browser through each proxy and print its exit IP."""
    from playwright.sync_api import sync_playwright

    from .proxies import load_proxies

    cfg = load_config(args.config)
    proxies = load_proxies(cfg.proxies.file)
    if not proxies:
        print(f"No proxies found in {cfg.proxies.file}.")
        return 1

    url = "https://api.ipify.org?format=text"
    print(f"Testing {len(proxies)} prox(ies) against {url} ...\n")
    failures = 0
    with sync_playwright() as pw:
        for p in proxies:
            try:
                browser = pw.chromium.launch(headless=True, proxy=p.to_playwright())
                page = browser.new_context().new_page()
                page.goto(url, timeout=20000)
                ip = page.inner_text("body").strip()
                print(f"  OK   {p.label:32} -> exit IP {ip}")
                browser.close()
            except Exception as e:
                failures += 1
                print(f"  FAIL {p.label:32} -> {type(e).__name__}: {e}")
    print(f"\n{len(proxies) - failures}/{len(proxies)} proxies working.")
    return 1 if failures else 0


def _cmd_check(args: argparse.Namespace) -> int:
    """Validate config + accounts and print a summary without touching anything."""
    cfg = load_config(args.config)
    accounts = load_accounts(args.accounts)
    print(f"Config OK: {args.config}")
    print(f"  IMAP host:        {cfg.imap.host}:{cfg.imap.port} (ssl={cfg.imap.ssl})")
    print(f"  IMAP user:        {cfg.imap.username}")
    print(f"  Verification:     mode={cfg.verification.mode}")
    print(f"  Browser headless: {cfg.browser.headless}")
    print(
        f"  Proxies:          enabled={cfg.proxies.enabled} "
        f"file={cfg.proxies.file} rotation={cfg.proxies.rotation}"
    )
    print(f"Accounts OK: {args.accounts}")
    print(f"  {len(accounts)} account(s) loaded:")
    for a in accounts:
        print(f"    - {a.label}")
    return 0


def _cmd_test_imap(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    client = ImapClient(cfg.imap)

    print(f"Connecting to {cfg.imap.host}:{cfg.imap.port} as {cfg.imap.username} ...")
    try:
        folders = client.test_connection()
    except Exception as e:
        print(f"Connection FAILED: {type(e).__name__}: {e}")
        print(
            "\nFor iCloud: username must be your Apple ID email and the password "
            "must be an APP-SPECIFIC PASSWORD from appleid.apple.com."
        )
        return 1
    print("Connected. Mailboxes visible:")
    for f in folders[:20]:
        print(f"  {f}")

    if not args.to:
        print("\n(Pass --to EMAIL to poll for a recent verification email.)")
        return 0

    since = datetime.now(timezone.utc) - timedelta(minutes=args.since_minutes)
    print(
        f"\nPolling for a message to {args.to} received in the last "
        f"{args.since_minutes} min (timeout {cfg.imap.timeout_seconds}s)..."
    )
    client.connect()
    try:
        result = client.wait_for_verification(
            to_address=args.to, since=since, verification=cfg.verification
        )
    except VerificationTimeout as e:
        print(f"No match: {e}")
        return 1
    finally:
        client.close()
    print("Found a verification email:")
    print(f"  Subject:  {result.subject}")
    print(f"  Received: {result.received.isoformat()}")
    print(f"  Code:     {result.code}")
    print(f"  Link:     {result.link}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sams_automation")
    sub = p.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default="config.yaml")
    common.add_argument("--accounts", default="accounts.csv")

    r = sub.add_parser("run", parents=[common], help="Run the automation.")
    r.add_argument("--limit", type=int, default=None, help="Only process first N.")
    r.add_argument("--only", default=None, help="Process only this primary/secondary email.")
    r.add_argument("--no-resume", action="store_true", help="Don't skip done accounts.")
    r.add_argument("--headless", action="store_true", help="Force headless browser.")
    r.set_defaults(func=_cmd_run)

    c = sub.add_parser("check", parents=[common], help="Validate config + accounts.")
    c.set_defaults(func=_cmd_check)

    tp = sub.add_parser(
        "test-proxy",
        parents=[common],
        help="Launch a browser through each proxy and print its exit IP.",
    )
    tp.set_defaults(func=_cmd_test_proxy)

    t = sub.add_parser(
        "test-imap",
        parents=[common],
        help="Test the IMAP connection and optionally poll for a code.",
    )
    t.add_argument("--to", default=None, help="Poll for an email to this address.")
    t.add_argument(
        "--since-minutes",
        type=int,
        default=30,
        help="Consider emails newer than this many minutes (default 30).",
    )
    t.set_defaults(func=_cmd_test_imap)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
