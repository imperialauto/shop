const test = require("node:test");
const assert = require("node:assert/strict");
const { sendBookingConfirmation } = require("../src/lib/bookingConfirmations");

test("booking confirmation is sent once, never after STOP, and never retried", async () => {
  const confirmation = { key: "bookconf:7", phone: "+16025550177", text: "Fixture confirmation" };
  for (const scenario of ["new customer", "earlier thread", "opted out", "unreadable thread", "ambiguous send"]) {
    const sends = [];
    const reports = [];
    const logged = [];
    const quo = {
      listMessages: async () => {
        if (scenario === "unreadable thread") return { error: "fixture" };
        if (scenario === "new customer") return { data: [] };
        return { data: [
          { id: "a", direction: "outgoing", text: "Fixture earlier reply" },
          ...(scenario === "opted out" ? [{ id: "b", direction: "incoming", text: "STOP" }] : []),
        ] };
      },
      sendMessage: async (message) => {
        sends.push(message);
        if (scenario === "ambiguous send") throw Error("fixture timeout");
        return { id: "sent", createdAt: "2026-11-25T18:00:00Z" };
      },
    };
    const shopClient = {
      logCommunication: async (row) => logged.push(row),
      dispatchOutbound: async (report) => reports.push(report),
    };
    await sendBookingConfirmation({ confirmation, quo, shopClient });
    const sent = ["new customer", "earlier thread"].includes(scenario);
    assert.equal(sends.length, sent || scenario === "ambiguous send" ? 1 : 0, scenario);
    if (sends.length) assert.deepEqual(sends[0], { to: confirmation.phone, content: confirmation.text }, scenario);
    assert.equal(logged.length, sent ? 1 : 0, scenario);
    assert.deepEqual(reports, [{ key: "bookconf:7", status: sent ? "sent"
      : scenario === "ambiguous send" ? "failed" : "skipped" }], scenario);
  }
});

test("the dispatch loop sends booking confirmations without an owner notice", async () => {
  const { deliverOwnerMessages } = require("../src/lib/dispatchActions");
  const saved = { ...process.env };
  process.env.OWNER_TEXTS_ENABLED = "false";
  process.env.CUSTOMER_FOLLOWUP_WORKER_ENABLED = "false";
  process.env.BOOKING_CONFIRMATIONS_ENABLED = "true";
  try {
    const sends = [];
    const ticks = [];
    const quo = {
      listMessages: async () => ({ data: [] }),
      sendMessage: async (message) => { sends.push(message); return { id: "sent" }; },
    };
    const shopClient = {
      dispatchTick: async (options) => { ticks.push(options); return { ownerPhone: "+16025550100",
        bookingConfirmations: [{ key: "bookconf:9", phone: "+16025550199", text: "Fixture booked" }] }; },
      logCommunication: async () => {},
      dispatchOutbound: async () => {},
    };
    await deliverOwnerMessages({ quo, shopClient });
    assert.deepEqual(ticks, [{ ownerTexts: false }]);
    assert.deepEqual(sends.map((m) => m.to), ["+16025550199"]);
  } finally {
    process.env = saved;
  }
});
