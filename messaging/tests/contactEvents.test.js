const { test } = require("node:test");
const assert = require("node:assert/strict");
const { createContactEvents, ensureContactWebhook } = require("../src/lib/contactEvents");
const { noteWrite, recentlyWritten } = require("../src/lib/quoContacts");

// Made-up contacts and numbers only.
function fixture({ contact = {}, wrote = () => false, skip = new Set() } = {}) {
  const sent = [];
  const quo = {
    getContact: async (id) => (contact === null ? null : {
      id, externalId: null,
      defaultFields: { firstName: "Dana", lastName: "Sample", company: "", phoneNumbers: [{ value: "+16025550111" }] },
      ...contact,
    }),
  };
  const shopClient = { quoContactChanged: async (c) => { sent.push(c); return { updated: 1 }; } };
  return { events: createContactEvents({ quo, shopClient, skip, wroteRecently: wrote }), sent };
}

const event = (id = "EV1", type = "contact.updated") => ({ id, type, data: { resource: { id: "CT1", firstName: "Body Says", phoneNumbers: [{ value: "+16025550999" }] } } });

test("a renamed contact is read back from Quo and sent to the dashboard", async () => {
  const { events, sent } = fixture();
  assert.equal(await events.handle(event()), "renamed");
  assert.deepEqual(sent, [{ id: "CT1", firstName: "Dana", lastName: "Sample", company: "", emails: [], phones: ["+16025550111"] }]);
});

test("the request body is never trusted for the name or number", async () => {
  const { events, sent } = fixture();
  await events.handle(event());
  assert.notEqual(sent[0].firstName, "Body Says");
  assert.ok(!sent[0].phones.includes("+16025550999"));
});

test("the same event twice is handled once", async () => {
  const { events, sent } = fixture();
  await events.handle(event(), "hook-1");
  assert.equal(await events.handle(event(), "hook-1"), "duplicate");
  assert.equal(sent.length, 1);
});

test("deletes and other events are ignored", async () => {
  const { events, sent } = fixture();
  assert.equal(await events.handle(event("EV2", "contact.deleted")), "ignored");
  assert.equal(await events.handle({ type: "call.completed" }), "ignored");
  assert.equal(sent.length, 0);
});

test("loop guard: a number our own sync just wrote is dropped", async () => {
  const { events, sent } = fixture({ wrote: () => true });
  assert.equal(await events.handle(event()), "our_own_write");
  assert.equal(sent.length, 0);
});

test("loop guard: writes are remembered for a few minutes only", () => {
  noteWrite("+16025550123", 1000);
  assert.equal(recentlyWritten("(602) 555-0123", 1000 + 60 * 1000), true);
  assert.equal(recentlyWritten("6025550123", 1000 + 6 * 60 * 1000), false);
});

test("skipped numbers and contacts with no number go nowhere", async () => {
  const skipped = fixture({ skip: new Set(["6025550111"]) });
  assert.equal(await skipped.events.handle(event()), "no_phone");
  const missing = fixture({ contact: null });
  assert.equal(await missing.events.handle(event("EV3")), "not_found");
  assert.equal(skipped.sent.length + missing.sent.length, 0);
});

test("the contact webhook is made once", async () => {
  const made = [];
  const quo = { listWebhooks: async () => made.map((m) => ({ url: m.url })), createContactWebhook: async (h) => made.push(h) };
  assert.equal(await ensureContactWebhook({ quo, url: "https://example.test/webhooks/quo-contacts/t" }), "created");
  assert.equal(await ensureContactWebhook({ quo, url: "https://example.test/webhooks/quo-contacts/t" }), "exists");
});

test("hourly catch-up sends every contact with a number, minus skipped and our own writes", async () => {
  const pages = [
    { data: [{ id: "A", defaultFields: { firstName: "Dana", lastName: "Sample", emails: [{ value: "dana@example.test" }], phoneNumbers: [{ value: "+16025550111" }] } },
             { id: "B", defaultFields: { firstName: "No", lastName: "Phone", phoneNumbers: [] } }], nextPageToken: "p2" },
    { data: [{ id: "C", defaultFields: { firstName: "Skip", phoneNumbers: [{ value: "+16025550122" }] } },
             { id: "D", defaultFields: { firstName: "Ours", phoneNumbers: [{ value: "+16025550133" }] } }] },
  ];
  let sent;
  const quo = { listContacts: async ({ pageToken }) => pages[pageToken ? 1 : 0] };
  const shopClient = { quoContactsCatchUp: async (c) => { sent = c; return { contacts: c.length, renamed: 1 }; } };
  const events = createContactEvents({ quo, shopClient, skip: new Set(["6025550122"]), wroteRecently: (p) => p.endsWith("0133"), pageDelayMs: 0 });
  const result = await events.catchUp();
  assert.deepEqual(sent, [{ id: "A", firstName: "Dana", lastName: "Sample", company: "", emails: ["dana@example.test"], externalId: "", phones: ["+16025550111"] }]);
  assert.equal(result.renamed, 1);
});

test("dashboard names go onto our own placeholder contacts only when write-back is on", async () => {
  const ours = { id: "A", externalId: "imperial-ops:6025550111", defaultFields: { firstName: "New Lead", lastName: "0111", phoneNumbers: [{ value: "+16025550111" }] } };
  const handMade = { id: "B", defaultFields: { firstName: "Honda", lastName: "Accord", phoneNumbers: [{ value: "+16025550122" }] } };
  const quo = { listContacts: async () => ({ data: [ours, handMade] }), updates: [], async updateContact(id, f) { this.updates.push([id, f]); return {}; } };
  const offers = [{ id: "A", firstName: "Dana", lastName: "Sample" }, { id: "B", firstName: "Robin", lastName: "Typed" }];
  const shopClient = { quoContactsCatchUp: async (c) => ({ contacts: c.length, renamed: 0, names_for_quo: offers }) };
  const marked = [];
  const off = createContactEvents({ quo, shopClient, pageDelayMs: 0, wroteRecently: () => false, markWritten: (p) => marked.push(p) });
  assert.equal((await off.catchUp()).renamedInQuo, 0);
  assert.equal(quo.updates.length, 0);
  const on = createContactEvents({ quo, shopClient, pageDelayMs: 0, wroteRecently: () => false, writeBack: true, markWritten: (p) => marked.push(p) });
  assert.equal((await on.catchUp()).renamedInQuo, 1);
  assert.deepEqual(quo.updates, [["A", { firstName: "Dana", lastName: "Sample", phoneNumbers: [{ value: "+16025550111" }] }]]);
  assert.deepEqual(marked, ["+16025550111"]);
});
