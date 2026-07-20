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

You give it **one CSV** — one row per membership — and it runs **both sides** of
each row end to end:

**Phase 1 — the main account adds the member.** It logs into the **primary**
account (reusing a saved session when possible), opens the *add a member* form,
fills in the new member's name + email (+ phone), and submits. That triggers the
activation email.

**Phase 2 — the new member activates their own membership.** It watches your
iCloud catch-all inbox for the Sam's Club email addressed to that member,
extracts the **code** (or **activation link**), and — by default in a fresh
browser session, as if the member did it on their own device — opens it and sets
the member's **own password** (from the `secondary_password` column).

It records each outcome in `results.csv`, screenshots every step under
`screenshots/`, and skips rows already marked `ok`, so you can stop and resume
any time. This is built so one person can run it for the **whole family** from a
single uploaded list — nobody else has to touch it.

The two-phase behavior is configurable under `activation:` in `config.yaml`
(`new_session: true` runs phase 2 as a separate session; set it `false` to keep
everything in the primary's one session).

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
- **`accounts.csv`** — one row per membership: the primary login plus the new
  member's name, email (on your catch-all domain), phone, and the password the
  new member's account should use (`secondary_password`).

## The easy way: the web UI (upload a CSV, click a button)

If you'd rather not use the terminal — the setup for the rest of the family —
start the local web page and drive everything with buttons:

```bash
python -m sams_automation serve
```

It opens `http://127.0.0.1:8765/` in your browser, where you can:

- **Download a blank CSV template**, fill it in, and **drag & drop it back**
  (or click to pick it). The list is validated the instant you upload — missing
  columns or empty rows are rejected with a plain-English message, and you get a
  **preview table** (passwords masked) confirming exactly what will run.
- Paste your iCloud **app password**, **test the email connection**, then
  **Run 1 account** or **Run all accounts** — solving the one-time "press & hold"
  check in the Chrome window when it appears.
- Watch **live progress**, per-step **screenshots**, and a **Results** table
  showing which members are done.

Everything stays on your machine; the page just runs the same commands below for
you. The uploaded list is saved as `accounts.csv` (the previous one is kept as
`accounts.csv.bak`).

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
