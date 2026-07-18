# Messaging module (Quo texting + AI draft replies)

Lets a coordinator at Imperial Auto Care see and reply to customer texts (via
Quo, formerly OpenPhone) from a small web dashboard, with Claude drafting a
suggested reply to every inbound text — reviewed and sent by a human, never
sent automatically.

## Architecture note — read this first

This module was originally scoped to "fold into" the existing Cowork
dashboard app as a few files inside its `src/`. Once pointed at the real
target repo (`imperialauto/shop`), that assumption turned out to be wrong:
**imperialauto/shop is a Python/FastAPI + SQLAlchemy + Jinja2 app.** You
can't `require()` a Node/Express module into a Python process, and there's
no React build step to compile a `.jsx` component into anything a browser
can run.

Rather than force a bad fit, this module now runs as **its own small
standalone Node service** — a second Railway service in the same project,
built from the `messaging/` folder of this repo, with its own Postgres
database. It doesn't touch the shop app's code at all. It's reachable at
its own Railway URL, gated behind a simple username/password (see below),
with a link you can add to the shop app's nav if you want it one click away.

```
messaging/
├── .env.example
├── .gitignore
├── package.json
├── railway.toml
├── server.js                          Entry point (npm start)
├── prisma/
│   ├── schema.prisma                  This service's own DB schema
│   └── messaging-schema-additions.prisma   SUPERSEDED, ignore — see schema.prisma
├── public/
│   └── messages.html                  The actual dashboard UI (plain HTML/JS)
└── src/
    ├── app.js                         Express app: webhook + API + static dashboard
    ├── db/prisma.js                   Prisma client singleton
    ├── lib/
    │   ├── basicAuth.js                HTTP Basic Auth gate (real, not a placeholder)
    │   ├── quoClient.js                Quo REST API wrapper (send, list, mark-read/done/open)
    │   ├── verifyQuoWebhookSignature.js  Dual-scheme webhook signature verifier
    │   ├── getJobContext.js            Job-context lookup — currently a documented no-op, see file
    │   └── generateDraftReply.js       Calls Claude to draft a reply, never sends
    ├── routes/
    │   ├── quoWebhook.js               Receives Quo events, stores messages, drafts a reply
    │   └── messagesApi.js              API behind the dashboard (conversations, draft, send, status)
    ├── scripts/
    │   └── backfillQuoHistory.js       One-time (safe to re-run) import of existing Quo history
    └── components/
        └── MessagingPanel.jsx          React port of the dashboard — unused today, kept for if/when
                                         the shop app ever grows a React front end. public/messages.html
                                         is the one that's actually live.
```

## What this actually does

1. Quo calls `POST /webhooks/quo` on every text event. The signature is
   verified (both Quo's legacy `openphone-signature` scheme and the newer
   Standard-Webhooks-compatible `webhook-id`/`webhook-timestamp`/
   `webhook-signature` scheme — auto-detected, or force one via
   `QUO_WEBHOOK_SCHEME`), the message is stored, and on a genuinely new
   inbound text, Claude drafts a reply and stores it as a `PENDING`
   `DraftReply`. **Nothing is ever sent automatically.**
2. The dashboard (`public/messages.html`, served at the service's root
   `/`) lists conversations, shows the thread, and shows the pending draft
   in an editable box. A human edits or accepts it, then clicks **Send** —
   that's the only action in the whole codebase that calls Quo's send-a-text
   endpoint.
3. `npm run backfill` does a one-time (idempotent) import of existing Quo
   conversation/message history so the dashboard doesn't start empty.

## Two real bugs fixed in this pass

1. **Raw-body signature bug.** An earlier version re-serialized the
   already-parsed `req.body` with `JSON.stringify()` to check the
   signature instead of using Quo's exact raw bytes — if Express's
   re-serialization ever produced different bytes (key order, whitespace),
   that silently rejected valid webhooks. Fixed by mounting the route with
   `express.raw()` and verifying against the literal raw body.
2. **Timestamp unit bug.** The clock-skew check compared Quo's legacy
   timestamp as if it were in seconds; Quo's own documented example
   (`hmac;1;1639710054089;...`) is milliseconds. Fixed, and verified with a
   signature self-test (7/7 passing: valid + tampered-body + stale-timestamp
   for the legacy scheme, valid + key-rotation + invalid-signature for the
   standard scheme).

## Also fixed / added in this pass

- **`server.js` required `./app`, but `app.js` lives in `src/app.js`** — that
  path never resolved. Fixed to `./src/app`.
- **`requireAuth` was a placeholder that 501'd every request in
  production** — replaced with `src/lib/basicAuth.js`, real HTTP Basic Auth
  gating both the dashboard and its API. Set `DASHBOARD_USERNAME` /
  `DASHBOARD_PASSWORD` in env; there's no default, and the app refuses to
  boot in production without them.
- **Async route handlers weren't error-safe** — Express 4 doesn't catch
  rejected promises from `async` handlers on its own; an unhandled
  rejection would hang the request. All routes in `messagesApi.js` are now
  wrapped so failures return a clean JSON 500 instead.
- **`POST /conversations/:id/status`** (mark a thread OPEN/DONE) — the
  dashboard needs this to close out a thread, and it didn't exist before.
  It also calls Quo's own `mark-as-done`/`mark-as-open` so the shop's view
  inside the actual Quo app doesn't drift from this dashboard.
- **SENT vs EDITED on send** — previously always recorded `EDITED` even
  when a draft was approved verbatim. Now compares the sent body to the
  original draft body and records `SENT` or `EDITED` correctly.
- **A failed draft regeneration no longer destroys the existing draft** —
  it used to discard the current pending draft before checking whether the
  new one actually generated; if Claude's call failed, you'd lose the
  working draft and get nothing back. Now it discards the old one only
  after confirming the new one generated successfully.
- Filled in every file the original upload was missing: `prisma/schema.prisma`,
  `src/db/prisma.js`, `src/lib/quoClient.js`, `src/lib/getJobContext.js`,
  `src/lib/generateDraftReply.js`, `src/scripts/backfillQuoHistory.js`,
  `public/messages.html`.

## Deliberately deferred (needs your input, not a code fix)

- **Customer names.** This service's own DB has a lightweight `Customer`
  table (phone → name) — it is **not** the shop app's real customer table
  (that lives in the FastAPI app's Postgres/SQLAlchemy world, a separate
  system). Conversations will show phone numbers until names are populated,
  either by hand or by wiring a small sync — see the comment at the top of
  `src/lib/getJobContext.js` for the two realistic options.
- **Job context in AI drafts** (referencing an open repair order/estimate)
  is currently a graceful no-op for the same reason — no shared schema to
  read from yet.
- **Which Quo webhook scheme you're actually on.** Auto-detection is a
  convenience, not a substitute for checking your webhook subscription's
  settings in the Quo dashboard.

## Environment variables

See `.env.example`. You'll need: `QUO_API_KEY`, `QUO_DEFAULT_FROM_NUMBER`,
`QUO_WEBHOOK_SIGNING_SECRET` (and/or `_STANDARD`), `ANTHROPIC_API_KEY`,
`DATABASE_URL` (Railway's Postgres plugin sets this for you if attached to
this service), and `DASHBOARD_USERNAME`/`DASHBOARD_PASSWORD` (pick your own —
there's no default).

## Local development

```
cd messaging/
cp .env.example .env   # fill in real values
npm install
npm run migrate         # first time only: creates the migration history
npm start                # runs `prisma db push` then boots the server
npm run backfill         # optional: import existing Quo history
```

## Deploying

This folder is meant to be its own Railway service (separate from the
`imperialauto/shop` FastAPI service, same project): create a new service
from this GitHub repo, set its **Root Directory** to `messaging/`, attach a
Postgres plugin, and set the environment variables above. Railway/Nixpacks
will run `npm install` (which also runs `prisma generate` via `postinstall`)
and then `npm start` (which runs `prisma db push` before booting — safe on a
fresh database; it fails closed rather than silently dropping data if a
future schema change is destructive).

Point Quo's webhook subscription at `https://<this-service>.up.railway.app/webhooks/quo`.
