const { test } = require("node:test");
const assert = require("node:assert/strict");
const { runRecovery } = require("../src/lib/dispatchRecovery");

function fixture({ listError = false, intake = async () => ({ recorded: true }) } = {}) {
  const calls = { intake: [], sync: [], sends: 0 };
  const quo = {
    listConversations: async () => {
      if (listError) throw new Error("quo unavailable");
      return { data: [{ id: "CNtest", participants: ["+16025550100"] }] };
    },
    listMessages: async () => ({
      data: [
        { id: "m1", direction: "incoming", text: "Need brakes", from: "+16025550100" },
        { id: "m2", direction: "outgoing", text: "Sure" },
      ],
    }),
    sendMessage: async () => { calls.sends += 1; },
    listCalls: async () => ({ data: [] }),
  };
  const shopClient = {
    dispatchIntake: async (message) => { calls.intake.push(message); return intake(message); },
    dispatchHandled: async () => ({ closed: true }),
    dispatchSync: async (report) => { calls.sync.push(report); return { recorded: true }; },
  };
  return { quo, shopClient, calls };
}

test("offers only inbound messages and never sends", async () => {
  const f = fixture();
  const totals = await runRecovery({ ...f, phoneNumberId: "PNtest", pauseMs: 0 });
  assert.deepEqual(f.calls.intake.map((m) => m.messageId), ["m1"]);
  assert.equal(totals.recorded, 1);
  assert.equal(f.calls.sends, 0);
  assert.equal(f.calls.sync[0].ok, true);
});

test("a missed call is queued with a draft and sends nothing", async () => {
  const f = fixture();
  f.quo.listCalls = async () => ({ data: [{ id: "call1", direction: "incoming", status: "no-answer", createdAt: "2026-11-02T20:00:00Z" }] });
  f.quo.getVoicemail = async () => ({ transcript: "Please call me about my 2003 Acura" });
  f.quo.getTranscript = async () => { throw new Error("none"); };
  const drafts = [];
  f.shopClient.dispatchDraft = async (d) => { drafts.push(d); };
  await runRecovery({ ...f, phoneNumberId: "PNtest", pauseMs: 0, draft: async () => "Sorry we missed you." });
  const queued = f.calls.intake.find((m) => m.kind === "call");
  assert.equal(queued.messageId, "call1");
  assert.equal(queued.threadMessageId, "m1");
  assert.equal(queued.recovery, true);
  assert.equal(queued.callOutcome, "voicemail");
  assert.equal(drafts[0].body, "Sorry we missed you.");
  assert.equal(f.calls.sends, 0);
});

test("messages already queued count as healthy", async () => {
  const f = fixture({ intake: async () => ({ recorded: false, duplicate: true }) });
  const totals = await runRecovery({ ...f, phoneNumberId: "PNtest", pauseMs: 0 });
  assert.equal(totals.failed, 0);
  assert.equal(f.calls.sync[0].ok, true);
});

test("failed intake and Quo errors are reported", async () => {
  const rejected = fixture({ intake: async () => ({ recorded: false, reason: "error" }) });
  await runRecovery({ ...rejected, phoneNumberId: "PNtest", pauseMs: 0 });
  assert.equal(rejected.calls.sync[0].ok, false);
  const down = fixture({ listError: true });
  await runRecovery({ ...down, phoneNumberId: "PNtest", pauseMs: 0 });
  assert.equal(down.calls.sync[0].ok, false);
});

// Recovery Nov 15 (D): history mirroring and draft retries, never a send.
function d1fixture() {
  const f = fixture();
  f.mirrored = [];
  f.handled = [];
  f.drafts = [];
  f.failures = [];
  f.quo.listMessages = async () => ({ data: [
    { id: "in1", direction: "incoming", text: "Just sent 200", createdAt: "2026-11-15T19:36:05Z" },
    { id: "out1", direction: "outgoing", text: "got it", createdAt: "2026-11-15T19:38:30Z" },
    { id: "out2", direction: "outgoing", text: "", createdAt: "2026-11-16T00:38:01Z", media: [{ url: "a" }, { url: "b" }] },
  ] });
  f.shopClient.logCommunication = async (c) => { f.mirrored.push(c); return { logged: true }; };
  f.shopClient.dispatchHandled = async (h) => { f.handled.push(h); return { closed: true }; };
  f.shopClient.dispatchDraft = async (d) => { f.drafts.push(d); return { recorded: true }; };
  f.shopClient.draftFailed = async (x) => { f.failures.push(x); return { recorded: true }; };
  return f;
}

test("outgoing Quo texts reach the customer history with their own time and the phone closes the item", async () => {
  const f = d1fixture();
  f.shopClient.draftsNeeded = async () => ({ items: [] });
  const totals = await runRecovery({ ...f, phoneNumberId: "PNtest", pauseMs: 0 });
  assert.deepEqual(f.mirrored.map((c) => [c.externalId, c.direction, c.body, c.createdAt]), [
    ["in1", "inbound", "Just sent 200", "2026-11-15T19:36:05Z"],
    ["out1", "outbound", "got it", "2026-11-15T19:38:30Z"],
    ["out2", "outbound", "[2 photos]", "2026-11-16T00:38:01Z"],
  ]);
  assert.equal(totals.mirrored, 3);
  assert.equal(f.handled.at(-1).phone, "+16025550100");
  assert.equal(f.calls.sends, 0);
  assert.match(f.calls.sync[0].detail, /mirrored 3/);
});

test("a repeat run mirrors nothing new (the shop app reports duplicates)", async () => {
  const f = d1fixture();
  f.shopClient.logCommunication = async (c) => { f.mirrored.push(c); return { logged: true, duplicate: true }; };
  f.shopClient.draftsNeeded = async () => ({ items: [] });
  const totals = await runRecovery({ ...f, phoneNumberId: "PNtest", pauseMs: 0 });
  assert.equal(totals.mirrored, 0);
});

test("customer messages without a draft are drafted, and failures are recorded, never sent", async () => {
  const f = d1fixture();
  f.shopClient.draftsNeeded = async () => ({ items: [
    { itemId: 1, conversationId: "CNa", messageId: "m-a", phone: "+16025550111" },
    { itemId: 2, conversationId: "CNb", messageId: "m-b", phone: "+16025550122" },
  ] });
  let n = 0;
  const totals = await runRecovery({ ...f, phoneNumberId: "PNtest", pauseMs: 0,
    draft: async () => (n++ === 0 ? "Hi, what's the year, make and model?" : null) });
  assert.deepEqual(f.drafts.map((d) => [d.conversationId, d.messageId]), [["CNa", "m-a"]]);
  assert.deepEqual(f.failures.map((x) => [x.conversationId, x.messageId, x.reason]), [["CNb", "m-b", "empty draft"]]);
  assert.equal(totals.drafted, 1);
  assert.equal(totals.draftFailed, 1);
  assert.equal(f.calls.sends, 0);
});

test("a draft the shop app rejects (newer message arrived) counts as a failure", async () => {
  const f = d1fixture();
  f.shopClient.draftsNeeded = async () => ({ items: [{ itemId: 1, conversationId: "CNa", messageId: "m-old", phone: "+1" }] });
  f.shopClient.dispatchDraft = async () => ({ recorded: false });
  await runRecovery({ ...f, phoneNumberId: "PNtest", pauseMs: 0, draft: async () => "text" });
  assert.equal(f.failures[0].reason, "draft not accepted");
});
