const { test, beforeEach } = require("node:test");
const assert = require("node:assert/strict");
const { handleIntakeResult, deliverOwnerMessages } = require("../src/lib/dispatchActions");

const PHONE = "+16025550100";
const OWNER = "+16025550199";
const autoReply = { key: "auto:CN:hours:2026-45", text: "We open at 11." };

function fixture({ newest = "m1", sendError = false, tick = [] } = {}) {
  const sends = [];
  const outcomes = [];
  const quo = {
    listMessages: async () => ({ data: [{ id: "old", createdAt: "2026-11-01" }, { id: newest, createdAt: "2026-11-02" }] }),
    sendMessage: async (m) => { if (sendError) throw new Error("timeout"); sends.push(m); },
  };
  const shopClient = {
    dispatchOutbound: async (o) => { outcomes.push(o); return { recorded: true }; },
    dispatchTick: async () => ({ ownerPhone: OWNER, messages: tick }),
  };
  return { quo, shopClient, sends, outcomes };
}

beforeEach(() => {
  delete process.env.AUTO_REPLIES_ENABLED;
  delete process.env.OWNER_TEXTS_ENABLED;
});

test("nothing sends while the switches are off", async () => {
  const f = fixture({ tick: [{ key: "a", text: "x" }] });
  await handleIntakeResult({ result: { autoReply, ownerReply: "ok" }, messageId: "m1", phone: PHONE, ...f });
  assert.equal(f.sends.length, 0);
  assert.deepEqual(f.outcomes, [{ key: autoReply.key, status: "skipped" }]);
});

test("auto reply goes out only when the customer has not texted again", async () => {
  process.env.AUTO_REPLIES_ENABLED = "true";
  const fresh = fixture();
  await handleIntakeResult({ result: { autoReply }, messageId: "m1", phone: PHONE, ...fresh });
  assert.equal(fresh.sends.length, 1);
  assert.equal(fresh.outcomes[0].status, "sent");
  const stale = fixture({ newest: "m2" });
  await handleIntakeResult({ result: { autoReply }, messageId: "m1", phone: PHONE, ...stale });
  assert.equal(stale.sends.length, 0);
  assert.equal(stale.outcomes[0].status, "skipped");
});

test("a failed customer text is reported, not retried", async () => {
  process.env.AUTO_REPLIES_ENABLED = "true";
  const f = fixture({ sendError: true });
  await handleIntakeResult({ result: { autoReply }, messageId: "m1", phone: PHONE, ...f });
  assert.deepEqual(f.outcomes, [{ key: autoReply.key, status: "failed" }]);
});

const reply = { key: "reply:5:m1", itemId: 5, phone: PHONE, text: "We can help.", messageId: "m1", conversationId: "CN" };

test("YES sends the approved reply and confirms to the owner", async () => {
  process.env.OWNER_TEXTS_ENABLED = "true";
  const f = fixture();
  f.shopClient.logCommunication = async () => ({});
  const action = await handleIntakeResult({ result: { customerReply: reply }, messageId: "o1", phone: OWNER, ...f });
  assert.deepEqual(f.sends, [{ to: PHONE, content: "We can help." }, { to: OWNER, content: "Sent #5 to the customer." }]);
  assert.equal(f.outcomes[0].status, "sent");
  assert.equal(action.customerSent, true);
});

test("YES stops if anything newer is in the thread", async () => {
  process.env.OWNER_TEXTS_ENABLED = "true";
  const f = fixture({ newest: "m2" });
  await handleIntakeResult({ result: { customerReply: reply }, messageId: "o1", phone: OWNER, ...f });
  assert.equal(f.sends.length, 1);
  assert.equal(f.sends[0].to, OWNER);
  assert.match(f.sends[0].content, /not sent/);
  assert.equal(f.outcomes[0].status, "skipped");
});

test("a failed customer send tells the owner and is not retried", async () => {
  process.env.OWNER_TEXTS_ENABLED = "true";
  const f = fixture({ sendError: true });
  const action = await handleIntakeResult({ result: { customerReply: reply }, messageId: "o1", phone: OWNER, ...f });
  assert.equal(f.outcomes[0].status, "failed");
  assert.equal(action.customerSent, false);
});

test("a failed owner reply does not throw", async () => {
  process.env.OWNER_TEXTS_ENABLED = "true";
  const f = fixture({ sendError: true });
  await handleIntakeResult({ result: { ownerReply: "Nothing is waiting on you." }, messageId: "m1", phone: OWNER, ...f });
  assert.deepEqual(f.outcomes, []);
});

test("owner texts go only to the owner and report each result", async () => {
  process.env.OWNER_TEXTS_ENABLED = "true";
  const f = fixture({ tick: [{ key: "alert:1:m1", text: "Approval #1" }] });
  await deliverOwnerMessages(f);
  assert.deepEqual(f.sends, [{ to: OWNER, content: "Approval #1" }]);
  assert.deepEqual(f.outcomes, [{ key: "alert:1:m1", status: "sent" }]);
});
