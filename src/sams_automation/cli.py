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
import json
import sys
from datetime import datetime, timedelta, timezone

from .config import load_accounts, load_config
from .imap_client import ImapClient, VerificationTimeout


def _cmd_run(args: argparse.Namespace) -> int:
    # Imported here, not at module scope: this is the only command that needs
    # Playwright, and `sms-check` must stay usable without a browser install.
    from .runner import run

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


def _copy_to_clipboard(text: str) -> bool:
    """Best-effort clipboard copy. Returns True on success."""
    import shutil
    import subprocess

    for tool in (["pbcopy"], ["xclip", "-selection", "clipboard"], ["wl-copy"]):
        if shutil.which(tool[0]):
            try:
                subprocess.run(tool, input=text.encode(), check=True)
                return True
            except Exception:
                return False
    return False


def _cmd_watch(args: argparse.Namespace) -> int:
    """Live-print Sam's Club verification codes as they hit the catch-all inbox."""
    import time
    from datetime import datetime, timedelta, timezone

    cfg = load_config(args.config)
    client = ImapClient(cfg.imap)
    print(f"Connecting to {cfg.imap.host} as {cfg.imap.username} ...")
    client.connect()

    start = datetime.now(timezone.utc) - timedelta(minutes=args.backfill)
    seen: set[str] = set()
    scope = f" for {args.to}" if args.to else ""
    print(
        f"Watching for Sam's Club codes{scope} "
        f"(last {args.backfill} min + new). Press Ctrl-C to stop.\n"
    )
    try:
        while True:
            hits = client.scan_recent(
                since=start,
                verification=cfg.verification,
                to_filter=args.to,
                skip_uids=seen,
            )
            for h in hits:
                value = h.code or h.link or "(nothing matched — check the regex)"
                local = h.received.astimezone().strftime("%H:%M:%S")
                # \a rings the terminal bell so you notice without watching.
                print(f"\a{'=' * 52}")
                print(f"  {local}   ->  {h.to_address}")
                print(f"  CODE:  {value}")
                print(f"  ({h.subject})")
                if args.copy and h.code and _copy_to_clipboard(h.code):
                    print("  (copied to clipboard — just paste)")
                print(f"{'=' * 52}\n")
            time.sleep(cfg.imap.poll_interval_seconds)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        client.close()
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


def _cmd_serve(args: argparse.Namespace) -> int:
    from .web import serve

    serve(args.config, args.accounts, port=args.port,
          open_browser=args.open_browser)
    return 0


def _cmd_sms_probe(args: argparse.Namespace) -> int:
    """Work out which API protocol an unknown provider speaks."""
    import os

    from .config import load_sms_settings
    from .sms_providers import probe, probe_config_snippet

    name = args.name or "probe"
    key = args.key or os.environ.get(args.key_env or "", "")
    if not key:
        settings = load_sms_settings(args.config)
        key = str((settings.get(name) or {}).get("api_key") or "")
    if not key:
        print(
            "No API key found. Pass --key, or --key-env NAME to read it from the\n"
            "environment (preferred — keeps the key out of your shell history).",
            file=sys.stderr,
        )
        return 2

    print(f"Probing {args.base_url} with {len(PROTOCOL_NAMES())} protocol(s) ...\n")
    results = probe(args.base_url, key, name=name)
    for r in results:
        mark = "MATCH" if r.sells_walmart else ("ok   " if r.ok else "no   ")
        print(f"  {mark}  {r.protocol:<14} {r.detail}")
        for offer in r.walmart[:5]:
            price = f"{offer.price:.2f} {offer.currency}" if offer.price is not None else "n/a"
            qty = offer.count if offer.count is not None else "-"
            print(f"  {'':<7} {'':<14}   {offer.service} · {offer.country} · {price} · qty={qty}")

    winner = next((r for r in results if r.sells_walmart), None) or next(
        (r for r in results if r.ok), None
    )
    if winner is None:
        print(
            "\nNothing matched. Either the key is wrong, the base URL is off, or this\n"
            "site speaks an API we don't know yet — send me its docs and I'll add it."
        )
        return 1

    print(f"\nBest match: {winner.protocol}")
    if not winner.sells_walmart:
        print("  (authenticated, but no Walmart service in its catalog)")
    print("\nAdd it to config.yaml:\n")
    print(probe_config_snippet(name, args.base_url, winner.protocol))
    return 0


def PROTOCOL_NAMES():
    from .sms_providers import PROTOCOLS

    return [p for p in PROTOCOLS if p != "handler_api"]


def _sms_list_providers(providers, settings) -> int:
    """Show what's wired up and what's still missing a key."""
    from .sms_providers import (
        ACTIVATE_ALIASES,
        DEAD_PROVIDERS,
        KNOWN_ACTIVATE_HOSTS,
        PROVIDERS,
        build_known_activate,
    )

    active = {p.name for p in providers}
    print("Active (a key was found in config or the environment):")
    for name in sorted(active):
        print(f"  + {name}")
    if not active:
        print("  (none)")

    print("\nKnown but not configured:")
    missing = []
    for name in sorted(PROVIDERS):
        if name not in active:
            env = PROVIDERS[name].env_key
            missing.append(f"  - {name:<16} {'$' + env if env else ''}")
    for name in sorted(KNOWN_ACTIVATE_HOSTS):
        # Skip aliases: they're the same site as a built-in listed just above.
        if name in ACTIVATE_ALIASES:
            continue
        if name not in active and build_known_activate(name) is None:
            missing.append(f"  - {name:<16} ${KNOWN_ACTIVATE_HOSTS[name]['env']}")
    print("\n".join(missing) if missing else "  (none)")

    print("\nDefunct:")
    for name, reason in DEAD_PROVIDERS.items():
        print(f"  x {name:<16} {reason}")
    print(
        "\nSet a key by exporting the listed variable, or add it under "
        "`sms_providers:` in your config.\nAnything not listed can be added as a "
        "`custom:` entry — see docs/SMS_PROVIDERS.md."
    )
    return 0


def _sms_render(reports, rub_per_usd, in_stock_only: bool) -> list[tuple[float, str]]:
    """Print the per-provider table; return (usd, label) for in-stock offers."""
    from .sms_providers import to_usd

    rows: list[tuple[float, str]] = []
    for report in sorted(reports, key=lambda r: r.provider):
        if not report.ok:
            print(f"  {report.provider:<14} ERROR  {report.error}")
            continue
        offers = [o for o in report.offers if o.in_stock or not in_stock_only]
        stock = report.total_stock
        stock_txt = f"{stock} in stock" if stock is not None else "stock not reported"
        print(f"  {report.provider:<14} {len(offers)} offer(s), {stock_txt}")
        for note in report.notes:
            print(f"  {'':<14} note: {note}")
        for o in sorted(offers, key=lambda o: (o.price is None, o.price or 0)):
            price = f"{o.price:.2f} {o.currency}" if o.price is not None else "n/a"
            count = str(o.count) if o.count is not None else "-"
            rate = f"{o.success_rate:.0f}%" if o.success_rate is not None else "-"
            print(
                f"  {'':<14}   {o.service[:18]:<18} {o.country[:14]:<14} "
                f"{o.operator[:12]:<12} {price:>12}  qty={count:<7} rate={rate}"
            )
            usd = to_usd(o, rub_per_usd)
            if usd is not None and o.in_stock:
                rows.append((usd, f"{o.provider} / {o.service} / {o.country} ({o.operator})"))
        print()
    return rows


def _sms_stock_keys(reports) -> set[str]:
    """Identity of every offer currently in stock, for change detection."""
    return {
        f"{o.provider}/{o.service_code}/{o.country}/{o.operator}"
        for r in reports
        if r.ok
        for o in r.offers
        if o.in_stock
    }


def _cmd_sms_watch(args, providers, terms, rub_per_usd) -> int:
    """Poll until stock shows up, then make noise about it.

    Walmart US pools sit empty for long stretches, so the useful mode isn't a
    one-shot check — it's sitting on the feed and catching the window. Only
    changes are printed, so this stays readable running for hours.
    """
    import time

    from .sms_providers import check_all

    print(
        f"Watching {len(providers)} provider(s) every {args.interval}s. "
        "Ctrl-C to stop.\n"
    )
    seen_in_stock: set[str] = set()
    first_pass = True
    polls = 0
    try:
        while True:
            reports = check_all(providers, terms, us_only=not args.all_countries)
            polls += 1
            current = _sms_stock_keys(reports)
            fresh = current - seen_in_stock

            if fresh or first_pass:
                stamp = datetime.now().strftime("%H:%M:%S")
                if fresh and not first_pass:
                    # \a rings the terminal bell — the whole point of watching.
                    print(f"\a{'=' * 62}")
                    print(f"  {stamp}  STOCK APPEARED ({len(fresh)} new offer(s))")
                    print(f"{'=' * 62}")
                else:
                    print(f"--- {stamp}  poll #{polls}")
                rows = _sms_render(reports, rub_per_usd, args.in_stock_only)
                if rows:
                    rows.sort()
                    print("Cheapest in-stock (USD equivalent):")
                    for usd, label in rows[:5]:
                        print(f"  ${usd:0.2f}  {label}")
                    print()
                if args.once_in_stock and current:
                    print("Stock found — exiting (--once-in-stock).")
                    return 0
            else:
                stamp = datetime.now().strftime("%H:%M:%S")
                errs = sum(1 for r in reports if not r.ok)
                state = "still no stock" if not current else f"{len(current)} offer(s), unchanged"
                suffix = f", {errs} provider error(s)" if errs else ""
                print(f"  {stamp}  poll #{polls}: {state}{suffix}")

            seen_in_stock = current
            first_pass = False
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print(f"\nStopped after {polls} poll(s).")
        return 0


def _cmd_sms_check(args: argparse.Namespace) -> int:
    """Ask each SMS provider whether it has Walmart numbers, and at what price."""
    from .config import load_sms_settings
    from .sms_providers import (
        DEFAULT_TERMS,
        PROTOCOLS,
        PROVIDERS,
        build_custom,
        build_providers,
        check_all,
    )

    settings = load_sms_settings(args.config)
    rub_per_usd = settings.get("rub_per_usd")
    providers, problems = build_providers(settings)
    for problem in problems:
        print(f"Config warning: {problem}", file=sys.stderr)

    if args.list_providers:
        return _sms_list_providers(providers, settings)

    if args.provider:
        wanted = {p.lower() for p in args.provider}
        by_name = {p.name.lower(): p for p in providers}
        # An explicit --provider overrides the config's enabled flags, so fall
        # back to a bare instance for a known built-in that's switched off.
        selected, unknown = [], []
        for name in sorted(wanted):
            if name in by_name:
                selected.append(by_name[name])
            elif name in PROVIDERS:
                selected.append(PROVIDERS[name](settings.get(name) or {}))
            else:
                unknown.append(name)
        if unknown:
            known = sorted(set(PROVIDERS) | {p.name.lower() for p in providers})
            print(
                f"Unknown provider(s): {', '.join(unknown)}. "
                f"Known: {', '.join(known)}",
                file=sys.stderr,
            )
            return 2
        providers = selected

    if not providers:
        print(
            "No providers configured. 5sim needs no key and works out of the box:\n"
            "  python -m sams_automation sms-check --provider 5sim\n"
            "For the rest, add an `sms_providers:` block to your config "
            "(see config.example.yaml).",
            file=sys.stderr,
        )
        return 2

    terms = args.term or list(DEFAULT_TERMS)
    if args.watch:
        return _cmd_sms_watch(args, providers, terms, rub_per_usd)

    scope = "USA" if not args.all_countries else "all countries"
    print(
        f"Checking {len(providers)} provider(s) for "
        f"{'/'.join(terms)} numbers in {scope} ...\n"
    )
    reports = check_all(providers, terms, us_only=not args.all_countries)

    if args.json:
        import dataclasses

        print(json.dumps([dataclasses.asdict(r) for r in reports], indent=2, default=str))
        return 0 if any(r.ok and any(o.in_stock for o in r.offers) for r in reports) else 1

    rows = _sms_render(reports, rub_per_usd, args.in_stock_only)
    if rows:
        rows.sort()
        print("Cheapest in-stock options (USD equivalent):")
        for usd, label in rows[:5]:
            print(f"  ${usd:0.2f}  {label}")
        if rub_per_usd is None and any(
            o.currency != "USD" for r in reports for o in r.offers
        ):
            print(
                "\n  (Set sms_providers.rub_per_usd in your config to include "
                "RUB-priced offers in this ranking.)"
            )
    else:
        listed = sum(len(r.offers) for r in reports if r.ok)
        if listed:
            print(
                f"No in-stock Walmart offers. {listed} offer(s) are listed but "
                "the pools are empty — try `--watch` to catch a restock."
            )
        else:
            print("No provider lists a Walmart service right now.")

    any_stock = any(r.ok and any(o.in_stock for o in r.offers) for r in reports)
    return 0 if any_stock else 1


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

    s = sub.add_parser(
        "serve", parents=[common], help="Open the local web UI in your browser."
    )
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-open", dest="open_browser", action="store_false")
    s.set_defaults(func=_cmd_serve, open_browser=True)

    sc = sub.add_parser(
        "sms-check",
        parents=[common],
        help="Check SMS providers for Walmart number availability and price.",
    )
    sc.add_argument(
        "--provider",
        action="append",
        default=None,
        help="Only check this provider (repeatable). Default: all configured.",
    )
    sc.add_argument(
        "--term",
        action="append",
        default=None,
        help="Service name to match (repeatable). Default: walmart + sam's club.",
    )
    sc.add_argument(
        "--all-countries",
        action="store_true",
        help="Don't restrict to US numbers.",
    )
    sc.add_argument(
        "--in-stock-only",
        action="store_true",
        help="Hide offers the provider reports as out of stock.",
    )
    sc.add_argument("--json", action="store_true", help="Emit raw JSON instead of a table.")
    sc.add_argument(
        "--watch",
        action="store_true",
        help="Poll on an interval and alert when stock appears.",
    )
    sc.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Seconds between polls in --watch mode (default 60).",
    )
    sc.add_argument(
        "--once-in-stock",
        action="store_true",
        help="In --watch mode, exit as soon as anything is in stock.",
    )
    sc.add_argument(
        "--list-providers",
        action="store_true",
        help="List every known provider and whether a key was found.",
    )
    sc.set_defaults(func=_cmd_sms_check)

    sp_ = sub.add_parser(
        "sms-probe",
        parents=[common],
        help="Detect which API protocol an unknown SMS provider speaks.",
    )
    sp_.add_argument("base_url", help="e.g. https://api.some-site.com")
    sp_.add_argument("--name", default=None, help="What to call it in config.")
    sp_.add_argument(
        "--key-env",
        default=None,
        help="Read the API key from this environment variable (preferred).",
    )
    sp_.add_argument(
        "--key", default=None, help="API key inline (ends up in shell history)."
    )
    sp_.set_defaults(func=_cmd_sms_probe)

    tp = sub.add_parser(
        "test-proxy",
        parents=[common],
        help="Launch a browser through each proxy and print its exit IP.",
    )
    tp.set_defaults(func=_cmd_test_proxy)

    w = sub.add_parser(
        "watch",
        parents=[common],
        help="Live-print verification codes as they arrive in the catch-all inbox.",
    )
    w.add_argument("--to", default=None, help="Only show codes for this secondary email.")
    w.add_argument(
        "--backfill",
        type=int,
        default=10,
        help="Also show codes from the last N minutes on startup (default 10).",
    )
    w.add_argument(
        "--no-copy",
        dest="copy",
        action="store_false",
        help="Don't auto-copy the code to the clipboard.",
    )
    w.set_defaults(func=_cmd_watch, copy=True)

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
