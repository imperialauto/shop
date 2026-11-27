# Assistant access keys (Claude)

Lets the owner's AI assistant add customers, vehicles, draft estimate lines and
tentative appointments when the owner asks, without using the owner's password
or two-step code.

## What a key can and cannot do

| Scope | Allows |
|---|---|
| `customers:read` | Search customers, read a customer with vehicles and recent jobs |
| `customers:write` | Add a customer (an existing phone returns that customer), add a vehicle |
| `estimates:read` | Read a job, its lines and totals; the estimate as a printable PDF |
| `estimates:write` | Start a job with draft lines; add, edit or remove draft lines on a job that is not invoiced or finished; move a job on the board |
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
`POST /jobs/{id}/status`, `PATCH /jobs/{id}/lines/{line_id}`, `DELETE /jobs/{id}/lines/{line_id}`,
`GET /jobs/{id}/estimate.pdf`, `GET /appointments`, `POST /appointments`.

`POST /jobs/{id}/status` with `{"status": "in_progress", "note": "parts in"}` moves a job on the
board (scope `estimates:write`). It runs the job page's own checks (`app/ro_status_policy.py`),
closes the job's website request item like the staff button, logs `job.status` with
`old -> new; note`, and returns the job with its next step. It can mark a job Complete (ready
for pickup; `ready_for_pickup` is accepted too) but does not fire `app.job_events`, so no pickup
text goes out and the reply says the customer hasn't been told. It never moves a job to Picked up
(delivered), or moves one that is already there, because that can send the review request.

`PATCH /jobs/{id}/lines/{line_id}` (only the fields sent change) and `DELETE` correct or remove a
draft line with the job page editor's rules: only lines the customer hasn't been asked about
(`approval` is null), no invoice, job not finished, and not a line marked done for removal. Each is
logged (`estimate.edit_line`, `estimate.remove_line`) with the old values, and the reply carries
the recalculated `totals` (every job reply does, from `app/estimate_pdf.py`).

`GET /jobs/{id}/estimate.pdf` returns the estimate as a PDF (recommended repair, `OPTIONAL` lines as
add-ons, declined lines listed but not counted, parts-only tax at the invoice rate, 50% deposit,
terms). Staff get the same file from the job page's Print estimate button (`/ro/{id}/estimate.pdf`,
office login). The connector's `get_estimate_pdf` returns it as an embedded PDF plus that link.

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
