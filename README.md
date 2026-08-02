# Sam's Club secondary-membership automation

Registers the complimentary secondary member that comes with each Sam's Club
membership you own, driving the browser and pulling the verification code out of
your iCloud catch-all mailbox automatically so you don't have to do it by hand
for every account.

> **Run this on your own machine, in a visible browser.** Sam's Club uses a
> "press and hold" bot check (PerimeterX) that hard-blocks stripped-down
> automation browsers. To get around it the tool drives your **real Google
> Chrome** with a persistent profile you warm up by solving one challenge by
> hand (see `browser.channel` / `browser.user_data_dir` in the config). The
> script pauses for you on a CAPTCHA, then continues. It's the best legitimate
> shot at hands-off runs, not a guaranteed bypass. Keep your passwords local —
> `config.yaml` and `accounts.csv` are git-ignored.
>
> If it ever gets fully walled, there's a manual fallback: do the browser part
> yourself and run `python -m sams_automation watch`, which live-prints each
> verification code the instant it lands so you never dig through email.

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
# 1. Python deps + browser (installs the package so `python -m sams_automation` works)
python -m pip install -e .
playwright install chromium
# (No install? Run in place with:  export PYTHONPATH=src )

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

### Manual fallback: just watch for codes

If you'd rather (or have to) do the browser part by hand, this tails your
catch-all inbox and prints each Sam's Club code as it arrives, copying it to
your clipboard so you just paste:

```bash
python -m sams_automation watch                       # all secondary emails
python -m sams_automation watch --to jane@yourdomain.com   # just one
```

Ctrl-C to stop. `--no-copy` disables the clipboard copy.

## Checking SMS providers for Walmart numbers

Separate from the membership flow, `sms-check` polls SMS-verification providers
(TextVerified, 5sim, DaisySMS, SMSPool, and a dozen sites on the shared
"activate" protocol) and reports which ones actually have Walmart numbers in
stock, and at what price. It's read-only — it never buys a number.

This part is standalone: it shares nothing with the Sam's Club membership flow
above beyond living in the same repo.

Easiest way in is the local web UI — a form for all the API keys, a live
availability table, and a watch mode that alerts you when a pool refills:

```bash
./start.sh          # sets up its own Python env on first run, then opens the UI
```

`start.sh` exists because macOS has no `python` (only `python3`) and refuses
`pip install` outside a virtualenv, which makes the plain commands below fail in
a confusing way on a fresh Mac. It installs only PyYAML and Flask — Playwright
is not needed for the SMS side.

Or from the terminal:

```bash
./start.sh sms-check --list-providers   # what's wired up
./start.sh sms-check                    # one-shot check
./start.sh sms-check --watch            # alert on restock
```

Keys are entered in the web UI (stored in git-ignored `sms_keys.json`), or come
from your environment (`DAISYSMS_API_KEY`, `SMSBOWER_API_KEY`, ...) or the
`sms_providers:` block in `config.yaml`. 5sim needs no account at all,
so `--provider 5sim` works out of the box.

US Walmart pools are frequently empty — `--watch` exists because catching a
restock matters more than any one-shot price comparison. Details, the full
provider list, and how to add a site in config without writing code:
**[docs/SMS_PROVIDERS.md](docs/SMS_PROVIDERS.md)**.

## Adding a phone number to Walmart accounts

**Setting this up for the first time: [docs/WALMART_SETUP.md](docs/WALMART_SETUP.md)**
— Gmail app password, accounts, proxies, provider keys, and the selector-tuning
loop, in order.


Buys a verification number and adds it to each account in
`walmart_accounts.csv`, logging in through that account's proxy.

```bash
cp walmart_accounts.example.csv walmart_accounts.csv
```

Then edit `walmart_accounts.csv`. One account per line, colon-separated:

```
you@gmail.com:YourPassword
you@gmail.com
```

Everything after the **first** colon is the password, so passwords containing
colons or commas are safe. The password is optional — sign-in uses the code
emailed to your catch-all, so an email on its own is a valid line. Proxies come
from `walmart_proxies.txt`, not from here.

Do a single account first, with a visible browser:

```bash
./start.sh walmart-add-phone --limit 1
```

Leave `purchasing.dry_run: true` for that first pass. It goes all the way to
the phone form and reports exactly what it would have bought, without spending
anything.

**The first run will fail on a selector, and that's the point.** Every entry in
`walmart.selectors` ships as a placeholder, so you'll get something like:

```
[FAIL] you@gmail.com  no selector configured for 'phone_input' —
       set walmart.selectors.phone_input in config.yaml
```

along with `screenshots/walmart-*.png` for each step. Correct the selector in
`config.yaml` and re-run. No code changes, same loop as the Sam's Club flow.

Order of operations matters here: the number is bought only once the browser is
on the phone form, because a bought number starts expiring immediately. A failed
login costs nothing.

`walmart_results.csv` records each outcome and drives resume, so re-running
skips accounts that already succeeded (`--no-resume` to redo them).

## Getting it working the first time

See **[docs/FIRST_RUN.md](docs/FIRST_RUN.md)** — a step-by-step for proving the
flow on one real account, including using Playwright's recorder to capture the
real page's selectors in one pass.

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
  sms_providers.py      # SMS provider adapters + Walmart availability check
  web_sms.py            # the /sms page: API keys, stock table, watch mode
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
