# Email inbox in the dashboard: plan (Nov 27, 2026)

Design only. Nothing is built, connected or deployed. Database changes, connecting an account and
deploys each wait for Jae's yes.

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
1. **Inbox page** `/dashboard/mail`: threads newest first, unread bold, full thread view, archive,
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

## Tables (phase 1, needs approval)
- `mail_accounts`: address, provider, encrypted refresh token, history_id, last_sync_at, status.
- `mail_threads`: account_id, gmail_thread_id, subject, last_from, last_at, unread, category,
  customer_id, repair_order_id.
- `mail_messages`: thread_id, gmail_message_id (unique), from/to/cc, date, snippet, body_text,
  body_html (sanitized on display), has_attachments.
- `mail_attachments`: message_id, gmail_attachment_id, filename, mime, size (bytes fetched on demand).

## What Jae has to do
1. Fix the imperialautocarellc.com domain renewal in Squarespace (card failed Oct 24). If the domain
   lapses, business mail stops and none of this works.
2. Say yes to building phase 1 on a test branch.
3. Later: create the Internal OAuth app in his Workspace's Google Cloud project (can be done together
   over Chrome) and approve the one sign-in.
4. Approve the database migration and the deploy.

Keep phone notifications for U-Haul and lead mail on until phase 1's alerts are live and tested.
