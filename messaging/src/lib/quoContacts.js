// Phone lookup and upkeep for Quo contacts. The shop often labels contacts by
// job ("Honda Accord Oil Leak"), so labels are for the owner's alerts only and
// must never be used as a customer's name in a message.
//
// syncContact only creates a contact for an unknown number, or fills in a
// placeholder this code created. It never changes a contact made by hand.

const REFRESH_MS = 60 * 60 * 1000;
const MAX_PAGES = 40;
const PLACEHOLDER = "New Lead";
const OWN_PREFIX = "imperial-ops:";

function digits(phone) {
  return String(phone || "").replace(/\D/g, "").slice(-10);
}

function labelFor(contact) {
  const fields = contact.defaultFields || contact;
  const name = [fields.firstName, fields.lastName].filter(Boolean).join(" ").trim();
  return (name || fields.company || "").trim().slice(0, 120) || null;
}

function titleCase(word) {
  return word ? word[0].toUpperCase() + word.slice(1) : "";
}

function createContactLookup({ quo, now = Date.now, skipNumbers = [], pageDelayMs = 250 }) {
  let names = new Map();
  let contacts = new Map();
  let loadedAt = 0;
  let loading = null;
  const skip = new Set(skipNumbers.map(digits));
  const busy = new Set();

  async function load() {
    const nextNames = new Map();
    const nextContacts = new Map();
    let pageToken;
    for (let page = 0; page < MAX_PAGES; page += 1) {
      const { data = [], nextPageToken } = await quo.listContacts({ pageToken });
      for (const contact of data) {
        const label = labelFor(contact);
        for (const phone of (contact.defaultFields || contact).phoneNumbers || []) {
          const key = digits(phone.value || phone);
          if (!key) continue;
          nextContacts.set(key, contact);
          if (label) nextNames.set(key, label);
        }
      }
      if (!nextPageToken) break;
      pageToken = nextPageToken;
      await new Promise((resolve) => setTimeout(resolve, pageDelayMs)); // stay under Quo's rate limit
    }
    names = nextNames;
    contacts = nextContacts;
    loadedAt = now();
  }

  function refresh() {
    loading = loading || load().finally(() => { loading = null; });
    return loading;
  }

  async function nameFor(phone) {
    if (now() - loadedAt > REFRESH_MS) {
      await refresh().catch((err) => {
        loadedAt = now() - REFRESH_MS + 5 * 60 * 1000; // try again in five minutes
        console.error("Quo contact refresh failed:", err.message);
      });
    }
    return names.get(digits(phone)) || null;
  }

  function remember(key, contact) {
    contacts.set(key, contact);
    const label = labelFor(contact);
    if (label) names.set(key, label);
  }

  async function syncContact({ phone, vehicleText, category }) {
    const key = digits(phone);
    if (!key || skip.has(key) || busy.has(key)) return "skipped";
    const hasLead = Boolean(vehicleText) || (category && category !== "general" && category !== "info");
    if (!hasLead) return "skipped";
    busy.add(key);
    try {
      await refresh(); // a contact saved by hand minutes ago must not be duplicated
      const firstName = vehicleText || PLACEHOLDER;
      const lastName = vehicleText ? titleCase(category === "general" ? "" : category) : key.slice(-4);
      const existing = contacts.get(key);
      if (!existing) {
        const created = await quo.createContact({ firstName, lastName, phone, externalId: OWN_PREFIX + key });
        remember(key, created);
        return "created";
      }
      const fields = existing.defaultFields || {};
      const ours = String(existing.externalId || "").startsWith(OWN_PREFIX);
      if (ours && fields.firstName === PLACEHOLDER && vehicleText) {
        const updated = await quo.updateContact(existing.id, { ...fields, firstName, lastName });
        remember(key, updated);
        return "updated";
      }
      return "unchanged";
    } finally {
      busy.delete(key);
    }
  }

  return { nameFor, syncContact };
}

module.exports = { createContactLookup };
