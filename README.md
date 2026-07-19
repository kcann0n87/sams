# Sam's Club secondary-membership automation

Registers the complimentary secondary member that comes with each Sam's Club
membership you own, driving the browser and pulling the verification code out of
your iCloud catch-all mailbox automatically so you don't have to do it by hand
for every account.

> **Run this on your own machine, in a visible browser.** Sam's Club uses
> CAPTCHAs and bot detection. The script pauses and lets you solve a CAPTCHA
> when one appears (headful mode), then continues. Running it headless will
> stop at the first CAPTCHA. Keep your passwords local — `config.yaml` and
> `accounts.csv` are git-ignored.

## What it does

For each row in `accounts.csv` it:

1. Logs into the **primary** account (reusing a saved session when possible).
2. Opens the *add a member* form and fills in the **secondary** profile
   (name + email + address).
3. Submits, then watches your iCloud catch-all inbox for the Sam's Club
   verification email addressed to that secondary email.
4. Extracts the **code** (or **activation link**) and completes verification.
5. Records the outcome in `results.csv` and screenshots every step under
   `screenshots/`.

It skips accounts already marked `ok` in `results.csv`, so you can stop and
resume any time.

## Setup

```bash
# 1. Python deps + browser
python -m pip install -r requirements.txt
playwright install chromium

# 2. Your config and profile list (both git-ignored)
cp config.example.yaml config.yaml
cp accounts.example.csv accounts.csv
```

Then edit:

- **`config.yaml`** — your iCloud IMAP login. For iCloud the host is always
  `imap.mail.me.com`; the username is your Apple ID email; the password must be
  an **app-specific password** generated at
  [appleid.apple.com](https://appleid.apple.com) (Sign-In and Security →
  App-Specific Passwords). Your normal Apple ID password will not work over IMAP.
- **`accounts.csv`** — one row per membership: the primary login plus the
  secondary member's name, email (on your catch-all domain), and address.

## Verify before you run anything

```bash
# Validate config + account list without touching the network
python -m sams_automation check

# Test the iCloud IMAP connection (lists your folders)
python -m sams_automation test-imap

# ...and, after you manually trigger one Sam's Club email, confirm we can read
# the code out of it:
python -m sams_automation test-imap --to jane.doe@yourdomain.com
```

## Proxies (avoid hitting the site from one IP)

So you're not registering every membership from the same IP, each account can
route through its own proxy.

1. `cp proxies.example.txt proxies.txt` and paste your proxies (one per line —
   `host:port`, `host:port:user:pass`, or a full `scheme://user:pass@host:port`
   URL all work). `proxies.txt` is git-ignored.
2. In `config.yaml` under `proxies:`, keep `enabled: true`.
3. Confirm they actually work and see each exit IP:

   ```bash
   python -m sams_automation test-proxy
   ```

**Rotation** (`proxies.rotation` in config):

- `sticky` *(default, recommended)* — a given primary account always exits
  through the same proxy. A stable IP per account looks natural; an account
  that hops between IPs every run looks suspicious.
- `round_robin` — cycle through the list across accounts.
- `random` — pick one at random per account.

To run without proxies, set `proxies.enabled: false`.

## Run it

```bash
# Do a single account first and watch the browser
python -m sams_automation run --limit 1

# Then the rest
python -m sams_automation run
```

Useful flags: `--only someone@yourdomain.com` (one specific row),
`--no-resume` (don't skip completed rows), `--headless` (no window — only once
the flow is proven and CAPTCHA-free).

## Tuning to the real page (we do this together)

The URLs and every selector live in `config.yaml` under `sams:` — the Python
never needs editing. The values shipped are **placeholders**. The plan:

1. Run `run --limit 1` with `headless: false`.
2. It screenshots each step into `screenshots/`. Where it can't find a field,
   the error names the selector key (e.g. `add_email`).
3. Open the real page's dev tools, copy the correct selector, paste it into
   `config.yaml`, re-run. Repeat until a full account goes green.

Two things we'll confirm from a real verification email:

- **`verification.mode`** — `code` if the email contains a number you type in,
  or `link` if it contains an activation URL to click. One-line toggle.
- **`verification.code_regex`** — defaults to a 6-digit code; adjust if Sam's
  Club uses a different format.

## Layout

```
config.example.yaml     # copy to config.yaml (git-ignored)
accounts.example.csv    # copy to accounts.csv (git-ignored)
src/sams_automation/
  config.py             # load + validate config and the account list
  imap_client.py        # poll the catch-all mailbox, extract code/link
  sams_flow.py          # Playwright login -> add-member -> verify
  runner.py             # loop over accounts, log results, pace requests
  cli.py                # run / check / test-imap commands
tests/                  # offline unit tests for the parsing logic
```

## Notes / limitations

- This automates a workflow on accounts **you own**; it does not bypass any
  paywall or access anything you aren't entitled to. Automated interaction may
  still be against Sam's Club's terms of service — that's on you to check.
- CAPTCHAs need a human. The script waits for you; it does not solve them.
- If Sam's Club changes its markup, update the selectors in `config.yaml`.
