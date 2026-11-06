const { test, beforeEach } = require("node:test");
const assert = require("node:assert/strict");
const { verifyApprovedReplyContext } = require("../src/lib/replyContext");
const { handleIntakeResult } = require("../src/lib/dispatchActions");

const OWNER = "+16025550199";
const CUSTOMER = "+16025550100";
const message = { id: "customer-1", createdAt: "2026-11-06T18:00:00Z" };
const reply = { key: "reply:9:customer-1", itemId: 9, phone: CUSTOMER,
                messageId: message.id, conversationId: "CNfake", text: "Tuesday works." };

function fakeQuo({ messages = [message], calls = [], callFailure = false } = {}) {
  const sent = [];
  return {
    sent,
    listMessages: async () => ({ data: messages }),
    listCalls: async () => {
      if (callFailure) throw Error("Quo unavailable");
      return { data: calls };
    },
    sendMessage: async (record) => { sent.push(record); },
  };
}
function fakeShop() {
  const outbound = [];
  return {
    outbound,
    dispatchOutbound: async (record) => { outbound.push(record); },
    logCommunication: async () => ({ logged: true }),
  };
}

beforeEach(() => {
  delete process.env.REVALIDATE_APPROVED_REPLIES_ENABLED;
  delete process.env.OWNER_TEXTS_ENABLED;
});

test("a reply with unchanged SMS/call context can be approved", async () => {
  const quo = fakeQuo();
  const state = await verifyApprovedReplyContext({ quo, phone: CUSTOMER, messageId: message.id });
  assert.deepEqual(state, { ok: true, reason: "verified" });
});

test("any call after source SMS invalidates draft, including Sona answered call", async () => {
  const quo = fakeQuo({ calls: [{ id: "call-new", aiHandled: true, createdAt: "2026-11-06T18:10:00Z" }] });
  const state = await verifyApprovedReplyContext({ quo, phone: CUSTOMER, messageId: message.id });
  assert.equal(state.ok, false);
  assert.equal(state.reason, "new_call");
});

test("newer text invalidates draft", async () => {
  const quo = fakeQuo({ messages: [message, { id: "customer-2", createdAt: "2026-11-06T18:12:00Z" }] });
  const state = await verifyApprovedReplyContext({ quo, phone: CUSTOMER, messageId: message.id });
  assert.equal(state.reason, "new_message");
});

test("Quo outage or missing timestamp fails closed", async () => {
  const quo = fakeQuo({ callFailure: true });
  const result = await verifyApprovedReplyContext({ quo, phone: CUSTOMER, messageId: message.id });
  assert.equal(result.ok, false);
  assert.equal(result.reason, "unavailable");
  const unknown = await verifyApprovedReplyContext({
    quo: fakeQuo({ messages: [{ id: message.id }] }), phone: CUSTOMER, messageId: message.id,
  });
  assert.equal(unknown.reason, "unavailable");
});

test("owner approved reply is not sent after a newer call; owner receives explanation", async () => {
  process.env.OWNER_TEXTS_ENABLED = "true";
  process.env.REVALIDATE_APPROVED_REPLIES_ENABLED = "true";
  const quo = fakeQuo({ calls: [{ id: "call-2", createdAt: "2026-11-06T18:11:00Z" }] });
  const shopClient = fakeShop();
  const result = await handleIntakeResult({
    result: { customerReply: reply }, messageId: "owner-1", phone: OWNER, quo, shopClient,
  });
  assert.equal(result.customerSent, false);
  assert.equal(shopClient.outbound[0].status, "skipped");
  assert.equal(quo.sent.length, 1);
  assert.equal(quo.sent[0].to, OWNER);
  assert.match(quo.sent[0].content, /NOT sent.*call since/);
});

test("approved reply can send only when both source checks succeed", async () => {
  process.env.OWNER_TEXTS_ENABLED = "true";
  process.env.REVALIDATE_APPROVED_REPLIES_ENABLED = "true";
  const quo = fakeQuo();
  const shopClient = fakeShop();
  const result = await handleIntakeResult({
    result: { customerReply: reply }, messageId: "owner-1", phone: OWNER, quo, shopClient,
  });
  assert.equal(result.customerSent, true);
  assert.equal(quo.sent[0].to, CUSTOMER);
  assert.equal(quo.sent[1].to, OWNER);
  assert.equal(shopClient.outbound[0].status, "sent");
});

test("call fetch failure never permits a customer send", async () => {
  process.env.OWNER_TEXTS_ENABLED = "true";
  process.env.REVALIDATE_APPROVED_REPLIES_ENABLED = "true";
  const quo = fakeQuo({ callFailure: true });
  const shopClient = fakeShop();
  const result = await handleIntakeResult({
    result: { customerReply: reply }, messageId: "owner-1", phone: OWNER, quo, shopClient,
  });
  assert.equal(result.customerSent, false);
  assert.equal(quo.sent.length, 1);
  assert.equal(quo.sent[0].to, OWNER);
  assert.equal(shopClient.outbound[0].status, "skipped");
});
