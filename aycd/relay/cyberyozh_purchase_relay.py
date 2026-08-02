#!/usr/bin/env python3
"""
CyberYozh purchase relay - v1

CyberYozh's POST /api/v1/numbers/ does not return phone_number immediately
for this account - it comes back status "new" with no number, and the
number only appears a few seconds later when you GET the same order by id.

AYCD's schema format needs getPhoneNumber to return a populated phone
number in a single response, so this relay does the waiting:

  1. POST the purchase to CyberYozh (forwarding whatever body + X-Api-Key
     Inbox sends).
  2. If CyberYozh rejects the purchase (400/402/429), pass that error
     straight back so Inbox/Profile Builder shows the real reason
     (e.g. "no numbers available").
  3. On success, take the returned order id and poll
     GET /api/v1/numbers/{id}/ every few seconds until phone_number is
     populated (or a timeout is hit).
  4. Return {"phone_number": ..., "order_id": ...} to Inbox.

Everything else (polling for the SMS code, cancelling) does NOT go
through this relay - the schema calls CyberYozh directly for those,
since GET /api/v1/numbers/{id}/ and PUT .../cancel/ both work fine
synchronously.
"""

from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

import requests

CYBER_BASE = "https://app.cyberyozh.com/api/v1"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_ASSIGNMENT_TIMEOUT = 90.0
DEFAULT_POLL_INTERVAL = 2.0
REQUEST_TIMEOUT = (15.0, 30.0)


class RelayServer(ThreadingHTTPServer):
    daemon_threads = True
    assignment_timeout: float
    poll_interval: float


class RelayHandler(BaseHTTPRequestHandler):
    server_version = "CyberYozhPurchaseRelay/1.0"

    def setup(self) -> None:
        super().setup()
        self.cyber = requests.Session()
        self.cyber.headers.update({"User-Agent": "CyberYozh-Purchase-Relay/1.0"})

    def finish(self) -> None:
        try:
            self.cyber.close()
        finally:
            super().finish()

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] 127.0.0.1 - {fmt % args}")

    def send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            print("Inbox closed the local request before the response was sent.")

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid JSON body: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("JSON body must be an object.")
        return parsed

    def api_key(self) -> str | None:
        return self.headers.get("X-Api-Key")

    def cyber_headers(self) -> dict[str, str]:
        return {
            "accept": "application/json",
            "X-Api-Key": self.api_key() or "",
            "Content-Type": "application/json",
        }

    def cancel_order(self, order_id: str) -> bool:
        """Give an unassigned order back. It was charged the moment it was made.

        Without this, every timeout leaves a paid number nobody is watching —
        the account fills up with orders that will never receive a code.
        """
        try:
            result = self.cyber.put(
                f"{CYBER_BASE}/numbers/{order_id}/cancel/",
                headers=self.cyber_headers(),
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            print(f"Could not cancel {order_id}: {exc}")
            return False
        if result.status_code in (200, 201, 202, 204):
            print(f"Cancelled unassigned order {order_id} — refunded")
            return True
        print(f"Cancel of {order_id} refused: HTTP {result.status_code} "
              f"{result.text[:160]}")
        return False

    def passthrough(self, response: requests.Response) -> None:
        try:
            payload: Any = response.json()
        except ValueError:
            payload = {
                "detail": response.text
                or f"CyberYozh returned HTTP {response.status_code}"
            }
        self.send_json(response.status_code, payload)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path.rstrip("/") == "/health":
            self.send_json(200, {"status": "ok", "version": "1.0"})
            return
        self.send_json(404, {"detail": "Not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path.rstrip("/") != "/number":
            self.send_json(404, {"detail": "Not found"})
            return
        if not self.api_key():
            self.send_json(401, {"detail": "Missing X-Api-Key header"})
            return

        try:
            body = self.read_json()
        except ValueError as exc:
            self.send_json(400, {"detail": str(exc)})
            return

        try:
            purchase = self.cyber.post(
                f"{CYBER_BASE}/numbers/",
                headers=self.cyber_headers(),
                json=body,
                timeout=REQUEST_TIMEOUT,
            )
            print(f"CyberYozh POST /numbers/ -> HTTP {purchase.status_code}")
        except requests.RequestException as exc:
            print(f"PURCHASE NETWORK ERROR: {type(exc).__name__}: {exc}")
            self.send_json(502, {"detail": f"Could not reach CyberYozh: {exc}"})
            return

        if purchase.status_code not in (200, 201):
            print(f"Purchase error body: {purchase.text[:1000]}")
            self.passthrough(purchase)
            return

        try:
            order = purchase.json()
        except ValueError:
            self.send_json(502, {"detail": "CyberYozh purchase response was not JSON"})
            return

        order_id = str(order.get("id") or "")
        if not order_id:
            self.send_json(502, {"detail": "CyberYozh purchase response had no id"})
            return

        if order.get("phone_number"):
            self.send_json(
                200,
                {
                    "phone_number": order["phone_number"],
                    "order_id": order_id,
                    "status": order.get("status"),
                },
            )
            return

        print(f"Order {order_id} created, waiting for phone_number to populate...")
        deadline = time.monotonic() + self.server.assignment_timeout

        while time.monotonic() < deadline:
            time.sleep(self.server.poll_interval)
            try:
                detail = self.cyber.get(
                    f"{CYBER_BASE}/numbers/{order_id}/",
                    headers=self.cyber_headers(),
                    timeout=REQUEST_TIMEOUT,
                )
            except requests.RequestException as exc:
                print(f"Polling error: {exc}")
                continue

            if detail.status_code == 429:
                # Their rate limit. Polling harder makes it worse, and with
                # several tasks buying at once this is easy to hit.
                print(f"Rate limited polling {order_id}; backing off")
                time.sleep(self.server.poll_interval * 3)
                continue

            if detail.status_code != 200:
                # A 404 straight after creation is normal here — the order is
                # not queryable until it has been assigned.
                if detail.status_code != 404:
                    print(f"Polling GET /numbers/{order_id}/ -> "
                          f"HTTP {detail.status_code}")
                continue

            try:
                detail_body = detail.json()
            except ValueError:
                continue

            if detail_body.get("phone_number"):
                print(f"Got phone number for order {order_id}: {detail_body['phone_number']}")
                self.send_json(
                    200,
                    {
                        "phone_number": detail_body["phone_number"],
                        "order_id": order_id,
                        "status": detail_body.get("status"),
                    },
                )
                return

        # Charged but never assigned. Hand it back before giving up.
        refunded = self.cancel_order(order_id)
        self.send_json(
            504,
            {
                "detail": (
                    "CyberYozh accepted the purchase, but no phone number was "
                    "assigned within the timeout."
                    + (" The order was cancelled and refunded."
                       if refunded else
                       " The order could NOT be cancelled — check it manually,"
                       " it has been charged.")
                ),
                "order_id": order_id,
                "cancelled": refunded,
            },
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="CyberYozh purchase relay for AYCD")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--assignment-timeout", type=float, default=DEFAULT_ASSIGNMENT_TIMEOUT
    )
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL)
    parser.add_argument(
        "--no-cancel-on-timeout", action="store_true",
        help="Leave unassigned orders alone instead of refunding them.",
    )
    args = parser.parse_args()

    server = RelayServer((args.host, args.port), RelayHandler)
    server.assignment_timeout = args.assignment_timeout
    server.poll_interval = args.poll_interval

    print(f"CyberYozh purchase relay listening on http://{args.host}:{args.port}")
    print("Only /number (POST) goes through this relay - getMessage and cancel")
    print("call CyberYozh directly from the schema.")
    print("Health check: http://127.0.0.1:8765/health")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping relay...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
