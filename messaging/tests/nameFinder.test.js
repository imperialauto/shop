const test = require('node:test');
const assert = require('node:assert/strict');
const { findName, verify } = require('../src/lib/nameFinder');

function quoWith({ messages = [], calls = [], summaries = {}, transcripts = {} }) {
  let writes = 0;
  return {
    writes: () => writes,
    listMessages: async ({ participants }) => { assert.deepEqual(participants, ['+16235550177']); return { data: messages }; },
    listCalls: async () => ({ data: calls }),
    getCallSummary: async (id) => summaries[id] || null,
    getTranscript: async (id) => transcripts[id] || null,
    sendMessage: async () => { writes++; }, updateContact: async () => { writes++; }, createContact: async () => { writes++; },
  };
}

test('finds a name the customer gave in a text, with the exact quote', async () => {
  const quo = quoWith({ messages: [
    { direction: 'outgoing', text: 'Hi, this is Jaelan with Imperial Auto Care', createdAt: '2026-11-01T10:00:00Z' },
    { direction: 'incoming', text: 'Hey this is Dana Lee, my Focus is grinding', createdAt: '2026-11-01T10:05:00Z' },
  ] });
  const complete = async () => JSON.stringify({ first: 'dana', last: 'lee', quote: 'this is Dana Lee' });
  const r = await findName({ quo, phone: '+16235550177', complete, phoneNumberId: 'PN' });
  assert.deepEqual(r.name, { first: 'Dana', last: 'Lee', quote: 'this is Dana Lee', source: 'text' });
  assert.equal(quo.writes(), 0);
});

test('rejects an invented or shop-side name', () => {
  const lines = [{ who: 'SHOP', text: 'Hi, this is Jaelan with Imperial', source: 'text' }, { who: 'CUSTOMER', text: 'my truck wont start', source: 'text' }];
  assert.equal(verify({ first: 'Jaelan', last: '', quote: 'this is Jaelan' }, lines), null); // shop said it
  assert.equal(verify({ first: 'Mike', last: '', quote: 'my truck wont start' }, lines), null); // not in the quote
  assert.equal(verify({ first: 'Mike', last: '', quote: 'this is Mike' }, lines), null); // quote is made up
  assert.equal(verify({ first: '', last: '', quote: '' }, lines), null);
});

test('uses the caller side of a call transcript and call summaries', async () => {
  const quo = quoWith({
    calls: [{ id: 'C1', duration: 120, createdAt: '2026-11-02T10:00:00Z' }],
    transcripts: { C1: { dialogue: [
      { identifier: '+14809144144', userId: 'US1', content: 'Imperial Auto Care, this is Jaelan' },
      { identifier: '+16235550177', content: "Yeah hi, it's Sam Rivera, calling about my Ram" },
    ] } },
  });
  const complete = async () => JSON.stringify({ first: 'Sam', last: 'Rivera', quote: "it's Sam Rivera" });
  const r = await findName({ quo, phone: '+16235550177', complete, phoneNumberId: 'PN' });
  assert.equal(r.name.source, 'call');
  assert.equal(r.checked.calls, 1);
});

test('AI outage returns no name and says why', async () => {
  const quo = quoWith({ messages: [{ direction: 'incoming', text: 'this is Dana', createdAt: '2026-11-01T10:05:00Z' }] });
  const r = await findName({ quo, phone: '+16235550177', complete: async () => { throw Error('down'); }, phoneNumberId: 'PN' });
  assert.deepEqual([r.name, r.error], [null, 'ai_unavailable']);
});
