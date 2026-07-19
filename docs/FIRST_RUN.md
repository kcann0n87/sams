# First run — proving the flow

Goal: get **one** real account to go start-to-finish with the automation typing
the login and the member details for you, pausing only if a "press and hold"
CAPTCHA appears. Do this on your own machine.

## Why this setup

Sam's Club's "press and hold" is bot detection (PerimeterX). It hard-blocks
stripped-down automation browsers even if a human does the hold. The way around
it is to **be a real browser**: the automation drives your installed Google
Chrome, using a persistent profile that you warm up by passing one challenge by
hand, from your normal home IP. After that the profile is usually trusted.

That's already the default in `config.example.yaml`:

```yaml
browser:
  channel: "chrome"
  user_data_dir: "chrome-profile"
  stealth: true
```

## 1. Install

```bash
cd ~/sams
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium   # bundled browser (fallback); real Chrome is used via channel
```

You also need Google Chrome installed normally (you already have it).

## 2. Fill in one account

```bash
cp config.example.yaml config.yaml
cp accounts.example.csv accounts.csv
```

- **`config.yaml`** → fill the `imap:` block with your iCloud email + an
  **app-specific password** from [appleid.apple.com](https://appleid.apple.com).
- **`accounts.csv`** → replace the examples with **one** real row.

Check it and confirm iCloud connects:

```bash
python -m sams_automation check
python -m sams_automation test-imap
```

## 3. Get the real selectors (CAPTCHA-free)

The automation needs to know which field is which on the real page. Easiest way
that doesn't trip any bot check:

1. In your **normal Chrome**, log into one account and go to the page where you
   add the complimentary / secondary member.
2. Right-click the page → **Save As** → "Web Page, HTML Only" → save it.
3. Send me that `.html` file (it's just the page markup — no passwords).

I'll read the field names out of it and fill in every selector and the URLs in
`config.yaml`. Also tell me the exact URL of that add-member page.

## 4. Get one real verification email

Trigger one member invite by hand (or we'll trigger it in step 5), then:

```bash
python -m sams_automation test-imap --to the.secondary@yourdomain.com
```

Paste me what it prints (or forward the email). I need to see whether Sam's Club
sends a **numeric code** or an **activation link**, so I set the mode + regex.

## 5. First automated run — warm the profile

```bash
python -m sams_automation run --limit 1
```

Real Chrome opens with the `chrome-profile` folder. If a press-and-hold appears,
**solve it by hand** — the script waits for you, then continues. Once you've
passed it, that profile is warmed, so later accounts should see it rarely or not
at all. The script fills the login, fills the member form, grabs the code from
your inbox, and enters it. It screenshots each step into `screenshots/`.

We iterate on anything that trips using those screenshots until one account goes
fully green. After that:

```bash
python -m sams_automation run          # the rest of the list
```

and it skips any account already marked `ok` in `results.csv`, so you can stop
and resume anytime.

## Reality check

This is the best legitimate shot at hands-off runs, but it's not guaranteed —
PerimeterX can still challenge, and if it ever hard-blocks even your warmed real
Chrome, the fallback is the fully manual path plus `python -m sams_automation
watch`, which live-prints each code as it lands so you at least never dig
through email.
