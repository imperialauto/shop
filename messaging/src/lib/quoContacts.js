// Phone-to-label lookup from Quo contacts. The shop often labels contacts by
// job ("Honda Accord Oil Leak"), so these are for the owner's alerts only and
// must never be used as a customer's name in a message.

const REFRESH_MS = 60 * 60 * 1000;
const MAX_PAGES = 40;

function digits(phone) {
  return String(phone || "").replace(/\D/g, "").slice(-10);
}

function labelFor(contact) {
  const fields = contact.defaultFields || contact;
  const name = [fields.firstName, fields.lastName].filter(Boolean).join(" ").trim();
  return (name || fields.company || "").trim().slice(0, 120) || null;
}

function createContactLookup({ quo, now = Date.now }) {
  let names = new Map();
  let loadedAt = 0;
  let loading = null;

  async function load() {
    const next = new Map();
    let pageToken;
    for (let page = 0; page < MAX_PAGES; page += 1) {
      const { data = [], nextPageToken } = await quo.listContacts({ pageToken });
      for (const contact of data) {
        const label = labelFor(contact);
        const phones = (contact.defaultFields || contact).phoneNumbers || [];
        for (const phone of phones) {
          const key = digits(phone.value || phone);
          if (label && key) next.set(key, label);
        }
      }
      if (!nextPageToken) break;
      pageToken = nextPageToken;
    }
    names = next;
    loadedAt = now();
  }

  async function nameFor(phone) {
    if (now() - loadedAt > REFRESH_MS) {
      loading = loading || load().catch((err) => {
        loadedAt = now() - REFRESH_MS + 5 * 60 * 1000; // try again in five minutes
        console.error("Quo contact refresh failed:", err.message);
      }).finally(() => { loading = null; });
      await loading;
    }
    return names.get(digits(phone)) || null;
  }

  return { nameFor };
}

module.exports = { createContactLookup };
