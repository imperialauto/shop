// Quo contact events: a contact saved or renamed in Quo renames the matching
// dashboard customer. The event is only a hint: the contact is read back from
// Quo, so nothing in the request body is trusted. Deletes are ignored (a
// customer is never removed). Never texts anyone and never writes to Quo.
//
// Loop guard: our own contact sync (quoContacts.syncContact) writes Quo
// contacts too, and every write fires contact.updated. Events for a number we
// wrote in the last few minutes are dropped, and the dashboard also refuses
// any label that reads like a job ("Honda Accord Oil Leak"), which is what
// our sync writes. Nothing here writes back to Quo, so nothing can loop.

const { recentlyWritten } = require("./quoContacts");

const SEEN_LIMIT = 500;

function phonesOf(contact) {
  const fields = contact?.defaultFields || contact || {};
  return (fields.phoneNumbers || contact?.phoneNumbers || [])
    .map((p) => String(p?.value || p || ""))
    .filter(Boolean);
}

function namesOf(contact) {
  const fields = contact?.defaultFields || contact || {};
  return {
    firstName: String(fields.firstName || "").slice(0, 120),
    lastName: String(fields.lastName || "").slice(0, 120),
    company: String(fields.company || "").slice(0, 120),
    emails: (fields.emails || contact?.emails || []).map((e) => String(e?.value || e || "").slice(0, 120)).filter(Boolean).slice(0, 10),
  };
}

const MAX_PAGES = 40;

function createContactEvents({ quo, shopClient, skip = new Set(), wroteRecently = recentlyWritten, pageDelayMs = 250 }) {
  const seen = new Set();

  async function handle(body, eventId) {
    const type = body?.type;
    const contactId = body?.data?.resource?.id || body?.data?.object?.id;
    if (type !== "contact.updated" || !contactId) return "ignored";
    const key = eventId || body?.id || `${type}:${contactId}:${body?.createdAt || ""}`;
    if (seen.has(key)) return "duplicate";
    seen.add(key);
    if (seen.size > SEEN_LIMIT) seen.delete(seen.values().next().value);

    const contact = await quo.getContact(contactId);
    if (!contact) return "not_found";
    const phones = phonesOf(contact).filter((p) => !skip.has(p.replace(/\D/g, "").slice(-10)));
    if (!phones.length) return "no_phone";
    if (phones.some((p) => wroteRecently(p))) return "our_own_write";
    const result = await shopClient.quoContactChanged({ id: String(contact.id || contactId), ...namesOf(contact), phones });
    return result?.updated ? "renamed" : result?.reason || "unchanged";
  }

  // Every contact once an hour: catches a missed webhook, and contacts renamed
  // before this existed. Reports to the Home health line through the dashboard.
  async function catchUp() {
    const contacts = [];
    let pageToken;
    for (let page = 0; page < MAX_PAGES; page += 1) {
      const { data = [], nextPageToken } = await quo.listContacts({ pageToken });
      for (const contact of data) {
        const phones = phonesOf(contact).filter((p) => !skip.has(p.replace(/\D/g, "").slice(-10)));
        if (!phones.length || phones.some((p) => wroteRecently(p))) continue;
        contacts.push({ id: String(contact.id || ""), ...namesOf(contact), phones: phones.slice(0, 10) });
      }
      if (!nextPageToken) break;
      pageToken = nextPageToken;
      await new Promise((resolve) => setTimeout(resolve, pageDelayMs)); // stay under Quo's rate limit
    }
    return shopClient.quoContactsCatchUp(contacts);
  }

  return { handle, catchUp };
}

async function ensureContactWebhook({ quo, url }) {
  const existing = await quo.listWebhooks();
  if (existing.some((hook) => hook.url === url)) return "exists";
  await quo.createContactWebhook({ url });
  return "created";
}

module.exports = { createContactEvents, ensureContactWebhook, phonesOf, namesOf };
