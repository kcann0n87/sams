# Inbox | Custom SMS Schema

> AYCD developer documentation for Inbox | Custom SMS Schema, packaged as a single Markdown file you can hand to an AI agent
> (Claude, ChatGPT, Cursor, etc.) for code generation and integration help.

---

# Custom SMS | Introduction

The Custom SMS Schema lets you integrate any SMS provider's API with AYCD Inbox without waiting for native code support.
Instead of writing Java code, you describe the provider's request and response formats in a single JSON file, and Inbox
handles the rest — issuing requests, polling for messages, extracting verification codes, and managing rentals.

Schemas are portable. A developer or AI agent can author one once and share it as a file or hosted URL — any Inbox user
can then load it from inside the app.

## Loading a Schema

Inbox accepts a Custom SMS Schema in two forms. Both are configured from the credentials dialog after selecting
**Custom** as the provider:

- **Local JSON file** — click **Load Schema File...** and pick a `.json` file from disk. Best for private schemas or
  while you're authoring one.
- **Remote URL** — paste a URL that serves the schema as JSON. Best for sharing a schema across multiple machines or
  publishing one for others to use. The URL must return a `Content-Type` of `application/json` (or otherwise serve raw
  JSON) and be reachable from the user's machine.

Once loaded, the schema's `credentials` and `userDataFields` (see [Schema Reference](schema-reference.md)) are rendered
as form fields the user fills in.

## Quick Start

The smallest viable schema is a few credentials, one config the user can pick, and two endpoints — `getPhoneNumber` and
`getMessage`:

```json
{
  "name": "My SMS Provider",
  "description": "Integration with my custom SMS API",
  "version": "1.0.0",
  "author": "Your Name",
  "credentials": [
    {
      "name": "apiKey",
      "label": "API Key",
      "description": "Your API key from the provider dashboard",
      "required": true
    }
  ],
  "tempApi": {
    "userDataFields": [
      {
        "name": "country",
        "label": "Country",
        "required": true
      },
      {
        "name": "service",
        "label": "Service",
        "required": true
      }
    ],
    "userDataConfigs": [
      {
        "name": "US Discord",
        "values": {
          "country": "us",
          "service": "discord"
        }
      }
    ],
    "getPhoneNumber": {
      "method": "GET",
      "url": "https://api.example.com/number?apiKey=${credentials.apiKey}&country=${userData.country}&service=${userData.service}",
      "responseType": "JSON",
      "responseMapping": {
        "phoneNumber": "$.data.phone",
        "orderId": "$.data.id"
      }
    },
    "getMessage": {
      "method": "GET",
      "url": "https://api.example.com/sms?apiKey=${credentials.apiKey}&id=${session.orderId}",
      "responseType": "JSON",
      "responseMapping": {
        "message": "$.data.sms"
      },
      "pendingCheck": {
        "type": "FIELD_VALUE",
        "path": "$.data.status",
        "value": "waiting"
      },
      "pollingIntervalSeconds": 5
    }
  }
}
```

Save this as a `.json` file and load it via **Load Schema File...** to try it out. For more thorough samples covering
Bearer auth, plain-text APIs, multi-credential auth, and rental-only providers, see [Examples](examples.md).

## Top-Level Fields

| Field         | Type   | Required | Description                                                                                                |
|---------------|--------|----------|------------------------------------------------------------------------------------------------------------|
| `name`        | string | Yes      | Display name for the provider.                                                                             |
| `description` | string | No       | Short description of what this provider does.                                                              |
| `version`     | string | No       | Schema version (for your own tracking).                                                                    |
| `author`      | string | No       | Schema author name.                                                                                        |
| `priceFormat` | string | No       | Price format suffix (e.g., `"USD"`, `"EUR"`, `"Credits"`).                                                 |
| `credentials` | array  | No       | Authentication fields the user must fill in. See [Schema Reference](schema-reference.md#credentials).      |
| `generalApi`  | object | No       | General endpoints (e.g., balance checking). See [API Endpoints](api-endpoints.md#generalapi-endpoints).    |
| `tempApi`     | object | No*      | Endpoints for temporary/single-use phone numbers. See [API Endpoints](api-endpoints.md#tempapi-endpoints). |
| `rentApi`     | object | No*      | Endpoints for rental/long-term phone numbers. See [API Endpoints](api-endpoints.md#rentapi-endpoints).     |

<blockquote><p>* At least one of <code>tempApi</code> or <code>rentApi</code> must be defined.</p></blockquote>

## Validation Rules

The schema validator enforces these rules when a schema is loaded:

1. `name` is required and must not be blank.
2. At least one of `tempApi` or `rentApi` must be defined.
3. **tempApi** requires: `getPhoneNumber` (with `phoneNumber` mapping), `getMessage` (with `message` mapping and
   `pendingCheck`).
4. **rentApi** requires: `listPhoneNumbers` (with `phoneNumbers`, `phoneNumberId`, `phoneNumberValue` mappings),
   `getMessage` (with `message` mapping and `pendingCheck`).
5. Every endpoint must have a non-empty `url`.

If any of these checks fail, Inbox will reject the schema at load time with an error describing the failure.

## Where to Go Next

- **[Schema Reference](inbox/custom-sms/schema-reference.md)** — every field that lives outside an endpoint: credentials, user data, variables, transforms, and JSONPath syntax.
- **[Endpoint Structure](inbox/custom-sms/endpoints.md)** — the shape every endpoint follows: HTTP method, URL, headers, body, response parsing, polling, and rate limiting.
- **[API Endpoints](inbox/custom-sms/api-endpoints.md)** — the specific endpoints (`getPhoneNumber`, `getMessage`, `listPhoneNumbers`, etc.) that `generalApi`, `tempApi`, and `rentApi` expect.
- **[Examples](inbox/custom-sms/examples.md)** — five complete, working schema files covering different API styles.

---

# Custom SMS | Schema Reference

This page covers every field that lives **outside** an endpoint definition: how the user authenticates, what extra data
they fill in per request, and how those values get substituted into URLs, headers, and request bodies. For the shape of
an endpoint itself, see [Endpoint Structure](endpoints.md).

## Credentials

Define what authentication information the user needs to provide. Each credential becomes a field in the Inbox UI.

```json
"credentials": [
  {"name": "apiKey", "label": "API Key", "description": "Your provider API key", "required": true},
  {"name": "username", "label": "Username", "required": true},
  {"name": "password", "label": "Password", "required": true}
]
```

| Field         | Type    | Required | Description                                        |
|---------------|---------|----------|----------------------------------------------------|
| `name`        | string  | Yes      | Internal key name (used in variable substitution). |
| `label`       | string  | Yes      | Display label shown in the UI.                     |
| `description` | string  | No       | Help text shown below the field in the UI.         |
| `required`    | boolean | Yes      | Whether this field must be filled in.              |

Access these values inside any endpoint using `${credentials.<name>}`.

## User Data

User data fields and configs live **inside** each API section (`tempApi` or `rentApi`), so each API type can have its
own set of inputs.

### userDataFields

Define additional information the user provides per request:

```json
"userDataFields": [
  {"name": "country", "label": "Country", "description": "Two-letter country code (e.g. us, uk)", "required": true},
  {"name": "service", "label": "Service", "description": "Target service name (e.g. discord, instagram)", "required": true},
  {"name": "max_price", "label": "Max Price", "description": "Maximum price per number in USD", "required": false, "defaultValue": "1.00"}
]
```

| Field          | Type    | Required | Description                            |
|----------------|---------|----------|----------------------------------------|
| `name`         | string  | Yes      | Internal key name.                     |
| `label`        | string  | Yes      | Display label.                         |
| `description`  | string  | Yes      | Help text shown below the field in UI. |
| `required`     | boolean | Yes      | Whether the field is required.         |
| `defaultValue` | string  | No       | Default value if not filled in.        |

### userDataConfigs

Pre-defined configurations that auto-fill the user data fields. For temporary numbers, these also serve as the **target
selection** the user picks from a dropdown:

```json
"userDataConfigs": [
  {"name": "US Discord", "values": {"country": "us", "service": "discord"}},
  {"name": "UK Instagram", "values": {"country": "uk", "service": "instagram"}},
  {"name": "US Twitter", "values": {"country": "us", "service": "twitter", "max_price": "0.50"}}
]
```

When the user selects a config, its `values` are loaded into the user data fields and used for variable substitution.

## Variable Substitution

Use `${scope.key}` placeholders anywhere in URLs, headers, and request bodies. They are resolved at request time.

### Scopes

| Scope         | Description                                        | Example                 |
|---------------|----------------------------------------------------|-------------------------|
| `credentials` | Values from the credentials fields.                | `${credentials.apiKey}` |
| `userData`    | Values from user data fields/configs.              | `${userData.country}`   |
| `session`     | Values extracted from previous endpoint responses. | `${session.orderId}`    |

### URL Encoding

Variables in URLs are **automatically URL-encoded**. Variables in headers and bodies are **not** URL-encoded.

### Preconfigured Session Keys

These session keys are always available without needing to extract them:

| Key                                | Description                         | Example Value          |
|------------------------------------|-------------------------------------|------------------------|
| `${session.startedAtEpochSeconds}` | Session start time (epoch seconds). | `1712160000`           |
| `${session.startedAtEpochMillis}`  | Session start time (epoch millis).  | `1712160000000`        |
| `${session.startedAtIsoDateTime}`  | Session start time (ISO-8601).      | `2026-04-03T12:00:00Z` |

### How Session State Works

Session state is built up from the `responseMapping` of each endpoint call:

1. `getPhoneNumber` is called. Its `responseMapping` extracts `phoneNumber` and `orderId`.
2. These values are now available as `${session.phoneNumber}` and `${session.orderId}`.
3. `getMessage` uses `${session.orderId}` in its URL to poll for the SMS.

You choose what to extract and what to name it. There's no hardcoded requirement for `orderId` — if your API uses just
the phone number, map only `phoneNumber` and use `${session.phoneNumber}` in `getMessage`.

<blockquote><p><strong>Important:</strong> the <code>phoneNumber</code> value is internally formatted to include the dial code. For example, if your API returns <code>5551234567</code> for a US number, <code>${session.phoneNumber}</code> becomes <code>15551234567</code>. If you need the exact phone number string your API returned (e.g., to pass back in subsequent requests), use <code>${session.orderId}</code> or map it to a custom session key in <code>responseMapping</code>.</p></blockquote>

## Variable Transforms

You can apply encoding transforms to variable values using the syntax `${transform:scope.key}`. The transform is applied
**before** any URL encoding.

| Transform    | Syntax                             | Description                                          |
|--------------|------------------------------------|------------------------------------------------------|
| `base64`     | `${base64:credentials.apiKey}`     | Standard Base64 encoding (RFC 4648, no line breaks). |
| `base64Url`  | `${base64Url:credentials.apiKey}`  | URL-safe Base64 encoding (`+/` replaced with `-_`).  |
| `base64Mime` | `${base64Mime:credentials.apiKey}` | MIME Base64 encoding (line-wrapped per RFC 2045).    |

**Example — Basic auth with Base64-encoded API key:**

```json
"headers": {
  "Authorization": "Basic ${base64:credentials.apiKey}"
}
```

## JSONPath Reference

Response mappings (in [Endpoint Structure](endpoints.md#response-mapping)) and pending/error checks use
[Jayway JSONPath](https://github.com/json-path/JsonPath) syntax. Common patterns:

| Expression                        | Description                 |
|-----------------------------------|-----------------------------|
| `$.field`                         | Top-level field.            |
| `$.parent.child`                  | Nested field.               |
| `$.array[0].field`                | First element of array.     |
| `$.array[*].field`                | All elements' field values. |
| `$.data.items[?(@.active==true)]` | Filtered array.             |
| `$..field`                        | Deep scan for field name.   |

For `listPhoneNumbers`, use `$.path.to.array` for the items path, and `$.fieldName` (relative to each item) for per-item
fields like `phoneNumberId` and `phoneNumberValue`.

---

# Custom SMS | Endpoint Structure

Every endpoint in a Custom SMS Schema — whether it lives under `generalApi`, `tempApi`, or `rentApi` — follows the same
shape. This page documents that shape. For the **specific** endpoints each API section requires, see
[API Endpoints](api-endpoints.md).

## Common Fields

```json
{
  "method": "GET",
  "url": "https://api.example.com/endpoint?key=${credentials.apiKey}",
  "headers": {
    "Authorization": "Bearer ${credentials.apiKey}"
  },
  "body": null,
  "contentType": "application/json",
  "responseType": "JSON",
  "responseMapping": {
    "...": "..."
  },
  "pendingCheck": null,
  "errorCheck": null,
  "pollingIntervalSeconds": 10,
  "rateLimit": null
}
```

| Field                    | Type   | Default              | Description                                     |
|--------------------------|--------|----------------------|-------------------------------------------------|
| `method`                 | string | `"GET"`              | HTTP method: GET, POST, PUT, DELETE, PATCH.     |
| `url`                    | string | **required**         | Full URL with `${...}` placeholders.            |
| `headers`                | object | `{}`                 | Header name-value map with placeholders.        |
| `body`                   | JSON   | `null`               | Request body (JSON template with placeholders). |
| `contentType`            | string | `"application/json"` | Content-Type header for requests with a body.   |
| `responseType`           | string | `"JSON"`             | `"JSON"` or `"TEXT"`.                           |
| `responseMapping`        | object | `{}`                 | Map of logical name to JSONPath / regex.        |
| `pendingCheck`           | object | `null`               | Pending message detection (`getMessage` only).  |
| `errorCheck`             | object | `null`               | Error detection.                                |
| `pollingIntervalSeconds` | int    | `10`                 | Seconds between polls (`getMessage` only).      |
| `rateLimit`              | object | `null`               | Per-endpoint rate limiting.                     |

Variables (`${credentials.x}`, `${userData.x}`, `${session.x}`) and transforms (`${base64:...}`) are documented in the
[Schema Reference](schema-reference.md#variable-substitution).

## Response Mapping

`responseMapping` is the bridge between the provider's response and the session state Inbox uses to chain calls
together. The set of keys that are required (e.g. `phoneNumber`, `message`, `phoneNumbers`) depends on the endpoint —
see [API Endpoints](api-endpoints.md). You can also extract any custom keys you want into the session.

### JSON Responses

For `responseType: "JSON"`, mapping values are
[JSONPath](https://github.com/json-path/JsonPath) expressions:

```json
"responseMapping": {
  "phoneNumber": "$.data.phone",
  "orderId": "$.data.activation_id",
  "customField": "$.data.extra_info"
}
```

See the [JSONPath Reference](schema-reference.md#jsonpath-reference) for syntax.

### Text Responses

For `responseType: "TEXT"`, mapping values are **regex replacement patterns**. The pattern describes what to **remove**
from the response body — whatever remains after removal is the extracted value.

- Empty string `""` or omitted → entire body is used as-is (trimmed).
- Non-empty string → used as a Java regex with `replaceAll(pattern, "")`, then trimmed.

```json
"responseMapping": {
  "message": "^FULL_SMS:"
}
```

For response `FULL_SMS:Your verification code is 843291`, this strips the `FULL_SMS:` prefix and yields
`Your verification code is 843291`.

```json
"responseMapping": {
  "phoneNumber": "^ACCESS_NUMBER:\\d+:",
  "orderId": "^ACCESS_NUMBER:|:\\d+$"
}
```

For response `ACCESS_NUMBER:12345:79001234567`:

- `phoneNumber` strips `ACCESS_NUMBER:12345:` → `79001234567`
- `orderId` strips `ACCESS_NUMBER:` and `:79001234567` → `12345`

<blockquote><p><strong>Note:</strong> backslashes must be double-escaped in JSON (<code>\\d</code> for <code>\d</code>, <code>\\n</code> for newline).</p></blockquote>

## Pending Check

Used on `getMessage` endpoints. Determines whether the API response means "still waiting for SMS" or "here is the
message". A pending check is **required** for every `getMessage` endpoint, since polling depends on it.

```json
"pendingCheck": {
  "type": "FIELD_VALUE",
  "path": "$.status",
  "value": "pending"
}
```

| Type            | Description                           | Required Fields |
|-----------------|---------------------------------------|-----------------|
| `FIELD_VALUE`   | A JSON field equals a specific value. | `path`, `value` |
| `FIELD_ABSENT`  | A JSON field is null or missing.      | `path`          |
| `EMPTY_ARRAY`   | A JSON field is an empty array `[]`.  | `path`          |
| `STATUS_CODE`   | HTTP response code matches a value.   | `value`         |
| `BODY_CONTAINS` | Response body contains a substring.   | `value`         |

**Examples:**

```json
{
  "type": "FIELD_VALUE",
  "path": "$.data.status",
  "value": "waiting"
}
```

```json
{
  "type": "FIELD_ABSENT",
  "path": "$.data.message"
}
```

```json
{
  "type": "EMPTY_ARRAY",
  "path": "$.data.messages"
}
```

```json
{
  "type": "STATUS_CODE",
  "value": "404"
}
```

```json
{
  "type": "BODY_CONTAINS",
  "value": "NO_SMS"
}
```

## Error Check

Optional error detection on any endpoint. If the configured check evaluates to non-empty, the response is treated as a
provider-side error and surfaced to the user.

```json
"errorCheck": {
  "path": "$.error",
  "messageField": "$.error.message"
}
```

| Field          | Type   | Description                                               |
|----------------|--------|-----------------------------------------------------------|
| `path`         | string | JSONPath to check — if non-null/non-empty, it's an error. |
| `messageField` | string | JSONPath to extract the error message (optional).         |

## Rate Limiting

Optional per-endpoint rate limiting. Use this when the provider documents per-endpoint quotas you should respect.

```json
"rateLimit": {
  "maxConcurrency": 1,
  "delayMillis": 500
}
```

| Field            | Type | Default | Description                                              |
|------------------|------|---------|----------------------------------------------------------|
| `maxConcurrency` | int  | `1`     | Maximum simultaneous requests.                           |
| `delayMillis`    | long | `250`   | Delay (ms) after a request before the next one proceeds. |

---

# Custom SMS | API Endpoints

This page lists every endpoint name Inbox knows about, grouped by which API section it lives under (`generalApi`,
`tempApi`, or `rentApi`). Each endpoint follows the common shape documented in [Endpoint Structure](endpoints.md) — the
differences below are the **required response mappings** and any endpoint-specific behavior.

## generalApi Endpoints

General-purpose endpoints not tied to a specific number type. The entire `generalApi` object is optional.

### getBalance (OPTIONAL)

Fetches the account balance.

**Required response mapping:** `balance`

**Example:**

```json
"generalApi": {
  "getBalance": {
    "method": "GET",
    "url": "https://api.example.com/balance?apiKey=${credentials.apiKey}",
    "responseType": "JSON",
    "responseMapping": {
      "balance": "$.data.balance"
    }
  }
}
```

For `responseType: "TEXT"`, the entire response body is used as the balance string.

## tempApi Endpoints

For temporary/single-use phone numbers. At least `getPhoneNumber` and `getMessage` are required.

### getPhoneNumber (REQUIRED)

Requests a temporary phone number.

**Required response mapping:** `phoneNumber`
**Optional response mapping:** any custom keys (stored in session for later endpoints to reference).

```json
"getPhoneNumber": {
  "method": "POST",
  "url": "https://api.example.com/number/request",
  "headers": {
    "Authorization": "Bearer ${credentials.apiKey}"
  },
  "body": {
    "service": "${userData.service}",
    "country": "${userData.country}"
  },
  "contentType": "application/json",
  "responseType": "JSON",
  "responseMapping": {
    "phoneNumber": "$.data.phone_number",
    "orderId": "$.data.activation_id"
  }
}
```

<blockquote><p><strong>Note:</strong> the extracted <code>phoneNumber</code> is parsed and reformatted with the dial code prepended, so <code>${session.phoneNumber}</code> may not match the exact value from your API response. Use <code>orderId</code> to store the exact value returned by your API, or map a custom session key if you need the original phone number string for subsequent requests.</p></blockquote>

### getMessage (REQUIRED)

Polls for the received SMS. **Must include `pendingCheck`** — see [Pending Check](endpoints.md#pending-check).

**Required response mapping:** `message`

```json
"getMessage": {
  "method": "GET",
  "url": "https://api.example.com/sms/check?apiKey=${credentials.apiKey}&id=${session.orderId}",
  "responseType": "JSON",
  "responseMapping": {
    "message": "$.data.sms_code"
  },
  "pendingCheck": {
    "type": "FIELD_VALUE",
    "path": "$.data.status",
    "value": "waiting"
  },
  "pollingIntervalSeconds": 5
}
```

### cancelPhoneNumber (OPTIONAL)

Cancels/rejects a phone number. If not defined, cancellation is treated as unsupported.

```json
"cancelPhoneNumber": {
  "method": "POST",
  "url": "https://api.example.com/number/cancel",
  "headers": {
    "Authorization": "Bearer ${credentials.apiKey}"
  },
  "body": {
    "activation_id": "${session.orderId}"
  },
  "responseType": "JSON"
}
```

## rentApi Endpoints

For rental/long-term phone numbers. At least `listPhoneNumbers` and `getMessage` are required.

### listPhoneNumbers (REQUIRED)

Fetches available rental phone numbers.

**Required response mappings:** `phoneNumbers`, `phoneNumberId`, `phoneNumberValue`
**Optional:** `phoneNumberDescription`

```json
"listPhoneNumbers": {
  "method": "GET",
  "url": "https://api.example.com/rental/numbers?apiKey=${credentials.apiKey}",
  "responseType": "JSON",
  "responseMapping": {
    "phoneNumbers": "$.data.numbers",
    "phoneNumberId": "$.id",
    "phoneNumberValue": "$.phone_number",
    "phoneNumberDescription": "$.description"
  }
}
```

`phoneNumbers` is the JSONPath to the array of items. The remaining mappings are evaluated **relative to each array
item**.

<blockquote><p><strong>Note:</strong> the <code>phoneNumberValue</code> is internally formatted with the dial code prepended, similar to tempApi's <code>phoneNumber</code>. Use <code>phoneNumberId</code> when you need the exact original value for subsequent requests.</p></blockquote>

### activatePhoneNumber (OPTIONAL)

Activates a rental number before receiving SMS.

```json
"activatePhoneNumber": {
  "method": "POST",
  "url": "https://api.example.com/rental/activate",
  "headers": {
    "Authorization": "Bearer ${credentials.apiKey}"
  },
  "body": {
    "number_id": "${session.phoneNumber}"
  },
  "responseType": "JSON",
  "responseMapping": {
    "activationId": "$.data.activation_id"
  }
}
```

### getMessage (REQUIRED)

Same shape as [tempApi `getMessage`](#getmessage-required). Polls for SMS on the rental number. **Must include
`pendingCheck`.**

### deactivatePhoneNumber (OPTIONAL)

Deactivates a rental number after use.

```json
"deactivatePhoneNumber": {
  "method": "POST",
  "url": "https://api.example.com/rental/deactivate",
  "headers": {
    "Authorization": "Bearer ${credentials.apiKey}"
  },
  "body": {
    "number_id": "${session.phoneNumber}"
  },
  "responseType": "JSON"
}
```

For complete schemas wiring these endpoints together end-to-end, see [Examples](examples.md).

---

# Custom SMS | Examples

Five complete schemas covering the most common API styles. Each example is a fully valid schema you can save as a
`.json` file and load via **Load Schema File...** in Inbox (see [Loading a Schema](introduction.md#loading-a-schema)).

## Example 1: Simple GET-based API (temp only)

An API where all operations use GET requests with query parameters.

```json
{
  "name": "SimpleAPI",
  "description": "Simple GET-based SMS provider",
  "version": "1.0.0",
  "author": "Dev Team",
  "priceFormat": "USD",
  "credentials": [
    {
      "name": "apiKey",
      "label": "API Key",
      "description": "Your SimpleAPI key",
      "required": true
    }
  ],
  "tempApi": {
    "userDataFields": [
      {
        "name": "country",
        "label": "Country Code",
        "required": true
      },
      {
        "name": "service",
        "label": "Service Name",
        "required": true
      }
    ],
    "userDataConfigs": [
      {
        "name": "US Discord",
        "values": {
          "country": "1",
          "service": "discord"
        }
      },
      {
        "name": "US Instagram",
        "values": {
          "country": "1",
          "service": "instagram"
        }
      },
      {
        "name": "UK WhatsApp",
        "values": {
          "country": "44",
          "service": "whatsapp"
        }
      }
    ],
    "getPhoneNumber": {
      "method": "GET",
      "url": "https://api.simpleapi.com/v1/order?api_key=${credentials.apiKey}&country=${userData.country}&service=${userData.service}",
      "responseType": "JSON",
      "responseMapping": {
        "phoneNumber": "$.phone",
        "orderId": "$.order_id"
      },
      "errorCheck": {
        "path": "$.error",
        "messageField": "$.error_msg"
      }
    },
    "getMessage": {
      "method": "GET",
      "url": "https://api.simpleapi.com/v1/sms?api_key=${credentials.apiKey}&order_id=${session.orderId}",
      "responseType": "JSON",
      "responseMapping": {
        "message": "$.sms_text"
      },
      "pendingCheck": {
        "type": "FIELD_VALUE",
        "path": "$.status",
        "value": "waiting"
      },
      "pollingIntervalSeconds": 10,
      "errorCheck": {
        "path": "$.error",
        "messageField": "$.error_msg"
      }
    },
    "cancelPhoneNumber": {
      "method": "GET",
      "url": "https://api.simpleapi.com/v1/cancel?api_key=${credentials.apiKey}&order_id=${session.orderId}",
      "responseType": "JSON",
      "errorCheck": {
        "path": "$.error",
        "messageField": "$.error_msg"
      }
    }
  }
}
```

## Example 2: POST-based API with Bearer Auth (temp + rental)

An API that uses POST requests with JSON bodies and Bearer token auth, with both temp and rental support.

```json
{
  "name": "AdvancedSMS",
  "description": "REST API with Bearer auth and JSON bodies",
  "version": "2.0.0",
  "author": "Dev Team",
  "credentials": [
    {
      "name": "bearerToken",
      "label": "Bearer Token",
      "required": true
    }
  ],
  "tempApi": {
    "userDataFields": [
      {
        "name": "country",
        "label": "Country",
        "required": true
      },
      {
        "name": "app",
        "label": "Application",
        "required": true
      }
    ],
    "userDataConfigs": [
      {
        "name": "US Discord",
        "values": {
          "country": "US",
          "app": "discord"
        }
      },
      {
        "name": "US Telegram",
        "values": {
          "country": "US",
          "app": "telegram"
        }
      }
    ],
    "getPhoneNumber": {
      "method": "POST",
      "url": "https://api.advancedsms.com/v2/activations",
      "headers": {
        "Authorization": "Bearer ${credentials.bearerToken}"
      },
      "body": {
        "country": "${userData.country}",
        "application": "${userData.app}"
      },
      "contentType": "application/json",
      "responseType": "JSON",
      "responseMapping": {
        "phoneNumber": "$.activation.phone_number",
        "orderId": "$.activation.id"
      },
      "rateLimit": {
        "maxConcurrency": 2,
        "delayMillis": 1000
      }
    },
    "getMessage": {
      "method": "GET",
      "url": "https://api.advancedsms.com/v2/activations/${session.orderId}",
      "headers": {
        "Authorization": "Bearer ${credentials.bearerToken}"
      },
      "responseType": "JSON",
      "responseMapping": {
        "message": "$.activation.sms_message"
      },
      "pendingCheck": {
        "type": "FIELD_ABSENT",
        "path": "$.activation.sms_message"
      },
      "pollingIntervalSeconds": 5
    },
    "cancelPhoneNumber": {
      "method": "DELETE",
      "url": "https://api.advancedsms.com/v2/activations/${session.orderId}",
      "headers": {
        "Authorization": "Bearer ${credentials.bearerToken}"
      },
      "responseType": "JSON"
    }
  },
  "rentApi": {
    "userDataFields": [],
    "userDataConfigs": [],
    "listPhoneNumbers": {
      "method": "GET",
      "url": "https://api.advancedsms.com/v2/rentals",
      "headers": {
        "Authorization": "Bearer ${credentials.bearerToken}"
      },
      "responseType": "JSON",
      "responseMapping": {
        "phoneNumbers": "$.rentals",
        "phoneNumberId": "$.id",
        "phoneNumberValue": "$.phone_number",
        "phoneNumberDescription": "$.monthly_cost"
      }
    },
    "activatePhoneNumber": {
      "method": "POST",
      "url": "https://api.advancedsms.com/v2/rentals/${session.phoneNumber}/activate",
      "headers": {
        "Authorization": "Bearer ${credentials.bearerToken}"
      },
      "responseType": "JSON",
      "responseMapping": {
        "rentalSessionId": "$.session_id"
      }
    },
    "getMessage": {
      "method": "GET",
      "url": "https://api.advancedsms.com/v2/rentals/${session.phoneNumber}/sms?since=${session.startedAtEpochSeconds}",
      "headers": {
        "Authorization": "Bearer ${credentials.bearerToken}"
      },
      "responseType": "JSON",
      "responseMapping": {
        "message": "$.messages[0].text"
      },
      "pendingCheck": {
        "type": "EMPTY_ARRAY",
        "path": "$.messages"
      },
      "pollingIntervalSeconds": 10
    },
    "deactivatePhoneNumber": {
      "method": "POST",
      "url": "https://api.advancedsms.com/v2/rentals/${session.phoneNumber}/deactivate",
      "headers": {
        "Authorization": "Bearer ${credentials.bearerToken}"
      },
      "responseType": "JSON"
    }
  }
}
```

## Example 3: Plain-text API with regex extraction

An API that returns plain text instead of JSON. Response mapping values are regex patterns that describe what to
**remove** — whatever remains is the extracted value. An empty string means use the entire body as-is.

```json
{
  "name": "TextSMS",
  "description": "Provider with plain text API responses",
  "version": "1.0.0",
  "author": "Dev Team",
  "credentials": [
    {
      "name": "apiKey",
      "label": "API Key",
      "required": true
    }
  ],
  "generalApi": {
    "getBalance": {
      "method": "GET",
      "url": "https://api.textsms.com/balance.php?api_key=${credentials.apiKey}",
      "responseType": "TEXT",
      "responseMapping": {
        "balance": "[^\\d.]"
      }
    }
  },
  "tempApi": {
    "userDataFields": [
      {
        "name": "service",
        "label": "Service",
        "required": true
      }
    ],
    "userDataConfigs": [
      {
        "name": "Discord",
        "values": {
          "service": "discord"
        }
      },
      {
        "name": "Twitter",
        "values": {
          "service": "twitter"
        }
      }
    ],
    "getPhoneNumber": {
      "method": "GET",
      "url": "https://api.textsms.com/get_number.php?api_key=${credentials.apiKey}&service=${userData.service}",
      "responseType": "TEXT",
      "responseMapping": {
        "phoneNumber": "^ACCESS_NUMBER:\\d+:",
        "orderId": "^ACCESS_NUMBER:|:\\d+$"
      }
    },
    "getMessage": {
      "method": "GET",
      "url": "https://api.textsms.com/get_sms.php?api_key=${credentials.apiKey}&number=${session.phoneNumber}",
      "responseType": "TEXT",
      "responseMapping": {
        "message": "^FULL_SMS:"
      },
      "pendingCheck": {
        "type": "BODY_CONTAINS",
        "value": "WAITING"
      },
      "pollingIntervalSeconds": 8
    }
  }
}
```

In this example:

- `getBalance` response `Balance: $5.23 USD` → strips non-numeric chars → `5.23`
- `getPhoneNumber` response `ACCESS_NUMBER:12345:79001234567` → `phoneNumber` strips prefix → `79001234567`,
  `orderId` strips prefix and suffix → `12345`
- `getMessage` response `FULL_SMS:Your code is 843291` → strips `FULL_SMS:` prefix → `Your code is 843291`

## Example 4: API with multiple auth fields

```json
{
  "name": "MultiAuth Provider",
  "version": "1.0.0",
  "credentials": [
    {
      "name": "username",
      "label": "Username",
      "required": true
    },
    {
      "name": "apiKey",
      "label": "API Key",
      "description": "Your API key from the settings page",
      "required": true
    },
    {
      "name": "projectId",
      "label": "Project ID",
      "description": "Optional project ID for scoped access",
      "required": false
    }
  ],
  "tempApi": {
    "userDataFields": [
      {
        "name": "country",
        "label": "Country",
        "required": true
      },
      {
        "name": "service",
        "label": "Service",
        "required": true
      }
    ],
    "userDataConfigs": [
      {
        "name": "US Discord",
        "values": {
          "country": "us",
          "service": "discord"
        }
      }
    ],
    "getPhoneNumber": {
      "method": "POST",
      "url": "https://api.multiauth.com/order",
      "headers": {
        "X-Api-Key": "${credentials.apiKey}",
        "X-Username": "${credentials.username}",
        "X-Project": "${credentials.projectId}"
      },
      "body": {
        "country": "${userData.country}",
        "service": "${userData.service}"
      },
      "responseType": "JSON",
      "responseMapping": {
        "phoneNumber": "$.result.number",
        "orderId": "$.result.order_id"
      }
    },
    "getMessage": {
      "method": "GET",
      "url": "https://api.multiauth.com/sms/${session.orderId}",
      "headers": {
        "X-Api-Key": "${credentials.apiKey}",
        "X-Username": "${credentials.username}"
      },
      "responseType": "JSON",
      "responseMapping": {
        "message": "$.result.message"
      },
      "pendingCheck": {
        "type": "STATUS_CODE",
        "value": "404"
      },
      "pollingIntervalSeconds": 6
    }
  }
}
```

## Example 5: Rental-only provider

```json
{
  "name": "RentalOnly Provider",
  "version": "1.0.0",
  "credentials": [
    {
      "name": "apiKey",
      "label": "API Key",
      "required": true
    }
  ],
  "rentApi": {
    "userDataFields": [],
    "userDataConfigs": [],
    "listPhoneNumbers": {
      "method": "GET",
      "url": "https://api.rentalonly.com/numbers?key=${credentials.apiKey}",
      "responseType": "JSON",
      "responseMapping": {
        "phoneNumbers": "$.numbers",
        "phoneNumberId": "$.id",
        "phoneNumberValue": "$.phone",
        "phoneNumberDescription": "$.plan_name"
      }
    },
    "getMessage": {
      "method": "GET",
      "url": "https://api.rentalonly.com/sms?key=${credentials.apiKey}&number=${session.phoneNumber}&since=${session.startedAtEpochMillis}",
      "responseType": "JSON",
      "responseMapping": {
        "message": "$.messages[0].body"
      },
      "pendingCheck": {
        "type": "EMPTY_ARRAY",
        "path": "$.messages"
      },
      "pollingIntervalSeconds": 15
    }
  }
}
```

