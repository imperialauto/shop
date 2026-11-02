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
  };
  const shopClient = {
    dispatchIntake: async (message) => { calls.intake.push(message); return intake(message); },
    dispatchSync: async (report) => { calls.sync.push(report); return { recorded: true }; },
  };
  return { quo, shopClient, calls };
}

test("offers only inbound messages and never sends", async () => {
  const f = fixture();
  const totals = await runRecovery({ ...f, phoneNumberId: "PNtest" });
  assert.deepEqual(f.calls.intake.map((m) => m.messageId), ["m1"]);
  assert.equal(totals.recorded, 1);
  assert.equal(f.calls.sends, 0);
  assert.equal(f.calls.sync[0].ok, true);
});

test("messages already queued count as healthy", async () => {
  const f = fixture({ intake: async () => ({ recorded: false, duplicate: true }) });
  const totals = await runRecovery({ ...f, phoneNumberId: "PNtest" });
  assert.equal(totals.failed, 0);
  assert.equal(f.calls.sync[0].ok, true);
});

test("failed intake and Quo errors are reported", async () => {
  const rejected = fixture({ intake: async () => ({ recorded: false, reason: "error" }) });
  await runRecovery({ ...rejected, phoneNumberId: "PNtest" });
  assert.equal(rejected.calls.sync[0].ok, false);
  const down = fixture({ listError: true });
  await runRecovery({ ...down, phoneNumberId: "PNtest" });
  assert.equal(down.calls.sync[0].ok, false);
});
