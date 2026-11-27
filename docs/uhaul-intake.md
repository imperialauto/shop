# U-Haul requests into the dashboard

U-Haul sends roadside offers and repair POs only by email, and has no API. This small Google Apps Script runs
inside the shop's Google Workspace mailbox, reads new U-Haul mail every few minutes and hands each one to the
dashboard at `POST /webhooks/uhaul`. The dashboard turns it into a card on Home and on the U-Haul page.

Nothing here answers, accepts or declines anything at U-Haul, and nothing is sent to anyone. Accepting an offer
stays a tap on U-Haul's own page, and submitting a bill-out stays a click in the vendor portal.

## Switching it on (owner only, in this order)

1. Apply `migrations/20261127_uhaul_flow.sql` after a fresh backup.
2. In Railway, set `UHAUL_ENABLED=true` and `UHAUL_INTAKE_KEY` to a new random value of 32+ characters.
   Until both are set the page says it isn't switched on, and `/webhooks/uhaul` answers 404 or 503.
3. Signed in as the business mailbox, open script.google.com, make a new project and paste the script below.
4. Project Settings, Script properties: add `DASHBOARD_URL` (the dashboard's address, no trailing slash) and
   `INTAKE_KEY` (the same value as `UHAUL_INTAKE_KEY`). The key never goes in the code.
5. Run `checkUhaulMail` once and allow it to read and label Gmail. Then Triggers, Add trigger:
   `checkUhaulMail`, time-driven, every 5 minutes.

To stop it, delete the trigger. To turn it off in the dashboard, set `UHAUL_ENABLED=false`.

## The script

```javascript
// Hands new U-Haul mail to the Imperial dashboard. Reads mail and adds a label; sends nothing.
const LABEL = 'Dashboard/U-Haul';
const QUERY = 'from:uhaul.com newer_than:3d -label:' + LABEL.replace('/', '-');

function checkUhaulMail() {
  const props = PropertiesService.getScriptProperties();
  const url = props.getProperty('DASHBOARD_URL');
  const key = props.getProperty('INTAKE_KEY');
  if (!url || !key) throw new Error('Set DASHBOARD_URL and INTAKE_KEY in Script properties.');
  const label = GmailApp.getUserLabelByName(LABEL) || GmailApp.createLabel(LABEL);
  GmailApp.search(QUERY, 0, 30).forEach(function (thread) {
    let allOk = true;
    thread.getMessages().forEach(function (msg) {
      const body = msg.getPlainBody().slice(0, 60000);
      const res = UrlFetchApp.fetch(url + '/webhooks/uhaul', {
        method: 'post',
        contentType: 'application/json',
        headers: { 'X-Uhaul-Intake-Key': key },
        muteHttpExceptions: true,
        payload: JSON.stringify({
          message_id: msg.getId(),
          thread_id: thread.getId(),
          from: msg.getFrom(),
          subject: msg.getSubject(),
          date: msg.getDate().toISOString(),
          body: body,
          links: (msg.getBody().match(/https:\/\/[^"'<> ]+/g) || []).slice(0, 20),
        }),
      });
      if (res.getResponseCode() !== 200) allOk = false;
    });
    if (allOk) thread.addLabel(label);  // labelled threads aren't sent again; a failure retries next run
  });
}
```

Sending the same message twice is harmless: the dashboard keeps one card per message, and folds U-Haul's
re-sends (same offer ~2 hours later, or a "Needs Review" mail for a PO it already has) into the existing card.

## What the dashboard accepts

- Only mail from `uhaul.com` or its subdomains. Anything else comes back `ignored`.
- An offer link is shown only when it is `https` on `uhaul.com` or `uhaul.net`. Any other link is dropped.
- Bodies over 200 KB are refused.
