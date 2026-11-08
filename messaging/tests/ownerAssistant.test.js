const { test } = require("node:test");
const assert = require("node:assert/strict");
const { interpretOwner, jobNote, answerOwner, recentTurns, mentionedItems, FALLBACK } = require("../src/lib/ownerAssistant");

test("sends the snapshot, history and question to the model", async () => {
  let sent;
  const reply = await answerOwner({
    question: "any task?",
    snapshot: { waitingOnYou: [{ id: 5 }] },
    history: [{ role: "user", content: "hi" }],
    complete: async (messages) => { sent = messages; return "#5 is waiting on you."; },
  });
  assert.equal(reply, "#5 is waiting on you.");
  assert.match(sent[0].content, /waitingOnYou/);
  assert.deepEqual(sent.at(-1), { role: "user", content: "any task?" });
});

test("a model failure returns a plain fallback and long answers are trimmed", async () => {
  assert.equal(await answerOwner({ question: "x", snapshot: {}, complete: async () => { throw new Error("down"); } }), FALLBACK);
  const long = await answerOwner({ question: "x", snapshot: {}, complete: async () => "a".repeat(2000) });
  assert.equal(long.length, 900);
});

test("history is ordered and skips the current text", () => {
  const turns = recentTurns([
    { id: "2", direction: "outgoing", text: "Nothing waiting.", createdAt: "2026-11-02T02:00:00Z" },
    { id: "1", direction: "incoming", text: "open", createdAt: "2026-11-02T01:00:00Z" },
    { id: "3", direction: "incoming", text: "any task?", createdAt: "2026-11-02T03:00:00Z" },
  ], "3");
  assert.deepEqual(turns, [{ role: "user", content: "open" }, { role: "assistant", content: "Nothing waiting." }]);
});

test("finds the customers a question names", () => {
  const snapshot = { waitingOnYou: [{ id: 9, who: "2005 GMC Yukon" }, { id: 5, who: "2013 Ford Fusion" }] };
  assert.deepEqual(mentionedItems("what did the yukon customer say?", snapshot).map((i) => i.id), [9]);
  assert.deepEqual(mentionedItems("update on #5", snapshot).map((i) => i.id), [5]);
  assert.deepEqual(mentionedItems("any tasks?", snapshot), []);
});

const openSnapshot = { waitingOnYou: [{ id: 24, who: "Sample Altima" }] };

test("a plain-English reply becomes a proposal for a known item", async () => {
  const action = await interpretOwner({ question: "tell her tomorrow at 1", snapshot: openSnapshot,
    complete: async () => '{"action":"propose","itemId":24,"text":"We can see you tomorrow at 1.","hours":null}' });
  assert.deepEqual(action, { action: "propose", itemId: 24, text: "We can see you tomorrow at 1.", hours: null });
});

test("an unknown item or bad output falls back to a normal answer", async () => {
  assert.equal(await interpretOwner({ question: "x", snapshot: openSnapshot, complete: async () => '{"action":"done","itemId":99}' }), null);
  assert.equal(await interpretOwner({ question: "x", snapshot: openSnapshot, complete: async () => "not json" }), null);
  assert.equal(await interpretOwner({ question: "x", snapshot: openSnapshot, complete: async () => { throw new Error("down"); } }), null);
  assert.equal(await interpretOwner({ question: "x", snapshot: openSnapshot, complete: async () => '{"action":"propose","itemId":24,"text":""}' }), null);
});

test("job notes are one line and failures are harmless", async () => {
  assert.equal(await jobNote({ conversation: "x", complete: async () => "Needs oil\nchange" }), "Needs oil change");
  assert.equal(await jobNote({ conversation: "x", complete: async () => { throw new Error("down"); } }), null);
});

test("a request to check prices is not turned into a reminder", async () => {
  const snapshot = { waitingOnYou: [{ id: 48 }] };
  const complete = async () => JSON.stringify({ action: "later", itemId: 48, hours: 2 });
  assert.equal(await interpretOwner({ question: "#48 can you check prices?", snapshot, complete }), null);
  const asked = await interpretOwner({ question: "remind me about 48 in 3 hours", snapshot, complete });
  assert.equal(asked.action, "later");
});

