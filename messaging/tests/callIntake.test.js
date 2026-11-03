const { test } = require("node:test");
const assert = require("node:assert/strict");
const { missedCall, callContext, isMissed } = require("../src/lib/callIntake");

const call = (id, extra = {}) => ({ id, direction: "incoming", status: "completed", duration: 0, createdAt: "2026-11-02T20:00:00Z", ...extra });
const quo = (calls, voicemail, transcript) => ({
  listCalls: async () => ({ data: calls }),
  getVoicemail: async () => { if (!voicemail) throw new Error("404"); return voicemail; },
  getTranscript: async () => { if (!transcript) throw new Error("404"); return transcript; },
});

test("answered and outgoing calls are not missed", () => {
  assert.equal(isMissed(call("a", { duration: 30 })), false);
  assert.equal(isMissed(call("a", { direction: "outgoing" })), false);
  assert.equal(isMissed(call("a", { status: "no-answer" })), true);
});

test("uses the newest missed call and its voicemail", async () => {
  const q = quo([call("old", { createdAt: "2026-11-01T20:00:00Z" }), call("new")], { transcript: "2003 Mercedes rack and pinion" });
  assert.deepEqual(await missedCall({ quo: q, phone: "+16025550100", since: "x" }),
    { id: "new", body: "Voicemail: 2003 Mercedes rack and pinion" });
});

test("a plain missed call is still a lead", async () => {
  const found = await missedCall({ quo: quo([call("a")]), phone: "+16025550100", since: "x" });
  assert.equal(found.body, "Missed call, no voicemail.");
});

test("robocallers, skipped numbers and silent toll-free calls are ignored", async () => {
  const spam = quo([call("a")], { transcript: "urgent issue with your Google business listing" });
  assert.equal(await missedCall({ quo: spam, phone: "+16025550100", since: "x" }), null);
  assert.equal(await missedCall({ quo: quo([call("a")]), phone: "+18335550100", since: "x" }), null);
  const skip = new Set(["6025550100"]);
  assert.equal(await missedCall({ quo: quo([call("a")]), phone: "+16025550100", since: "x", skip }), null);
});

test("call context reads voicemail and transcript text", async () => {
  const q = quo([call("a")], { transcript: "Need a tow" }, { dialogue: [{ content: "About twenty minutes" }] });
  const context = await callContext({ quo: q, phone: "+16025550100" });
  assert.match(context, /Need a tow/);
  assert.match(context, /About twenty minutes/);
});

test("a missed call you already returned is not a lead", async () => {
  const missed = call("a");
  const later = "2026-11-02T20:10:00Z";
  const called = quo([missed, call("b", { direction: "outgoing", duration: 35, createdAt: later })]);
  assert.equal(await missedCall({ quo: called, phone: "+16025550100", since: "x" }), null);
  const texted = [{ direction: "outgoing", createdAt: later }];
  assert.equal(await missedCall({ quo: quo([missed]), phone: "+16025550100", since: "x", messages: texted }), null);
});

test("an automatic text does not hide a call when the customer keeps writing", async () => {
  const call = { id: "AC1", direction: "incoming", status: "completed", duration: 0, createdAt: "2026-11-03T10:00:00Z" };
  const quo = { listCalls: async () => ({ data: [call] }), getVoicemail: async () => ({ transcript: "I'm at the door" }) };
  const messages = [
    { direction: "outgoing", createdAt: "2026-11-03T10:01:00Z" },
    { direction: "incoming", createdAt: "2026-11-03T10:02:00Z" },
  ];
  const found = await missedCall({ quo, phone: "+16025550100", since: "2026-11-03T09:00:00Z", messages });
  assert.equal(found.id, "AC1");
});

test("draft context keeps speakers, time order and the end of a long call", async () => {
  const long = Array.from({ length: 200 }, (_, i) => ({ identifier: "+14809144144", content: `early line ${i}` }));
  const dialogue = [...long, { identifier: "+16025550100", content: "I can be there in fifteen minutes" }];
  const quo = {
    listCalls: async () => ({ data: [
      { id: "AC2", createdAt: "2026-11-03T12:00:00Z" },
      { id: "AC1", createdAt: "2026-11-03T10:00:00Z" },
    ] }),
    getVoicemail: async () => null,
    getTranscript: async (id) => ({ dialogue: id === "AC2" ? dialogue : [{ identifier: "+14809144144", content: "first call" }] }),
  };
  const text = await callContext({ quo, phone: "+16025550100" });
  assert.ok(text.indexOf("first call") < text.indexOf("Customer: I can be there in fifteen minutes"));
  assert.match(text, /Customer: I can be there in fifteen minutes/);
  assert.doesNotMatch(text, /early line 0\n/);
});
