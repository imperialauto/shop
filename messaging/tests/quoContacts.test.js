const { test } = require("node:test");
const assert = require("node:assert/strict");
const { createContactLookup } = require("../src/lib/quoContacts");

function page(items, nextPageToken) {
  return { data: items, nextPageToken };
}
const contact = (firstName, lastName, value) => ({ defaultFields: { firstName, lastName, phoneNumbers: [{ value }] } });

test("reads every page and matches any phone format", async () => {
  let calls = 0;
  const quo = {
    listContacts: async ({ pageToken }) => {
      calls += 1;
      return pageToken ? page([contact("Melo", "Pontiac G6", "+16025550101")]) : page([contact("Jo", "", "+16025550100")], "next");
    },
  };
  const lookup = createContactLookup({ quo });
  assert.equal(await lookup.nameFor("(602) 555-0101"), "Melo Pontiac G6");
  assert.equal(await lookup.nameFor("+16025550100"), "Jo");
  assert.equal(await lookup.nameFor("+16025559999"), null);
  assert.equal(calls, 2);
});

test("a Quo failure returns no name and does not throw", async () => {
  const lookup = createContactLookup({ quo: { listContacts: async () => { throw new Error("down"); } } });
  assert.equal(await lookup.nameFor("+16025550100"), null);
});

function fakeQuo(initial = []) {
  const made = [];
  const patched = [];
  return {
    made,
    patched,
    listContacts: async () => page(initial),
    createContact: async (c) => {
      made.push(c);
      const row = { id: "new", externalId: c.externalId, defaultFields: { firstName: c.firstName, lastName: c.lastName, phoneNumbers: [{ value: c.phone }] } };
      initial.push(row);
      return row;
    },
    updateContact: async (id, fields) => { patched.push({ id, fields }); return { id, defaultFields: fields }; },
  };
}

test("creates a contact for a new number with its vehicle", async () => {
  const quo = fakeQuo();
  const lookup = createContactLookup({ quo });
  assert.equal(await lookup.syncContact({ phone: "+16025550100", vehicleText: "2013 Ford Fusion", category: "towing" }), "created");
  assert.equal(quo.made[0].firstName, "2013 Ford Fusion");
  assert.equal(quo.made[0].lastName, "Towing");
  assert.equal(await lookup.nameFor("+16025550100"), "2013 Ford Fusion Towing");
});

test("a placeholder gets its vehicle later and is created only once", async () => {
  const quo = fakeQuo();
  const lookup = createContactLookup({ quo });
  await lookup.syncContact({ phone: "+16025550100", vehicleText: null, category: "repair" });
  assert.equal(quo.made[0].firstName, "New Lead");
  assert.equal(await lookup.syncContact({ phone: "+16025550100", vehicleText: "2011 Honda Civic", category: "repair" }), "updated");
  assert.equal(quo.made.length, 1);
  assert.equal(quo.patched[0].fields.firstName, "2011 Honda Civic");
  assert.equal(quo.patched[0].fields.phoneNumbers[0].value, "+16025550100");
});

test("a contact made by hand is never changed or duplicated", async () => {
  const quo = fakeQuo([contact("Melo", "Pontiac G6", "+16025550100")]);
  const lookup = createContactLookup({ quo });
  assert.equal(await lookup.syncContact({ phone: "+16025550100", vehicleText: "2015 Pontiac G6", category: "repair" }), "unchanged");
  assert.equal(quo.made.length + quo.patched.length, 0);
});

test("plain chatter and skipped numbers do not create contacts", async () => {
  const quo = fakeQuo();
  const lookup = createContactLookup({ quo, skipNumbers: ["+16025550199"] });
  assert.equal(await lookup.syncContact({ phone: "+16025550100", vehicleText: null, category: "general" }), "skipped");
  assert.equal(await lookup.syncContact({ phone: "+16025550199", vehicleText: "2011 Ford Focus", category: "repair" }), "skipped");
  assert.equal(quo.made.length, 0);
});
