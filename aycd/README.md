# AYCD Inbox — Custom SMS schemas

Schemas for the SMS providers in this project, in AYCD Inbox's Custom SMS
format. Load one in Inbox: credentials dialog → provider **Custom** → **Load
Schema File...** → pick the `.json`.

These describe **provider APIs**. They have nothing to do with the Walmart
browser flow in the rest of this repo, and nothing to do with bot checks.

| File | Provider | AYCD likely has it? |
|---|---|---|
| `verifysms.json` | verifysms.io | No — bespoke API |
| `pvacodes.json` | beta.pvacodes.com | No — bespoke API |
| `secureiosms.json` | secureiosms.com | No — bespoke API |
| `smspool.json` | smspool.net | Possibly |
| `smsbower.json` | smsbower.online | Maybe |
| `grizzly-sms.json` | api.grizzlysms.com | Maybe |
| `simsms.json` | simsms.org | Maybe |
| `sms-acktiv.json` | sms-acktiv.ru | Maybe |
| `sms-activation.json` | sms-activation-service.com | Maybe |
| `smshub.json` | smshub.org | Maybe |
| `activate-protocol.json` | any other handler_api.php clone — edit the host | Some |
| `cyberyozh.json` | app.cyberyozh.com | No — and it sells **residential** numbers |

## How much to trust these

Be honest with yourself about this before spending money through one.

**Verified live.** Every provider above answered its catalogue endpoint during
this project's runs, so the hosts, auth style and service listings are real.
smspool returned 13 Walmart pools, verifysms 3, textverified 4.

**Not exercised.** The `getPhoneNumber` / `getMessage` / `cancelPhoneNumber`
paths have never been run — purchasing stayed off throughout. They are built
from each provider's published documentation and from this repo's adapters,
which is good evidence and not the same as proof.

**Guesses, flagged.** `getBalance` URLs other than the activate family's are
inferred from each API's conventions. If one 404s, delete the `generalApi`
block — it is optional and nothing else depends on it.

Buy one number through a schema and watch it before trusting it with a queue.

## Per-provider notes

### verifysms.json
Walmart is service code `fc`. The three carriers are genuinely separate pools
— one can be dry while another has stock — so there is a config for each
rather than one entry.

`/api/code` answers `409` until the code lands, which is what the
`STATUS_CODE` pending check keys on. It returns the code as a bare string, so
`responseType` is `TEXT` and the mapping strips surrounding quotes.

### pvacodes.json
Every reply is wrapped in a status envelope — `{"status": {"code": "1000"},
"data": …}` — and **failures arrive as HTTP 200**. `1000` is the only success.
The others: `1002` invalid key, `1003` insufficient balance, `2000` out of
stock, `429` rate limited.

`get_sms` is keyed by the **phone number**, not the order id, so the number is
mapped to a custom session key `numberValue`. Don't use `${session.phoneNumber}`
there — Inbox prepends the dial code to that value, which won't match.

If polling never completes, their "no SMS yet" reply probably carries a status
code rather than omitting `data`. Swap the pending check to:

```json
{"type": "FIELD_VALUE", "path": "$.status.code", "value": "2000"}
```

The app name is theirs, not a code. Confirm the exact spelling of Walmart from
`do=get_apps` before relying on the shipped config.

### secureiosms.json
Credits are deducted at purchase rather than on delivery, so cancelling
matters more here than elsewhere — `cancelPhoneNumber` refunds them.

Services are named, not coded. Confirm the exact Walmart spelling from
`/services_regions`.

### smspool.json
Everything is form-encoded, and both ids are numeric: Walmart is `999`,
Walmart Family Mobile is `1224`, United States is country `1`. Confirm the
country id from `/country/retrieve_all` — it's the one value here taken on
convention rather than observation.

Their US-only filtering deserves care: this project found that matching
country names loosely lets Australia, Austria, Belarus, Cyprus, Mauritius and
Russia through, because each contains "us". Pick the country by id.

### cyberyozh.json — read the polling note before using

Version 0.9.1: the buy path is pinned from their live catalogue, the poll is
still unverified.

**Use `wr` on `virtual`.** That's the pool that actually issues numbers.

| Config | Code | Provider | Status |
|---|---|---|---|
| Walmart | `wr` | virtual | **works** |
| Walmart | `se024415f170e25` | residential | listed, returns out-of-numbers |
| Walmart Family Mobile | `s9629aca71a8b02` | residential | untested |
| Walmart MoneyCard | `s5e85c951385be4` | residential | untested |

All United States (`667`), all `MIN_15`.

The residential entries are a trap worth understanding. `GET /numbers/services/`
lists them, but its own documentation says it is "deduplicated by service code
so a service is listed even when its only priced rows are per-country
sub-variants" — being listed is not the same as having US stock. Requesting one
gives out-of-numbers on every attempt, which in a task runner reads as a
five-minute timeout per try rather than an immediate refusal.

That's a shame, because residential is non-VoIP and would have been the better
answer to Walmart rejecting numbers. `wr` is virtual, same as everything else
here.

Residential codes are `s` plus a hash; virtual ones are short. The catalogue
runs to **12,693 entries**, so anything else you need is in there — filter it
by name rather than guessing a code.

**The poll is the guess.** There may be no detail route for a single order, so
`getMessage` reads `history_sms_code` off the **first** entry of the active
orders list. That is fine when you buy one number at a time and wrong when you
don't — with two orders in flight, the code from one could be handed to the
other. If a detail route like `/api/v1/numbers/{pk}/` exists, use it:

```json
"url": "https://app.cyberyozh.com/api/v1/numbers/${session.orderId}/",
"responseMapping": {"message": "$.history_sms_code"},
"pendingCheck": {"type": "FIELD_ABSENT", "path": "$.history_sms_code"}
```

There is also a risk the order **leaves** the active list once its code
arrives — that endpoint is documented as returning orders "currently awaiting
an SMS code", and completed ones move to `GET /history/`. If polling never
completes, that's why, and the detail route or `/history/` is the fix.

**No cancel yet.** The order object has `can_cancel`, so a route exists, but
it isn't in what I've seen of the docs. Without `cancelPhoneNumber` an unused
number isn't refunded — it just expires.

**Why bother:** `provider` accepts `residential`, which is non-VoIP. Walmart
rejects a large share of VoIP numbers, so residential stock is a materially
better bet than the cheap virtual pools, and none of the other providers here
offer it. Pair it with `period: MIN_15`, which is the only period valid for
one-time numbers — the `_rent` variants are rentals and not what you want for
a single code.

Errors are distinct and worth knowing: `402` insufficient balance, `400`
validation or no numbers available, `429` rate limited. Auth is the
`X-Api-Key` header; `"API key header is required."` means it wasn't sent at
all, `"Invalid API key provided."` means it was sent and rejected.

`need_fraud_score` requests a fraud check on the number, and `markup_percent`
pays more for presumably better stock. Both are left at their defaults.

### smsbower.json

The activate protocol pointed at `smsbower.online`, so it needs no host edit —
unlike `activate-protocol.json`, which ships aimed at DaisySMS.

Walmart is `wr`, country `187`. Confirmed against their live catalogue, which
reported **40,091 numbers in stock at $0.83** — the only provider here that
gives a real count rather than saying "available".

Service codes differ per site even though the protocol doesn't, so for any
other service, add the key to this repo and ask:

```
.venv/bin/python -m sams_automation sms-plan
```

Every pool it lists shows its code in brackets.

### grizzly-sms, simsms, sms-acktiv, sms-activation, smshub

The same protocol as SMSBower, each aimed at its own host, so none needs the
host edit the generic file does:

| File | Host |
|---|---|
| `grizzly-sms.json` | `api.grizzlysms.com` |
| `simsms.json` | `simsms.org` |
| `sms-acktiv.json` | `sms-acktiv.ru` |
| `sms-activation.json` | `sms-activation-service.com` |
| `smshub.json` | `smshub.org` |

**Grizzly is confirmed**: Walmart is `wr`, Sam's Club is `sams`, from its live
catalogue. The other four still ship with a blank service code. Country `187`
is the United States throughout.

`wr` is now Walmart on SMSBower, Grizzly and CyberYozh's virtual pool, so it
is a reasonable first guess elsewhere — but check rather than assume. Buying
the wrong service is how you pay for a number that can never receive the code
you're waiting for.

To find a site's own code, put its key in this repo and run:

```
.venv/bin/python -m sams_automation sms-plan
```

Each pool prints with its code in brackets. Don't assume `wr` carries over —
it happens to be Walmart on SMSBower and on CyberYozh's virtual pool, but
these sites number their catalogues independently.

Two of these hosts are `.ru` and may be slow or blocked depending on where you
run from. If a schema times out rather than erroring, that's usually why.

### activate-protocol.json
One schema for a dozen sites — they all clone `handler_api.php`. **Edit the
host in all four URLs** before loading; it ships pointing at DaisySMS.

Service codes differ per site even though the protocol doesn't, so
`service` is left blank in the config for you to fill. Country `187` is the
United States across the family.

Responses are plain text, so the mappings are removal regexes:
`ACCESS_NUMBER:12345:13055550123` → `phoneNumber` strips `ACCESS_NUMBER:12345:`,
`orderId` strips the prefix and the trailing number.

**sms-activate.org itself shut down on 2025-12-29.** Its infrastructure moved
to HeroSMS; balances did not transfer. Don't point this at the old host.

## Adding another provider

The shape is always the same: `getPhoneNumber` returns a number and something
to identify the order by, `getMessage` polls until the code lands, and a
`pendingCheck` tells Inbox which replies mean "not yet". Copy the closest file
and change the URLs.

`src/sams_automation/sms_providers.py` and `sms_purchase.py` in this repo hold
working implementations of all of these, including the response quirks each
one has. They're the reference if a schema misbehaves.
