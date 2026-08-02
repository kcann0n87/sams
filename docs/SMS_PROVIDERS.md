# SMS verification providers — availability checker

`sms-check` asks each provider whether it currently has numbers that can
receive a **Walmart** (or Sam's Club) verification code, and what they cost.
It's read-only: it never buys a number.

```bash
# What's wired up, and which env var each site looks for
python -m sams_automation sms-check --list-providers

# One-shot check across everything configured
python -m sams_automation sms-check

# Walmart US stock is intermittent — sit on the feed and get pinged (bell)
# the moment a pool refills
python -m sams_automation sms-check --watch --interval 60
python -m sams_automation sms-check --watch --once-in-stock   # exit on first hit

# Works with no config at all — 5sim's price feed needs no account
python -m sams_automation sms-check --provider 5sim

# Wider net
python -m sams_automation sms-check --all-countries --in-stock-only
python -m sams_automation sms-check --json > availability.json
```

Exit code is `0` when at least one provider has stock, `1` when nothing is
available, `2` on a usage error — so it drops straight into a cron job or an
`if` in a shell script.

## The web UI (easiest way in)

Entering a dozen API keys on the command line is miserable, so the local web UI
has a page for it:

```bash
python -m sams_automation serve      # opens your browser
```

Then click **SMS providers & Walmart stock** in the header, or go straight to
<http://127.0.0.1:8765/sms>. The page gives you:

- **A key form for every provider**, showing which already have a key and
  whether it came from the file or your shell environment.
- **A one-line verdict** — "in stock, cheapest $0.35 at daisysms" or "no stock;
  4 providers offer Walmart but the pools are empty" — so you don't have to
  read four tables to answer the only question that matters.
- **Keep watching** — re-checks every 60s and fires a browser notification,
  a sound and an on-page banner the moment a pool refills.

### Where keys are stored

Keys typed into the page go to **`sms_keys.json`** in the project folder:
git-ignored, `chmod 600`, and never sent back to the browser — the page only
ever sees that a key is set plus its last four characters. The server binds to
`127.0.0.1`, so nothing is reachable from your network.

Precedence is `sms_keys.json` > `config.yaml` > environment, so a key typed
into the UI overrides a stale exported one. Clearing the field falls back to
whatever config or the environment provides rather than blanking it.

## The landscape (researched 2026-08)

> **SMS-Activate shut down on 2025-12-29.** It was the biggest provider and is
> still what most tutorials and older code target, so anything pointing at
> `sms-activate.org/stubs/handler_api.php` is dead. Balances had to be
> withdrawn by 2026-01-29 with a $30 minimum, and accounts did not carry over.
> The team moved its infrastructure to **HeroSMS**, which speaks the same API
> protocol — hence `herosms` below rather than `sms-activate`.

| Provider | Key needed to check stock? | Numbers | Notes |
|---|---|---|---|
| **5sim** | **No** | Virtual, many countries | Only one with a public price+stock feed. Prices in **RUB**. Best for a quick "is it available anywhere" poll. |
| **daisysms** | Yes | US **non-VoIP** | Cheap, US-only, charged only when a code actually arrives. Strongest fit for a US retailer. |
| **textverified** | Yes | US **non-VoIP** | Priciest, most reliable reputation; explicitly advertises Walmart. Reports no stock count — a listed service is orderable. |
| **smspool** | Yes | Mixed VoIP/non-VoIP | Reports live stock counts and success rates. |
| **herosms** | Yes | Virtual, 180+ countries | SMS-Activate's successor and infrastructure. Newest, least track record. |

**Why non-VoIP matters here.** Walmart rejects a large share of VoIP numbers at
signup. A provider offering a "Walmart" service at a low price on VoIP stock
can still fail every time, so `daisysms` and `textverified` are the two worth
trusting for this specific target, with `5sim` as the free availability signal.

## When nothing has US Walmart stock

This is the normal state, not a bug. US Walmart pools on the resale market are
thin and get drained fast, so a one-shot check returning nothing says little.
The tool separates the three reasons so you can tell them apart:

| Output | Meaning |
|---|---|
| `qty=0` | Provider offers Walmart; pool is empty right now. **Watch it.** |
| `no Walmart-like service found in this catalog` | Provider doesn't sell it at all. Stop checking. |
| `ERROR ...` | Key, network or endpoint problem — not a stock signal. |

If the first row is what you're seeing everywhere, `--watch` is the answer:
restocks are short and unannounced, and the bell fires the moment one lands.
Worth trying alongside it:

- **`--all-countries`** — confirms the service exists at all and that only the
  US pool is dry.
- **A wider net.** Stock is uncorrelated between resellers; the more keys
  registered, the better the odds any one of them catches a refill.
- **Check Sam's Club separately.** It's matched by default but is often a
  distinct service code with its own, sometimes healthier, pool.

**A price is not a guarantee.** Every one of these is a resale pool. Stock and
delivery rates swing hour to hour, which is the reason this is a checker you
re-run rather than a table you read once.

## Pay-per-verification only — no rentals

Every provider here sells two different things:

- **Activation / verification** — one number, one code, pay per use (~$0.35).
- **Rental / hosting** — a dedicated number you keep for a day to a year.

This tool only ever reports the **first**. A rental listed next to a per-code
price isn't a like-for-like comparison, and a rented number reused across
several signups tends to get rejected anyway.

Each adapter enforces that at the endpoint it calls:

| Provider | What keeps rentals out |
|---|---|
| 5sim | catalog rows with `Category: hosting` are skipped (noted in output) |
| textverified | queries `reservationType=verification` |
| daisysms | uses `getPricesVerification` |
| activate family | `getPrices` is the activation feed; rentals live behind `getRent*` actions |
| smspool | `request/price` is the per-verification price; rentals are separate endpoints |

If a provider ever starts mixing the two, that's a bug — the output should say
`skipped <product> (hosting, not pay-per-code)` rather than quietly listing it.

## How service discovery works

Each provider names the service differently — `walmart`, `wm`, `1023` — and
renumbers over time. So no code is hard-coded: each adapter pulls the
provider's own catalog and keeps entries whose name matches `walmart` or
`sam's club` (punctuation- and case-insensitive, so `Sam's Club`, `sams_club`
and `SAMSCLUB` all hit).

The practical payoff: if a provider drops or renames the service, you get
`no Walmart-like service found in this catalog` instead of a silent "0 in
stock" that looks like a temporary shortage.

`count` is kept distinct from zero on purpose:

- `qty=0` — the provider says the pool is empty.
- `qty=-` — the provider doesn't report counts (TextVerified). Treated as
  available, because a service it lists is orderable.

## Configuration

Most sites need nothing but a key, and each one reads its own environment
variable — so an existing shell setup works with no config file at all:

```bash
export DAISYSMS_API_KEY=...
export SMSBOWER_API_KEY=...
python -m sams_automation sms-check --list-providers
```

Registered sites on the shared `activate` (`stubs/handler_api.php`) protocol —
key alone is enough, the base URL is already known:

| Site | Env var | Site | Env var |
|---|---|---|---|
| smsbower | `SMSBOWER_API_KEY` | sms-man | `SMSMAN_API_KEY` |
| tiger-sms | `TIGERSMS_API_KEY` | smshub | `SMSHUB_API_KEY` |
| grizzly-sms | `GRIZZLYSMS_API_KEY` | sms-acktiv | `SMS_ACKTIV_API_KEY` |
| hero-sms | `HEROSMS_API_KEY` | simsms | `SIMSMS_API_KEY` |
| daisy-sms | `DAISYSMS_API_KEY` | 5sim-activate | `FIVESIM_ACTIVATE_API_KEY` |
| sms-activation | `SMS_ACTIVATION_SERVICE_API_KEY` | | |

Plus the four with dedicated adapters: `5sim` (no key), `textverified`
(`TEXTVERIFIED_API_KEY` + `TEXTVERIFIED_USERNAME`), `daisysms`, `smspool`
(`SMSPOOL_API_KEY`).

Anything in config wins over the environment:

```yaml
sms_providers:
  rub_per_usd: 90.0        # only to rank 5sim's RUB prices against USD
  daisysms:
    api_key: "..."
  tiger-sms:
    base_url: "https://api.tiger-sms.com"   # override if the site moves
  smshub:
    enabled: false                          # skip without unsetting the key
```

### Working out what protocol a site speaks

Resellers rarely advertise "we're SMS-Activate compatible" — you're expected to
already know. With a dozen keys across a dozen sites that's a lot of guessing,
so `sms-probe` tries each known protocol and reports what actually parsed:

```bash
export MYSITE_KEY=...          # keeps the key out of your shell history
python -m sams_automation sms-probe https://api.some-site.com \
    --name some-site --key-env MYSITE_KEY
```

```
  MATCH  sms-activate   1 Walmart offer(s)
                           Walmart · USA · 0.45 USD · qty=12
  no     5sim           HTTP 404: Not Found
  no     smspool        HTTP 404: Not Found

Best match: sms-activate
```

It prints the exact `custom:` block to paste. Results rank protocols that found
a Walmart service above ones that merely authenticated, so a site that answers
on two protocols still points you at the useful one.

If nothing matches, add `--show-responses` to see every request tried and what
came back. That turns the probe into a discovery tool for an undocumented API:

- A **401/403** on a path means the path exists and only the auth style is
  wrong — worth trying a different header.
- **HTML or 404 everywhere** means the API lives at a different base URL.

The capture strips `api_key` from recorded parameters, so the output is safe to
paste to someone for diagnosis.

### When a provider publishes no API docs at all

Some do have an API but document nothing. Their own dashboard is a web app, so
it uses that API in front of you:

1. Open the provider's dashboard, then DevTools → **Network**, filter **Fetch/XHR**.
2. Do the thing you want to automate — list services, buy a verification.
3. Click the request that fires. **Headers** shows the base URL and auth style;
   **Response** shows the JSON shape.
4. Right-click → *Copy as cURL*, then delete the key before sharing it.

That's usually a two-minute job and it produces better information than most
published docs.

### Adding a site that isn't registered

Nearly every reseller is a clone of one of four APIs, so this needs no code —
name the protocol and the host:

```yaml
sms_providers:
  custom:
    - name: "some-new-site"
      protocol: "sms-activate"   # aka handler_api — by far the most common
      base_url: "https://api.some-new-site.com"
      api_key: "..."
```

Known protocols: `sms-activate` (alias `handler_api`), `5sim`, `smspool`,
`textverified`. A bad entry reports itself as a config warning and the rest of
the run continues.

A provider with no key is skipped rather than reported as failing. Without
`rub_per_usd`, 5sim's offers are still listed — they're just left out of the
"cheapest" ranking rather than compared at a made-up rate.

## If a provider starts erroring

The adapters are defensive about response shapes but these APIs do change, and
several of them report real errors (`BAD_KEY`, `NO_BALANCE`) as a bare string
with a non-2xx status. Those surface verbatim:

```
  herosms        ERROR  HTTP 401: BAD_KEY
```

To re-check a payload shape, `--json` prints what was parsed. The endpoint
paths all live in `src/sams_automation/sms_providers.py` next to the adapter
that uses them, and `base_url` is overridable per provider in config if one
moves domains. `tests/test_sms_providers.py` holds a fixture of every response
shape — correcting the fixture there is the quickest way to see how a parser
reacts to a new payload, with no network involved.

Endpoint confidence, since these were reconstructed from published docs and
client libraries rather than live calls:

- **High** — 5sim guest endpoints; the `stubs/handler_api.php` protocol shared
  by DaisySMS/HeroSMS; SMSPool's `retrieve_all` + `request/price`.
- **Medium** — the registered `base_url` for the less common handler_api
  sites, which do change domains; each is overridable in config, and a wrong
  one shows up immediately as an HTTP error rather than a silent zero.
- **Medium** — TextVerified's `/pricing/verifications` path. Its auth flow and
  services list are confirmed by two independent client libraries; the
  per-service pricing path is inferred. It's only used as a fallback when the
  services list omits `cost`, so the common path doesn't depend on it.

## Scope

This tells you what's *purchasable*. It does not buy numbers, and ordering a
number is a separate step against each provider's purchase endpoint. Whether
using one is consistent with Walmart's or Sam's Club's terms is the same
judgement call flagged in the main README.
