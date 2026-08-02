#!/usr/bin/env python3
"""List — and optionally cancel — your active CyberYozh number orders.

Every POST /numbers/ charges the account immediately. A run that cycles
through numbers quickly, or one where the relay timed out waiting for an
assignment, leaves paid orders sitting there. This shows them, and cancels
the ones that never received a code so the money comes back.

    python cyberyozh_orders.py --key YOUR_KEY
    python cyberyozh_orders.py --key YOUR_KEY --cancel-unused
"""

from __future__ import annotations

import argparse
import sys

import requests

BASE = "https://app.cyberyozh.com/api/v1"
TIMEOUT = (15.0, 30.0)


def headers(key: str) -> dict[str, str]:
    return {"X-Api-Key": key, "accept": "application/json"}


def codes_on(order: dict) -> list:
    """history_sms_code, normalised — it can be a list, a string, or absent."""
    raw = order.get("history_sms_code")
    if not raw:
        return []
    return raw if isinstance(raw, list) else [raw]


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect CyberYozh orders")
    parser.add_argument("--key", required=True, help="Your X-Api-Key")
    parser.add_argument(
        "--cancel-unused",
        action="store_true",
        help="Cancel every listed order that has received no code. Refunds it.",
    )
    args = parser.parse_args()

    try:
        response = requests.get(
            f"{BASE}/numbers/", headers=headers(args.key), timeout=TIMEOUT
        )
    except requests.RequestException as exc:
        print(f"Could not reach CyberYozh: {exc}")
        return 1

    if response.status_code != 200:
        print(f"HTTP {response.status_code}: {response.text[:300]}")
        return 1

    orders = response.json()
    if isinstance(orders, dict):                 # paginated
        orders = orders.get("results", [])
    if not orders:
        print("No active orders. Nothing is sitting unused.")
        return 0

    print(f"{len(orders)} active order(s):\n")
    unused = []
    for order in orders:
        order_id = order.get("pk") or order.get("id") or ""
        codes = codes_on(order)
        state = f"code: {codes[0]}" if codes else "no code yet"
        print(f"  {order.get('phone_number', '?'):<16} {order.get('status', '?'):<10} "
              f"{state:<20} {order_id}")
        if not codes and order_id:
            unused.append(order_id)

    if not args.cancel_unused:
        if unused:
            print(f"\n{len(unused)} have received no code. To cancel and refund them:")
            print("  python cyberyozh_orders.py --key YOUR_KEY --cancel-unused")
        return 0

    print(f"\nCancelling {len(unused)} order(s) with no code ...")
    refunded = failed = 0
    for order_id in unused:
        try:
            result = requests.put(
                f"{BASE}/numbers/{order_id}/cancel/",
                headers=headers(args.key),
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            print(f"  {order_id}: {exc}")
            failed += 1
            continue
        if result.status_code in (200, 201, 202, 204):
            refunded += 1
        else:
            # "too early to cancel" is common and not worth alarm.
            print(f"  {order_id}: HTTP {result.status_code} {result.text[:120]}")
            failed += 1
    print(f"Cancelled {refunded}, refused {failed}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
