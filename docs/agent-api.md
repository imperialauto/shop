# Assistant access keys (Claude)

Lets the owner's AI assistant add customers, vehicles, draft estimate lines and
tentative appointments when the owner asks, without using the owner's password
or two-step code.

## What a key can and cannot do

| Scope | Allows |
|---|---|
| `customers:read` | Search customers, read a customer with vehicles and recent jobs |
| `customers:write` | Add a customer (an existing phone returns that customer), add a vehicle |
| `estimates:read` | Read a job and its lines |
| `estimates:write` | Start a job with draft lines; add lines to a job that is not invoiced or finished |
| `appointments:read` | List appointments |
| `appointments:write` | Book a tentative appointment (same hours, buffer and double-booking rules as the calendar) |

Never: invoices, payments, finance, logins, settings, messaging, approval
requests, or any staff page. A key never creates a session. Read-only
(recovery) mode blocks writes exactly as it does for staff.

## Safety

* Shown once on Settings; only a SHA-256 hash and a short prefix are stored.
* Expires (30, 90, 180 or 365 days), at most 3 active, revocable any time.
* Each write is logged in `agent_actions` (append-only on PostgreSQL) in the same
  transaction as the change; Settings shows the recent list.
* Repeated bad keys from one address are throttled.

## Turning it on (owner approval needed for each step)

1. Apply `migrations/20261119_agent_keys.sql` to the production database.
2. Set `AGENT_API_ENABLED=true` on the web service and redeploy.
3. Settings → Assistant Access → Create Key. Copy the key once and store it only
   in the assistant's secret settings, never in a text or chat.

Turn off: revoke the key, or set `AGENT_API_ENABLED=false` (every key stops).

## Calling it

```
curl -H "Authorization: Bearer $IMPERIAL_AGENT_KEY" https://<dashboard>/internal/agent/v1/whoami
```

Endpoints (all under `/internal/agent/v1`): `GET /whoami`, `GET /customers?q=`,
`GET /customers/{id}`, `POST /customers`, `POST /customers/{id}/vehicles`,
`GET /jobs/{id}`, `POST /estimates`, `POST /jobs/{id}/lines`,
`GET /appointments`, `POST /appointments`.

## Claude connector (sign in once, no key in chat)

The same keys can be handed to Claude through a standard sign-in instead of
being copied anywhere. See `app/agent_connector.py`.

1. In Claude, add a custom connector with the URL
   `https://<dashboard host>/internal/agent/mcp`.
2. Tap Connect. Claude opens `/connect/authorize` on the dashboard. Sign in as the
   owner (password and two-step code as usual), tick what Claude may do and tap Allow.
3. The dashboard gives Claude's servers a key named "Claude connector". It shows in
   Settings → Assistant Access with its last use and every change it made, and
   Revoke there cuts Claude off at once. Connecting again replaces the old key.

Safety rules in the code:

* Only Claude's own callback addresses (`https://claude.ai/api/mcp/auth_callback`,
  `https://claude.com/api/mcp/auth_callback`) can register, so a code is never sent elsewhere.
* Only the owner can approve. One-time codes last five minutes, work once and need PKCE (S256).
* No new tables: registrations and codes are signed with `SECRET_KEY`. Changing
  `SECRET_KEY` cancels any connect that is half done; existing keys keep working.
* Tools are the assistant API's own actions, limited to the key's scopes. Nothing
  invoices, takes payments, changes settings or contacts customers.
* Everything is off unless `AGENT_API_ENABLED=true`. Recovery read-only mode blocks it.
