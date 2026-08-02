# CyberYozh purchase relay

A small local HTTP server that sits between AYCD Inbox and CyberYozh for one
step only: buying a number.

## Why it exists

CyberYozh's `POST /api/v1/numbers/` returns immediately with
`status: "new"` and `phone_number: null`. The number is assigned a few seconds
later and only shows up on `GET /api/v1/numbers/{id}/`.

Inbox's schema format expects `getPhoneNumber` to hand back a populated number
in a single response, so something has to do the waiting. That's all this is.

Polling for the SMS code and cancelling do **not** go through it — the schema
calls CyberYozh directly for those, because both work synchronously.

## Running it

On Windows, double-click in this order:

1. `install_requests.bat` — one time, installs the `requests` package
2. `run_relay.bat` — leave this window open while you're using Inbox

Elsewhere:

```
pip install requests
python cyberyozh_purchase_relay.py
```

Check it's up: <http://127.0.0.1:8765/health> should return
`{"status": "ok", "version": "1.0"}`.

Then load `../cyberyozh-relay.json` in Inbox. Your API key goes in Inbox as
usual — the relay forwards whatever `X-Api-Key` it receives and stores
nothing.

Options: `--port`, `--assignment-timeout` (default 40s), `--poll-interval`
(default 2s).

## Verified behaviour

Tested against a stubbed CyberYozh that reproduces the async assignment:

- **Async assignment** — POST returns no number, the relay polls the detail
  route and returns the number once assigned.
- **Upstream errors pass through** — a `400 no available numbers` from
  CyberYozh arrives at Inbox as `400 no available numbers`, not as a relay
  failure. That distinction matters: it's the difference between "they're out
  of stock" and "your relay is broken".
- **Timeout** — if no number is assigned within the window, it returns `504`
  with the order id, so the order can be chased manually rather than lost.

## Worth knowing

It binds to `127.0.0.1`, so nothing outside your machine can reach it. It
holds no credentials of its own. If Inbox reports a connection error on
purchase, the relay window has almost certainly been closed.
