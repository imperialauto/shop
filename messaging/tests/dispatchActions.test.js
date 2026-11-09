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
    listCalls: async () => ({ data: [] }),
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

test("owner-approved link is queued only after checking real customer Quo texts and calls", async () => {
  process.env.OWNER_TEXTS_ENABLED = "true";
  const observed = [];
  const sent = [];
  const quo = {
    listMessages: async ({ participants }) => ({
      data: participants[0] === OWNER ? [] : [
        { id: "customer-latest", createdAt: "2026-11-09T20:00:00Z", direction: "incoming" },
      ],
    }),
    listCalls: async () => ({ data: [] }),
    sendMessage: async (message) => sent.push(message),
  };
  const shopClient = {
    dispatchOwnerAction: async (request) => {
      observed.push(request);
      return request.customerMessageId
        ? { ownerReply: "Approval item #42 drafted. Nothing sent." }
        : { customerThreadPhone: PHONE };
    },
  };
  await handleIntakeResult({ result: { ownerQuestion: "prepare approval RO #17 fitment verified",
    snapshot: { waitingOnYou: [] } }, messageId: "owner-link-msg", phone: OWNER, quo, shopClient });
  assert.equal(observed.length, 2);
  assert.equal(observed[0].ownerMessageId, "owner-link-msg");
  assert.equal(observed[1].customerMessageId, "customer-latest");
  assert.equal(sent.length, 1);
  assert.equal(sent[0].to, OWNER);
  assert.match(sent[0].content, /Nothing sent/);
});

test("no approval link is queued when current Quo call context fails closed", async () => {
  process.env.OWNER_TEXTS_ENABLED = "true";
  const observed = [];
  const sent = [];
  const quo = {
    listMessages: async ({ participants }) => ({ data: participants[0] === OWNER ? [] : [
      { id: "customer-latest", createdAt: "2026-11-09T20:00:00Z", direction: "incoming" },
    ] }),
    listCalls: async () => { throw new Error("Quo unavailable"); },
    sendMessage: async (message) => sent.push(message),
  };
  const shopClient = {
    dispatchOwnerAction: async (request) => {
      observed.push(request);
      return { customerThreadPhone: PHONE };
    },
  };
  await handleIntakeResult({ result: { ownerQuestion: "prepare approval RO #17 fitment verified",
    snapshot: { waitingOnYou: [] } }, messageId: "owner-link-msg", phone: OWNER, quo, shopClient });
  assert.equal(observed.length, 1);
  assert.equal(sent.length, 1);
  assert.equal(sent[0].to, OWNER);
  assert.match(sent[0].content, /No approval link queued/);
});

test("old YES-approved quote is BLOCKED when it repeats a private vendor cost from owner history", async () => {
  process.env.OWNER_TEXTS_ENABLED = "true";
  const sent = [];
  const results = [];
  const quo = {
    listMessages: async ({ participants }) => ({ data: participants[0] === OWNER
      ? [{ id: "owner-cost", createdAt: "2026-11-09T21:00:00Z", direction: "incoming",
           text: "I sourced the tank for the Ford Fusion for $81.83 my cost." }]
      : [{ id: "customer-current", createdAt: "2026-11-09T20:00:00Z", direction: "incoming",
           text: "Any update on my Fusion?" }],
    }),
    listCalls: async () => ({ data: [] }),
    sendMessage: async (msg) => sent.push(msg),
  };
  const shopClient = {
    dispatchOutbound: async (r) => results.push(r),
    logCommunication: async () => { throw Error("customer must not be contacted"); },
  };
  const draft = { key: "reply:52:customer-current", itemId: 52, phone: PHONE,
    text: "We sourced your Ford Fusion tank for $81.83.", messageId: "customer-current",
    conversationId: "CNfusion" };
  await handleIntakeResult({ result: { customerReply: draft },
    messageId: "owner-yes", phone: OWNER, quo, shopClient });
  assert.equal(sent.length, 1);
  assert.equal(sent[0].to, OWNER);
  assert.match(sent[0].content, /BLOCKED/);
  assert.equal(results[0].status, "skipped");
});

test("a distinct approved selling price can still be sent after private-cost screening", async () => {
  process.env.OWNER_TEXTS_ENABLED = "true";
  const sent = [];
  const outcomes = [];
  const quo = {
    listMessages: async ({ participants }) => ({ data: participants[0] === OWNER
      ? [{ id: "owner-cost", createdAt: "2026-11-09T21:00:00Z", direction: "incoming",
           text: "I sourced the tank for $81.83 my cost." }]
      : [{ id: "customer-current", createdAt: "2026-11-09T20:00:00Z", direction: "incoming",
           text: "Can I get a quote?" }],
    }),
    listCalls: async () => ({ data: [] }),
    sendMessage: async (msg) => sent.push(msg),
  };
  const shopClient = {
    dispatchOutbound: async (r) => outcomes.push(r),
    logCommunication: async () => ({ logged: true }),
  };
  await handleIntakeResult({ result: { customerReply: {
    key: "reply:53:customer-current", itemId: 53, phone: PHONE,
    text: "Your approved part selling price is $155, labor quoted separately.",
    messageId: "customer-current", conversationId: "CNfusion",
  } }, messageId: "owner-yes", phone: OWNER, quo, shopClient });
  assert.equal(sent.length, 2);
  assert.equal(sent[0].to, PHONE);
  assert.equal(outcomes[0].status, "sent");
});
