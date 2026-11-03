const { test } = require("node:test");
const assert = require("node:assert/strict");
const { createCallEvents, ensureCallWebhook, parseEvent, summaryBody, tokenMatches } = require("../src/lib/callEvents");

function fixture(call = {}) {
  const log = { intake: [], drafts: [], passes: 0, delivered: 0, timers: [] };
  const quo = {
    getCall: async () => ({ direction: "incoming", aiHandled: true, participants: ["+16025550100"], ...call }),
    getCallSummary: async () => ({ summary: ["Customer is at the front door."], nextSteps: ["Call back"], jobs: [{ result: { data: [{ name: "Vehicle", value: "Sample car" }] } }] }),
    listConversations: async () => ({ data: [{ id: "CNfixture" }] }),
  };
  const shopClient = {
    dispatchIntake: async (m) => { log.intake.push(m); return { recorded: true }; },
    dispatchDraft: async (d) => { log.drafts.push(d); },
  };
  const events = createCallEvents({
    quo, shopClient, runPass: async () => { log.passes += 1; }, deliver: async () => { log.delivered += 1; },
    draft: async () => "Draft", schedule: (run) => log.timers.push(run),
  });
  return { events, log };
}

const summaryEvent = { id: "EV1", type: "call.summary.completed", data: { resource: { callId: "AC1" } } };

test("an AI call summary reaches the owner as one alert", async () => {
  const { events, log } = fixture();
  await events.handle(summaryEvent);
  await events.handle(summaryEvent);
  assert.equal(log.intake.length, 1);
  assert.match(log.intake[0].body, /front door.*Next: Call back.*Vehicle: Sample car/);
  assert.equal(log.intake[0].kind, "call");
  assert.equal(log.drafts.length, 1);
  assert.equal(log.delivered, 1);
});

test("a summary of a call that did not go to the AI is ignored", async () => {
  const { events, log } = fixture({ aiHandled: false });
  await events.handle(summaryEvent);
  assert.equal(log.intake.length, 0);
});

test("a call on another line is ignored", async () => {
  const log = [];
  const events = createCallEvents({
    quo: { getCall: async () => ({ direction: "incoming", aiHandled: true, phoneNumberId: "PNother", participants: ["+16025550100"] }) },
    shopClient: { dispatchIntake: async (m) => { log.push(m); return {}; } },
    phoneNumberId: "PNmine", runPass: async () => {}, deliver: async () => {},
  });
  await events.handle(summaryEvent);
  assert.equal(log.length, 0);
});

test("a voicemail checks right away and a missed call waits for its transcript", async () => {
  const { events, log } = fixture();
  await events.handle({ id: "EV2", type: "call.voicemail.completed", data: { resource: { callId: "AC2" } } });
  assert.equal(log.passes, 1);
  await events.handle({ id: "EV3", type: "call.missed", data: { resource: { id: "AC3" } } });
  assert.equal(log.passes, 1);
  await log.timers[0]();
  assert.equal(log.passes, 2);
  assert.equal(log.delivered, 2);
});

test("bad tokens are refused", () => {
  assert.equal(tokenMatches("abc", "abc"), true);
  assert.equal(tokenMatches("abd", "abc"), false);
  assert.equal(tokenMatches("", ""), false);
  assert.equal(tokenMatches("abc", undefined), false);
});

test("both Quo event shapes are read", () => {
  assert.equal(parseEvent({ type: "call.completed", data: { object: { id: "AC1" } } }).callId, "AC1");
  assert.equal(parseEvent({ type: "call.summary.completed", data: { resource: { callId: "AC2" } } }).callId, "AC2");
});

test("summary text copes with missing parts", () => {
  assert.equal(summaryBody({ summary: "Wants brakes" }), "AI answered a call. Wants brakes");
});

test("the webhook is only created once", async () => {
  const made = [];
  const quo = { listWebhooks: async () => [{ url: "https://x/y" }], createCallWebhook: async (a) => made.push(a) };
  assert.equal(await ensureCallWebhook({ quo, url: "https://x/y" }), "exists");
  assert.equal(await ensureCallWebhook({ quo, url: "https://x/z" }), "created");
  assert.equal(made.length, 1);
});

test("the shop's own number is never treated as the caller", async () => {
  const { events, log } = fixture({ participants: ["+14809144144", "+16025550100"] });
  process.env.QUO_DEFAULT_FROM_NUMBER = "+14809144144";
  const fresh = createCallEvents({
    quo: { getCall: async () => ({ direction: "incoming", aiHandled: true, participants: ["+14809144144", "+16025550100"] }),
      getCallSummary: async () => ({ summary: ["Wants brakes"] }), listConversations: async () => ({ data: [] }) },
    shopClient: { dispatchIntake: async (m) => { log.intake.push(m); return {}; } },
    runPass: async () => {}, deliver: async () => {},
  });
  await fresh.handle(summaryEvent);
  assert.equal(log.intake[0].phone, "+16025550100");
  void events;
});

for (const [name, summary] of [["empty", { summary: [] }], ["robocall", { summary: ["The caller delivered an automated financial loan message."] }]]) {
  test(`a ${name} summary raises no alert`, async () => {
    const log = [];
    const events = createCallEvents({
      quo: { getCall: async () => ({ direction: "incoming", aiHandled: true, participants: ["+16025550100"] }), getCallSummary: async () => summary },
      shopClient: { dispatchIntake: async (m) => { log.push(m); return {}; } },
      runPass: async () => {}, deliver: async () => {},
    });
    await events.handle(summaryEvent);
    assert.equal(log.length, 0);
  });
}
