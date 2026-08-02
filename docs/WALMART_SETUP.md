# Walmart phone verification — setup

Start to finish. Roughly 20 minutes, most of it waiting on Gmail.

Order matters: each step is checkable on its own, so a mistake shows up
immediately rather than three steps later as a confusing browser failure.

---

## 1. Get the code running

```bash
cd ~/sams
git pull
cp config.example.yaml config.yaml
```

Check it works — this needs no keys, no accounts, and touches nothing:

```bash
./start.sh sms-check --list-providers
```

You should see a list of providers and the environment variable each looks for.
If you get a Python error here, stop; nothing else will work until this does.

---

## 2. Gmail app password

Sign-in uses the one-time code Walmart emails, so the tool needs to read your
catch-all inbox. Gmail won't accept your normal password over IMAP.

1. Go to <https://myaccount.google.com/security>
2. Turn on **2-Step Verification** if it isn't already (required — app
   passwords don't exist without it)
3. Search settings for **App passwords**, or go to
   <https://myaccount.google.com/apppasswords>
4. Create one named `sams` and copy the 16 characters
5. In Gmail → Settings → **Forwarding and POP/IMAP** → enable **IMAP**

Put it in `config.yaml` under `walmart.imap`:

```yaml
walmart:
  imap:
    host: "imap.gmail.com"
    username: "you@gmail.com"
    password: "abcd efgh ijkl mnop"
    from_contains:
      - "walmart"
```

The spaces in the app password are fine — paste it as shown.

> This is a **second** IMAP block. The one at the top of `config.yaml` is the
> iCloud mailbox the Sam's Club flow uses. Don't overwrite it.

---

## 3. Accounts

```bash
open -e walmart_accounts.csv
```

One per line, colon-separated:

```
you+wm1@gmail.com:YourPassword1
you+wm2@gmail.com:YourPassword2
```

Everything after the **first** colon is the password, so passwords containing
colons or commas are safe. The password is optional — sign-in uses the emailed
code — so a bare email is a valid line.

Blank lines and `#` comments are ignored, and a malformed line is skipped with a
warning rather than stopping the run.

---

## 4. Proxies (optional but recommended)

```bash
cp walmart_proxies.example.txt walmart_proxies.txt
open -e walmart_proxies.txt
```

Paste your list, one per line. All of these parse:

```
1.2.3.4:8080
1.2.3.4:8080:username:password
username:password@1.2.3.4:8080
socks5://1.2.3.4:1080
```

A 5,000-line file is fine. Duplicates are dropped and unparseable lines skipped
with a count. The default `fresh` rotation gives each account a different IP and
never reuses one until the pool wraps.

To skip proxies entirely, set `walmart.proxies.enabled: false`.

---

## 5. SMS provider keys

```bash
./start.sh
```

Your browser opens. Click **SMS providers & Walmart stock**, or go to
<http://127.0.0.1:8765/sms> (the terminal prints the real port — it picks the
next free one if 8765 is taken).

Paste each key into the table and hit **Save**. Keys go to `sms_keys.json`,
which is git-ignored, `chmod 600`, and never sent back to the browser.

Two special cases:

- **smspva** also needs a `service_code`. Its services are opaque `optNN` codes
  rather than names, so Walmart can't be found by matching. Look it up on
  smspva.com's service list and set it in `config.yaml`.
- **textverified** needs your account email as well as the key.

Then hit **Check now**. The verdict line tells you whether anything has Walmart
stock right now. If everything is empty, that's normal — tick **Keep watching**
and it'll alert you when a pool refills.

---

## 6. First run — free

Confirm these two in `config.yaml`:

```yaml
purchasing:
  dry_run: true      # nothing is bought

browser:
  headless: false    # so you can watch, and solve a bot check
```

Then:

```bash
./start.sh walmart-add-phone --limit 1
```

A browser opens and drives your first account. **It will fail**, and that's the
point of this run — every `walmart.selectors` entry ships as a placeholder.

You'll get something like:

```
[FAIL] you@gmail.com  no selector configured for 'login_use_code' —
       set walmart.selectors.login_use_code in config.yaml
```

plus a screenshot of each step in `screenshots/walmart-*.png`.

**Send me that error line and those screenshots.** Correcting the page map is a
config edit, never a code change.

---

## 7. Tuning the page map

Repeat until a run gets all the way through:

1. Run `./start.sh walmart-add-phone --limit 1`
2. Read the error — it names the exact config key
3. Find that element on the real page (right-click → Inspect) and paste a
   selector into `config.yaml`
4. Run again

The keys, in the order the flow hits them:

| Key | What it points at |
|---|---|
| `login_email` | the email box on the sign-in page |
| `login_continue` | "Continue" between email and password (blank if one screen) |
| `login_use_code` | **the "send me a code" option** — set this and the password is never typed |
| `login_code_input` | where the emailed code goes |
| `login_code_submit` | the button that submits it |
| `add_phone_button` | "Add phone" on the profile page (blank if the URL lands on the form) |
| `phone_input` | the phone number box |
| `phone_submit` | the button that sends the SMS |
| `code_input` | where the SMS code goes |
| `code_submit` | the button that confirms it |
| `logged_in_marker` | anything that only exists when signed in |
| `success_marker` | anything shown once the number is confirmed |

Leave `logged_in_marker` and `success_marker` empty until last — set too early,
a correct run looks like a failure.

---

## 8. Going live

Once a dry run reaches the phone form cleanly:

```yaml
purchasing:
  enabled: true
  dry_run: false
  max_price_usd: 1.00
  max_total_usd: 5.00
```

Both switches are required — enabling purchasing and actually spending are
deliberately separate decisions. Start with a low `max_total_usd`; it caps the
whole run, not each account.

```bash
./start.sh walmart-add-phone --limit 1
```

When that works, drop `--limit`. Results land in `walmart_results.csv`, which
also drives resume: re-running skips accounts that already succeeded.

Purchases are logged to `purchases.csv` *before* the code is polled for, so a
crash never loses a number you paid for.

---

## When something goes wrong

| Symptom | Cause |
|---|---|
| `command not found: python` | use `./start.sh`, not `python` — macOS has only `python3` |
| Providers vanish from the web page | stale server; Ctrl-C and `./start.sh` again, then hard-refresh |
| `no sign-in code arrived` | app password wrong, IMAP off in Gmail, or `from_contains` doesn't match the sender |
| `no provider delivered a code` | every pool tried and none delivered — genuinely out of stock, not a bug |
| `refusing to buy blind` | the offer had no USD price. Set `sms_providers.rub_per_usd` for 5sim |
| A bot check appears | solve it in the window; the run waits up to 5 minutes |

Nothing here bypasses a CAPTCHA — the flow pauses for a human every time.
