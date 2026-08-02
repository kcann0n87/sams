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
| `activate-protocol.json` | DaisySMS, HeroSMS, tiger-sms, smsbower, grizzly-sms, sms-acktiv, simsms, smshub | Some |

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
