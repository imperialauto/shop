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
