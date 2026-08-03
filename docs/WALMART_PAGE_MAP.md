# Walmart sign-in and add-phone: what the pages actually look like

Everything here came from real runs against the live site, not from guesswork.
It is written to be usable by anything — this tool, AYCD, a hand-written
script — because the hard-won part isn't the code, it's knowing what each
screen contains and which control does what.

Recorded 2026-08-02. Walmart changes these pages; the wording has survived
several rounds but the ids have not.

## The one rule

**Never pin a `#react-aria...` id.** They look like
`#react-aria4671517987-:r6:` and are regenerated on every page load. Every
selector below is chosen to survive that: `name`, `aria-label`,
`data-testid`, or the visible wording.

---

## Screen 1 — email entry

`https://www.walmart.com/account/login` redirects to
`https://identity.walmart.com/account/login?tp=RequestOidcCompliantIdentityTp&client_id=…&code_challenge=…`

| What | Selector | Notes |
|---|---|---|
| Email field | `input[name='Phone number or email (required)']` | `type="text"`, **not** `type="email"` |
| Continue | `#login-continue-button` | Disabled until the field has content |
| Hidden username | a second `input[type=text]` | Not visible; for password managers |

Also present: `label[data-testid='auth-phone-or-email-label']`,
`div[data-testid='hidden-input-phone-email-input']`,
`div[data-testid='dropdown']`, and 3–5 iframes including
`#tmx_tags_iframe` (ThreatMetrix device fingerprinting).

**The PerimeterX bot check appears on this screen**, container `#px-captcha`.
It needs a human. A persistent browser profile means solving it roughly once
per profile rather than every run.

Counts when healthy: `input=2 email-ish=1 password=0 button=2`.

### After Continue

The next screen renders **in place** — no `load` or `networkidle` event
fires. Anything waiting on a load event reads the old page. Wait for the URL
to change, or for the next screen's own contents to appear.

---

## Screen 2 — "Choose a sign in method"

`https://identity.walmart.com/account/signin/withotpchoice?…`

Counts on arrival: `input=5 email-ish=1 password=1 button=4`.

The five inputs are: two radios, the hidden username, a password field
(`aria-label='Enter your password'`), and a "Keep me signed in" checkbox.

| What | How to find it | Notes |
|---|---|---|
| Email-code option | label with text `Email me a verification code <address>` | A radio + label. The radio itself has **no text** — the wording is on the label |
| Password option | label with text `Password` | The default |
| **Request code** | visible control with text `Request code` | This is what sends the email |
| Sign in (password) | `#withpassword-sign-in-button` | `type=submit`. **Do not press this on the code path** — it submits an empty password |
| Show/hide password | `#withpassword-show-hide-password-button` | |
| Change email | `a[aria-label='Change Email address, link']` | → `/account/login` |

Containers: `div[data-testid='identity-generic-choice-options']`,
`div[data-testid='auth-choice-title']` ("Choose a sign in method"),
`div[data-testid='auth-enter-pwd-field']`.

### The sequence that works

1. Click the **email-code label** (or check its radio). The page re-renders:
   the password field disappears, `Request code` appears.
2. Click **Request code**. Only now is the email sent.

Selecting the option alone sends nothing. Two separate actions.

### Traps on this screen

- The heading container's text reads
  `Choose a sign in method Email me a verification code <address> Password`.
  A text search that takes the first or longest match grabs this wrapper,
  and clicking it does nothing. Take the **shortest** matching element.
- `Sign in` is a visible `button[type=submit]` here. "First submit button on
  the page" is the wrong rule.
- Never pick the SMS option when the code is being read from a mailbox.

---

## Screen 3 — code entry

`https://identity.walmart.com/account/signin/otponly?…`
(sometimes `/account/verifyitsyou` first)

Counts: `input=7 password=0 button=3`.

**Six separate boxes**, one character each, plus the hidden username. They
carry **no `maxlength="1"`** — the usual way of detecting a segmented field
misses them. Filling the whole code into the first box gets it rejected as an
incorrect code.

Either put one digit in each box, or focus the first and type the code with
the keyboard so the page advances focus itself.

| What | Notes |
|---|---|
| **Resend code** | Also a submit button. Pressing it issues a new code and invalidates the one just typed — which looks exactly like the code being wrong |
| Verify / continue | Some layouts submit themselves once the last box is filled |

Submitting starts an **async check plus a redirect chain**. Reading the URL
immediately after says "still on the identity host" while it is in flight.
Poll for up to ~45s.

### Walmart may then ask for the password as well

Answering the code isn't always the end. A password screen can follow it.

---

## Screen 4 — "Make it easier to sign in"

Appears after sign-in. Offers to text a one-time code to a phone next time:
a phone field, **Text me**, **Call me instead**, **Not now**.

Click **Not now**. That phone field is for sign-in convenience, not the
account's phone number — it is not the form you want.

---

## Reading the code out of the mailbox

The catch-all addresses carry a plus-tag: `bjoey3739+668331@gmail.com`.

`668331` is six digits with a word boundary either side (`+` before, `@`
after), and Walmart's email contains the recipient address — usually **above**
the code. A bare `\b(\d{6})\b` therefore matches the address tag, and the
resulting failure looks like a delivery problem rather than a parsing one.

Strip email addresses and URLs first, then prefer a number sitting just after
words like "verification code" / "one-time passcode". Order numbers and dates
elsewhere in the message are equally six digits.

---

## Detecting whether you are signed in at all

Walmart runs sign-in on `identity.walmart.com`. Any account page redirects
there when the session is dead, and an already-signed-in visitor gets bounced
off the sign-in page. So:

- On `identity.walmart.com`, or a URL containing `/account/login` or
  `/account/signin` → signed out.
- Otherwise → signed in, **unless a sign-in form is visible**. The URL alone
  is not proof; check for the email field before believing it.

A persistent profile usually keeps the session between runs, which skips both
the sign-in and its bot check.

---

## Still unknown

The add-phone form itself — `walmart.com/account/profile` or wherever it
lives. No run has reached it yet, so `add_phone_button`, `phone_input`,
`phone_submit`, `code_input` and `code_submit` are still placeholders.
