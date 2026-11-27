# Email inbox in the dashboard: plan (Nov 27, 2026)

Phase 1 is built on the test branch (PR 254). Nothing is connected, migrated or deployed. Database
changes, connecting the mailbox and deploys each wait for Jae's yes. See "Phase 1 as built" below.

## Why
Jae is turning off email notifications on his phone. Today the mail that matters (website leads,
U-Haul roadside offers, bills and shutoff notices, PartsTech/AutoZone, Snap Finance) lands in
jaelan@imperialautocarellc.com and is read in Chrome. 299 U-Haul roadside offers went unanswered
since June, and past-due bills surfaced only from email (issues.md #11). The inbox should live in the
dashboard, sort itself, and alert only when something needs him.

## Pick: Gmail API, one internal Google app, business mailbox first
| Option | Cost | Setup | Fit |
|---|---|---|---|
| **Gmail API, Internal OAuth app in the imperialautocarellc.com Workspace** | Free | ~10 min in Google Cloud, one sign-in | Best. Internal apps skip Google's app verification and security review, even for full mail access. Read, send as the business address, labels, threads, attachments, history sync. |
| IMAP/SMTP with an app password | Free | 5 min | Fine fallback, weaker: no labels/threads model, polling only, app passwords need 2-step on and can be disabled by the Workspace admin. |
| Nylas / similar | Monthly per-mailbox fee | Easy | Not needed for one or two mailboxes; adds a vendor and a bill. |

- **Business mailbox first** (jaelan@imperialautocarellc.com). It gets the U-Haul, vendor and lead mail.
- **Personal Gmail later, optional.** An "Internal" app can't include a @gmail.com account. An external
  app left in Testing mode loses its sign-in every 7 days, and publishing it means Google's paid security
  review. If he wants personal mail in too, use IMAP with an app password, read-only.
- No forwarding rule is needed, so the Workspace forwarding block stays as it is.
- `google-api-python-client` and `google-auth` are already in requirements.txt (calendar uses them).

### Sync
- Phase 1: APScheduler job every 60 s calls `users.history.list` from the last stored historyId
  (cheap, well inside free quota). Full resync if the historyId is too old.
- Phase 3: Gmail `watch` + Pub/Sub push for instant delivery (Pub/Sub free tier covers this volume).
- Refresh token stored encrypted (Fernet, `cryptography` already a dependency) with the key in a Railway
  variable. Scopes: `gmail.modify` (read, label, archive) and `gmail.send`. No delete scope.

## Features
### Phase 1: a working inbox (replaces reading mail in Chrome)
1. **Inbox page** `/mail`: threads newest first, unread bold, full thread view, archive,
   mark read/unread, star, search (Gmail query passed through), attachments open/download.
2. **Auto-sort tabs**: Leads, U-Haul, Bills, Parts & vendors, Other. Sender/subject rules first
   (noreply@roadside.uhaul.com, U-Haul repair dispatch, Squarespace forms, utility/insurer/lender
   senders, PartsTech, AutoZone, FleetPride, Snap); AI fallback only for what rules miss. Tabs mirror
   to Gmail labels so Gmail and the dashboard agree.
3. **Urgent-only alerts in the app**: U-Haul roadside offer, new lead or web form, bill past due /
   cancellation / shutoff notice. They join the existing Needs-you queue (DispatchItem, like
   website_alerts.py) and the owner alert rules (quiet hours). Everything else is silent.
4. **Reply and compose as the business address** from the thread view, with one-tap starters in Jae's
   voice ("Got it, I can get you in…", "Here's your estimate…"). Nothing sends until he presses Send.
5. **Link to customers and jobs**: match the sender to a customer by email (then phone/name in the body);
   show the thread on the customer file and job page; save to `communications` with channel=email.

### Phase 2: mail that does work
6. **U-Haul offer → job**: the roadside offer becomes the alert and, on his accept tap, a fleet job with
   PO (see uhaul-automation-plan.md; this replaces the Apps Script webhook there).
7. **Bills → Money page**: due date, amount and payee pulled from the bill email onto the bill board,
   with the email linked. He still pays; the dashboard never pays.
8. **Attachments to jobs**: one tap saves a PDF/photo (receipt, PO, insurance doc) to the job;
   AutoZone/PartsTech order mails matched to the job's PO.
9. **Lead follow-up**: a lead email with no reply after 2 business hours shows in Needs you.

### Phase 3: polish
10. Instant push (Gmail watch + Pub/Sub). 11. Personal Gmail read-only via IMAP if wanted.
12. Bulk unsubscribe/cleanup suggestions for junk senders. 13. Multi-shop accounts for the SaaS version.

## Phase 1 as built (Nov 27, 2026)
- **Pages** (owner only): `/mail` inbox with tabs, Inbox/Starred/Sent/All, search (dashboard copy, or
  "Search all of Gmail"); `/mail/t/{id}` thread with reply, starters, signature, files (18 MB cap),
  earlier quoted text folded, original layout in a sandboxed frame with remote images held back;
  `/mail/compose`; `/mail/settings` to connect, check now and disconnect. "Email" appears in the owner
  menu only when `MAIL_INBOX_ENABLED` is on.
- **Sorting** is by sender/subject rules only (no AI fallback yet). Tabs live in the dashboard; they are
  not copied to Gmail labels. Read/unread, archive and star are changed in Gmail first, then mirrored.
  Moving a thread to another tab locks it there.
- **Alerts**: new unread mail in Leads (including any known customer), urgent U-Haul (roadside, dispatch,
  offer) and urgent bills (past due, cancel, shutoff) adds one Needs-you item per thread on Home, with an
  "Open the email" link. Replying, archiving or sending from Gmail closes it. Old mail copied in on the
  first connect (last 30 days) never alerts. These items never text a customer.
- **Customers**: the sender's email matches a customer and their open job; Jae can link, change or unlink,
  and optionally save the sender's address to a customer with none. Each message is also saved to
  `communications` (channel email) so it shows on the customer file.
- **Safety**: STOP ALL blocks email sends; a double tap sends once; a failed send keeps the draft; Gmail
  refusing a change shows a message and changes nothing; read-only mode changes nothing in Gmail.
- **Sync**: every 60 s when `MAIL_SYNC_ENABLED` is also on; full resync if Gmail's history point expired.
- **Tested**: 40 unit tests, the migration twice on PostgreSQL in CI, and a walk of every page, link and
  button as a phone and as a desktop on a pretend mailbox (no errors). Not yet seen on a real phone screen.

### Turning it on (each step needs Jae's yes)
1. Run `migrations/20261127_mail_inbox.sql` on the database (adds four new tables, changes nothing else).
2. In the Workspace's Google Cloud project: enable the Gmail API, create an **Internal** OAuth consent
   screen and a Web OAuth client with redirect URI `https://<dashboard domain>/mail/oauth/callback`.
3. Railway variables: `GOOGLE_MAIL_CLIENT_ID`, `GOOGLE_MAIL_CLIENT_SECRET`, `MAIL_TOKEN_KEY` (a new
   Fernet key), `MAIL_INBOX_ENABLED=true`, then after the first connect `MAIL_SYNC_ENABLED=true`.
   Optional: `MAIL_ADDRESS` (sign-in hint), `MAIL_SENDER_NAME`, `MAIL_OAUTH_REDIRECT_URI`.
4. Deploy, open Email > Settings > Connect, sign in as the business address and allow both boxes.

## Tables (phase 1, needs approval)
- `mail_accounts`: address, provider, encrypted refresh token, history_id, last_sync_at, status.
- `mail_threads`: account_id, gmail_thread_id, subject, last_from, last_at, unread, category,
  customer_id, repair_order_id.
- `mail_messages`: thread_id, gmail_message_id (unique), from/to/cc, date, snippet, body_text,
  body_html (shown only in a sandboxed frame), labels.
- `mail_attachments`: message_id, gmail_attachment_id, filename, mime, size (bytes fetched on demand).

## What Jae has to do
1. Fix the imperialautocarellc.com domain renewal in Squarespace (card failed Oct 24). If the domain
   lapses, business mail stops and none of this works.
2. ~~Say yes to building phase 1 on a test branch.~~ Done Nov 27.
3. Create the Internal OAuth app in his Workspace's Google Cloud project (can be done together
   over Chrome) and approve the one sign-in.
4. Approve the database migration and the deploy.

Keep phone notifications for U-Haul and lead mail on until phase 1's alerts are live and tested.
