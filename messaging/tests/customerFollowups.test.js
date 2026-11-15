const test = require("node:test");
const assert = require("node:assert/strict");
const { sendCustomerFollowup } = require("../src/lib/customerFollowups");

test("customer follow-up is sent once only after Quo context and current shop eligibility are rechecked", async () => {
  const reminder = { phone: "+16025550172", key: "fixture", messageId: "last", text: "A fixture check-in" };
  const latest = { id: "last", direction: "outgoing", createdAt: "2026-11-15T19:00:00Z" };
  for (const scenario of ["eligible", "opted out", "new reply", "new call", "shop paused", "ambiguous send"]) {
    const sends = [];
    const reports = [];
    const quo = {
      listMessages: async () => ({ data: [
        ...(scenario === "new reply" ? [{ id: "new", direction: "incoming", createdAt: "2026-11-16T19:00:00Z" }] : []),
        latest,
        ...(scenario === "opted out" ? [{ id: "earlier", direction: "incoming", text: "STOP" }] : []),
      ] }),
      listCalls: async () => ({ data: scenario === "new call" ? [{ createdAt: "2026-11-16T19:00:00Z" }] : [] }),
      sendMessage: async (message) => {
        sends.push(message);
        if (scenario === "ambiguous send") throw Error("fixture timeout");
        return { id: "sent" };
      },
    };
    const shopClient = {
      checkCustomerFollowup: async () => ({ ok: scenario !== "shop paused" }),
      logCommunication: async () => {},
      dispatchOutbound: async (status) => reports.push(status),
    };
    await sendCustomerFollowup({ reminder, quo, shopClient });
    assert.equal(sends.length, ["eligible", "ambiguous send"].includes(scenario) ? 1 : 0, scenario);
    assert.deepEqual(reports, [{ key: "fixture", status: scenario === "eligible" ? "sent"
      : scenario === "ambiguous send" ? "failed" : "skipped" }], scenario);
  }
});
