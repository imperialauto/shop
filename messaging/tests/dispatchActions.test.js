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

test("a failed owner reply does not stop pending alerts", async () => {
  process.env.OWNER_TEXTS_ENABLED = "true";
  const f = fixture({ tick: [{ key: "alert:1:m1", text: "Approval #1" }] });
  const send = f.quo.sendMessage;
  let first = true;
  f.quo.sendMessage = async (m) => {
    if (first) { first = false; throw new Error("quo down"); }
    return send(m);
  };
  await handleIntakeResult({ result: { ownerReply: "Nothing is waiting on you." }, messageId: "m1", phone: OWNER, ...f });
  assert.deepEqual(f.outcomes, [{ key: "alert:1:m1", status: "sent" }]);
});

test("owner texts go only to the owner and report each result", async () => {
  process.env.OWNER_TEXTS_ENABLED = "true";
  const f = fixture({ tick: [{ key: "alert:1:m1", text: "Approval #1" }] });
  await deliverOwnerMessages(f);
  assert.deepEqual(f.sends, [{ to: OWNER, content: "Approval #1" }]);
  assert.deepEqual(f.outcomes, [{ key: "alert:1:m1", status: "sent" }]);
});
