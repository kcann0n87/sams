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

import yaml

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


def _cmd_sms_plan(args: argparse.Namespace) -> int:
    """Show the exact order the purchase step will try pools in, buying nothing.

    The add-phone run only reaches the rotation once a browser is signed in and
    sitting on the phone form, which makes it a slow way to find out that the
    order is wrong or that the cap stops it early. This prints the same list
    `acquire_any` walks, from the same live stock check.
    """
    from pathlib import Path

    import yaml

    from .config import load_sms_settings
    from .sms_providers import build_providers, check_all, to_usd
    from .sms_purchase import load_purchase_config, rank_offers

    raw = yaml.safe_load(Path(args.config).read_text()) or {}
    settings = load_sms_settings(args.config)
    rub_per_usd = settings.get("rub_per_usd")
    pcfg = load_purchase_config(raw.get("purchasing"))

    providers, problems = build_providers(settings)
    for problem in problems:
        print(f"Config: {problem}")
    if not providers:
        print("No providers configured. Add API keys on the /sms page first.")
        return 2

    print(f"Checking {len(providers)} provider(s) — 30s each at worst ...")

    def progress(provider, report):
        if not report.ok:
            state = f"failed: {report.error}"
        else:
            live = [o for o in report.offers if o.in_stock]
            state = f"{len(live)} pool(s) in stock" if live else "nothing in stock"
        print(f"  {provider.name:<14} {state}")

    reports = check_all(providers, us_only=not args.worldwide, on_result=progress)
    pairs = list(zip(providers, reports))
    print()
    ranked = rank_offers(
        pairs, pcfg.max_price_usd, rub_per_usd, allow_unpriced=pcfg.allow_unpriced
    )
    unpriced_in_rotation = {id(offer) for _, _, offer in ranked
                            if to_usd(offer, rub_per_usd) is None}

    if ranked:
        print(f"Rotation order — {len(ranked)} pool(s), cheapest first:")
        for index, (usd, provider, offer) in enumerate(ranked, 1):
            stock = "?" if offer.count is None else str(offer.count)
            rate = f"  {offer.success_rate:.0f}%" if offer.success_rate else ""
            # An unpriced pool is charged at the cap, so say the number shown is
            # a worst case rather than a quote.
            cost = f"${usd:.2f}" if id(offer) not in unpriced_in_rotation else f"<=${usd:.2f}"
            # Country included: two rows with the same service and price are
            # usually different countries, and without it they read as the
            # same pool listed twice.
            print(
                f"  {index:2}. {provider.name:<14} {offer.service[:24]:<24} "
                f"{offer.country[:14]:<14} ({offer.service_code}) {cost}  "
                f"stock {stock}{rate}"
            )
    else:
        print("Nothing to rotate through: no in-stock pool is priceable in USD "
              f"under the ${pcfg.max_price_usd:.2f} cap.")

    # Anything in stock that ranking dropped, and the reason. Without this an
    # empty or short list looks like the provider has nothing.
    excluded = []
    for provider, report in pairs:
        if not report.ok:
            excluded.append((provider.name, "-", f"check failed: {report.error}"))
            continue
        for offer in report.offers:
            if not offer.in_stock:
                excluded.append((provider.name, offer.service, "pool empty"))
                continue
            usd = to_usd(offer, rub_per_usd)
            if usd is None:
                if id(offer) in unpriced_in_rotation:
                    continue  # in the rotation, just priced pessimistically
                excluded.append((
                    provider.name, offer.service,
                    "no USD price — set purchasing.allow_unpriced: true to try it",
                ))
            elif usd > pcfg.max_price_usd:
                excluded.append((
                    provider.name, offer.service,
                    f"${usd:.2f} is over the ${pcfg.max_price_usd:.2f} cap",
                ))
    if excluded:
        print(f"\nNot in the rotation ({len(excluded)}):")
        for name, service, why in excluded:
            print(f"  {name:<14} {service[:26]:<26} {why}")

    if ranked:
        cap = pcfg.max_attempts
        if cap <= 0:
            print(f"\nAttempt cap: none — all {len(ranked)} pools would be tried "
                  "(purchasing.max_attempts: 0).")
        elif cap < len(ranked):
            print(f"\nAttempt cap: {cap} (purchasing.max_attempts).")
            print(
                f"  Only the first {cap} of {len(ranked)} pools would be tried before\n"
                "  giving up. Set purchasing.max_attempts: 0 to work through every\n"
                "  one of them — the spend caps still stop a runaway."
            )
        else:
            print(f"\nAttempt cap: {cap} (purchasing.max_attempts).")
            print("  Enough to reach every pool listed above.")
    if pcfg.dry_run:
        print("\npurchasing.dry_run is true — a real run refuses at the first buy.")
    elif not pcfg.enabled:
        print("\npurchasing.enabled is false — a real run buys nothing.")
    return 0


def _cmd_walmart(args: argparse.Namespace) -> int:
    """Log into each Walmart account and add a freshly bought number."""
    from .config import load_walmart_accounts
    from .walmart_runner import RESULTS_FILE, load_done, run_add_phone

    # --accounts is inherited from the shared parser and belongs to the Sam's
    # Club flow. Passing it here looks like it chose the file but doesn't, so
    # say which file is actually being read rather than silently ignoring it.
    if args.accounts != "accounts.csv":
        print(
            f"Note: --accounts {args.accounts} is the Sam's Club list and isn't used "
            f"here.\n      Reading {args.walmart_accounts} (set it with "
            "--walmart-accounts)."
        )
    accounts = load_walmart_accounts(args.walmart_accounts)
    if not args.no_resume:
        done = load_done(RESULTS_FILE)
        skipped = [a for a in accounts if a.email in done]
        accounts = [a for a in accounts if a.email not in done]
        if skipped:
            print(
                f"Skipping {len(skipped)} account(s) already done "
                "(--no-resume to redo them)."
            )

    results = run_add_phone(
        args.config,
        accounts,
        limit=args.limit,
        only=args.only,
        headless=args.headless,
    )
    return 0 if results and all(r.ok for r in results) else 1


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
    calls: list[dict] = [] if args.show_responses else None
    results = probe(args.base_url, key, name=name, capture=calls)
    for r in results:
        mark = "MATCH" if r.sells_walmart else ("ok   " if r.ok else "no   ")
        print(f"  {mark}  {r.protocol:<14} {r.detail}")
        for offer in r.walmart[:5]:
            price = f"{offer.price:.2f} {offer.currency}" if offer.price is not None else "n/a"
            qty = offer.count if offer.count is not None else "-"
            print(f"  {'':<7} {'':<14}   {offer.service} · {offer.country} · {price} · qty={qty}")

    if calls:
        print("\nEvery request tried, and what came back:")
        for c in calls:
            mark = "ok " if c.get("ok") else "ERR"
            params = f"  {c['params']}" if c.get("params") else ""
            print(f"\n  {mark} {c['method']} {c['url']}{params}")
            print(f"      {c.get('detail', '')}")
        print(
            "\nNo match but a 401/403 above means the path exists and only the auth\n"
            "style is wrong. HTML or a 404 everywhere means the API lives elsewhere."
        )

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

    from .sms_providers import UNPROBEABLE

    return [p for p in PROTOCOLS if p not in UNPROBEABLE]


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
        print(f"Config: {problem}", file=sys.stderr)

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

    # Availability and priceability are different questions. An offer whose
    # price we can't read is still an offer you can buy — reporting it as an
    # empty pool is the exact confusion the qty=0 / qty=- split exists to
    # prevent, so the verdict comes from in_stock and never from the ranking.
    in_stock = [o for r in reports if r.ok for o in r.offers if o.in_stock]
    listed = sum(len(r.offers) for r in reports if r.ok)

    if in_stock:
        unpriced = len(in_stock) - len(rows)
        print(f"In stock: {len(in_stock)} offer(s) available now.")
        if rows:
            rows.sort()
            print("Cheapest (USD equivalent):")
            for usd, label in rows[:5]:
                print(f"  ${usd:0.2f}  {label}")
        if unpriced:
            print(
                f"  ({unpriced} available offer(s) reported no usable price — "
                "buyable, but not comparable on cost.)"
            )
        if rub_per_usd is None and any(
            o.currency != "USD" for r in reports for o in r.offers
        ):
            print(
                "\n  (Set sms_providers.rub_per_usd in your config to include "
                "RUB-priced offers in this ranking.)"
            )
    elif listed:
        print(
            f"No Walmart stock. {listed} offer(s) are listed but the pools are "
            "empty — try `--watch` to catch a restock."
        )
    else:
        print("No provider lists a Walmart service right now.")

    return 0 if in_stock else 1


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
        help="Service name to match (repeatable). Default: walmart.",
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

    pl = sub.add_parser(
        "sms-plan",
        parents=[common],
        help="Show the order pools would be tried in when buying. Buys nothing.",
    )
    pl.add_argument(
        "--worldwide",
        action="store_true",
        help="Include non-US pools (US-only by default).",
    )
    pl.set_defaults(func=_cmd_sms_plan)

    wm = sub.add_parser(
        "walmart-add-phone",
        parents=[common],
        help="Add a bought verification number to each Walmart account.",
    )
    wm.add_argument("--walmart-accounts", default="walmart_accounts.csv")
    wm.add_argument("--limit", type=int, default=None, help="Only the first N.")
    wm.add_argument("--only", default=None, help="Just this account email.")
    wm.add_argument("--no-resume", action="store_true", help="Don't skip done accounts.")
    wm.add_argument("--headless", action="store_true", help="No browser window.")
    wm.set_defaults(func=_cmd_walmart)

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
    sp_.add_argument(
        "--show-responses",
        action="store_true",
        help="Print every request tried and its raw response — use when nothing "
             "matches and you need to see what the API actually looks like.",
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
    except yaml.YAMLError as e:
        # A typo in config.yaml is the single most likely failure, and a raw
        # parser traceback buries the one thing that matters: the line number.
        mark = getattr(e, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        print(f"\nconfig.yaml isn't valid YAML{where}.", file=sys.stderr)
        print(f"  {getattr(e, 'problem', e)}", file=sys.stderr)
        if mark is not None:
            print(
                "\nOpen it with:  nano -w config.yaml\n"
                "then Ctrl-W and enter the line number. A long line split in two "
                "is the usual cause — editors that hard-wrap do it silently.",
                file=sys.stderr,
            )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
