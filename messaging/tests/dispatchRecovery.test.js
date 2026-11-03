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
