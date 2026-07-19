# First run — proving the flow

Goal: get **one** real account to go start-to-finish, and capture what I need to
wire the automation to the real Sam's Club page. Do this on your own machine.

## 1. Install

```bash
cd sams
python -m pip install -r requirements.txt
playwright install chromium
```

## 2. Fill in one account

```bash
cp config.example.yaml config.yaml
cp accounts.example.csv accounts.csv
```

- **`config.yaml`** → put your iCloud details in the `imap:` block. The password
  must be an **app-specific password** from
  [appleid.apple.com](https://appleid.apple.com) (Sign-In and Security →
  App-Specific Passwords). If you're not using proxies yet, set
  `proxies.enabled: false` for this first test.
- **`accounts.csv`** → delete the two example rows and put in **one** real row:
  a primary login plus the secondary member's name, email (on your catch-all
  domain), and address.

Sanity-check it loads:

```bash
python -m sams_automation check
python -m sams_automation test-imap        # confirms iCloud IMAP connects
```

## 3. Record the real flow (this is the important part)

This opens a browser with a recorder. Everything you click is turned into a
script with the exact selectors — which is precisely what I need.

```bash
playwright codegen --target python -o recorded_flow.py https://www.samsclub.com
```

In the window that opens, do the whole thing **by hand, slowly**:

1. Log into one of your primary accounts.
2. Navigate to wherever you add the complimentary / secondary member.
3. Fill in that member's name, email, and address.
4. Submit — up to the point where it says it sent a verification email.

Then close the window. You'll have a file `recorded_flow.py`.

**Send me `recorded_flow.py`.** It contains selectors and URLs, but **no
passwords** (you can open it and check — the recorder captures what you clicked,
not your saved credentials). If you typed your password into a field during
recording and it shows up, just delete that one line before sending.

## 4. Grab one real verification email

After step 3 triggered a verification email, either:

```bash
python -m sams_automation test-imap --to the.secondary@yourdomain.com
```

and paste me what it prints, **or** forward me the email itself. I need to see
whether Sam's Club sends a **numeric code** or an **activation link**, and the
exact format, so I can set `verification.mode` and the regex correctly.

## 5. What I do with it

From `recorded_flow.py` + the sample email I'll:

- fill in every real selector and URL in `config.yaml`,
- set the verification mode (code vs link) and regex,
- flag anything that needs a decision (e.g. a CAPTCHA step, an unexpected
  confirmation dialog).

Then you run:

```bash
python -m sams_automation run --limit 1
```

and we iterate on any step that trips, using the screenshots it drops in
`screenshots/`, until one account goes fully green. After that, the rest of the
list is just `python -m sams_automation run`, and we can talk about wrapping it
in a local dashboard.

---

### If you'd rather not use the recorder

Just run `python -m sams_automation run --limit 1` with `headless: false`. It
screenshots every step into `screenshots/` and, wherever it can't find a field,
the error names the selector key (e.g. `add_email`). Send me the screenshots and
those errors and we'll fill selectors in that way instead — it's just slower.
