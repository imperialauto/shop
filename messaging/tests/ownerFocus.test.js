const { test, beforeEach } = require("node:test");
const assert = require("node:assert/strict");
const { isAppDefectReport, defectReply, historyForFocus, guardAnswer, whichJobReply } = require("../src/lib/ownerFocus");
const { handleIntakeResult } = require("../src/lib/dispatchActions");

const OWNER = "+16025550199";
// The owner's actual Nov 15 texts (customer names kept; no phone numbers).
const HEADLIGHT = "Note I'm not able to upload photos to our number I believe it's eight for Antoine Chevy Impala SS. "
  + "I just installed a headlight bulb and it re-the headlight is currently working, but I'm not able to upload photos "
  + "of proof that the headlights working and complete that part of the repair there's no options for that.";
const RO8 = { ro: { roId: 8, label: "RO #8", customer: "Antwon Test", firstName: "Antwon", vehicle: "2009 Chevrolet Impala", lines: [] } };
const SNAPSHOT = { waitingOnYou: [
  { id: 111, who: "Unknown (1018) - 2014 jeep compass" },
  { id: 77, who: "Antwon Chevy Impala SS Coolant Oil Leak" },
] };

beforeEach(() => { process.env.OWNER_TEXTS_ENABLED = "true"; });

test("app-defect reports are recognized; questions, reminders and commands are not", () => {
  assert.equal(isAppDefectReport(HEADLIGHT), true);
  assert.equal(isAppDefectReport("The dashboard won't let me open task 118 from the homepage"), true);
  for (const text of ["111 more context?", "reminder buy microfiber towels and shop towels", "YES 109",
    "how do you want to proceed?", "send it!", "DONE 104"]) {
    assert.equal(isAppDefectReport(text), false, text);
  }
});

test("the reply quotes a Fix list ID only when the shop app saved it", () => {
  assert.match(defectReply({ id: 131, status: "open" }, RO8), /^Saved to the Fix list as #131 \(open\) for RO #8, Antwon Test \(2009 Chevrolet Impala\)\./);
  for (const failed of [null, {}, { status: "open" }]) {
    assert.match(defectReply(failed, RO8), /couldn't save that/);
    assert.doesNotMatch(defectReply(failed, RO8), /Saved/);
  }
});

test("another customer's approval number never gets attached to the record in focus", () => {
  const fixed = guardAnswer("Got it! For #111 with Antwon, you can update the customer.", { focus: RO8, snapshot: SNAPSHOT, question: "eight for Antoine" });
  assert.equal(fixed, "Got it! For RO #8 with Antwon, you can update the customer.");
  // His own approval keeps its number, labelled
  assert.match(guardAnswer("Antwon's #77 is still open.", { focus: RO8, snapshot: SNAPSHOT, question: "Antwon" }), /approval #77/);
  // A number the owner named himself is left alone
  assert.match(guardAnswer("#111 is the Jeep.", { focus: RO8, snapshot: SNAPSHOT, question: "what about #111" }), /#111 is the Jeep/);
  // Nothing to guard without a resolved record
  assert.equal(guardAnswer("For #111 do X", { focus: {}, snapshot: SNAPSHOT, question: "x" }), "For #111 do X");
});

test("typed records are never relabelled as the RO in focus (QA on #191)", () => {
  const ctx = { focus: RO8, snapshot: SNAPSHOT, question: "eight for Antoine" };
  for (const kept of ["Saved as task #118.", "That's Fix list #23.", "RO #12 is Dana's.", "Invoice #4 is paid.",
    "See job #5.", "Estimate #9 was sent."]) {
    assert.equal(guardAnswer(kept, ctx), kept, kept);
  }
  // A number he typed as another record ("approval 12", "task 12") counts as named
  assert.equal(guardAnswer("#12 is waiting.", { ...ctx, question: "approval 12?" }), "#12 is waiting.");
  assert.equal(guardAnswer("#12 is waiting.", { ...ctx, question: "task 12?" }), "#12 is waiting.");
  // Bare unnamed approval for someone else is still caught
  assert.equal(guardAnswer("Saved task #118. For #111 with Antwon, call back.", ctx),
    "Saved task #118. For RO #8 with Antwon, call back.");
});

test("a topic switch drops earlier turns about another customer's number", () => {
  const history = [
    { role: "user", content: "111 more context?" },
    { role: "assistant", content: "#111 is about a customer needing HVAC actuator installation on a 2014 Jeep Compass." },
    { role: "assistant", content: "FYI Antwon: Ok on it now" },
  ];
  const kept = historyForFocus(history, RO8, "the impala headlight is done");
  assert.deepEqual(kept.map((t) => t.content), ["FYI Antwon: Ok on it now"]);
  assert.equal(historyForFocus(history, RO8, "and #111?").length, 3); // he named a number: keep context
});

function owner({ saveResult, saveThrows = false, focus = RO8 } = {}) {
  const sent = [], saved = [], asked = [];
  const quo = {
    listMessages: async () => ({ data: [] }), listCalls: async () => ({ data: [] }),
    sendMessage: async (m) => sent.push(m),
  };
  const shopClient = {
    ownerFocus: async (text) => { asked.push(text); return focus; },
    saveOwnerProblem: async (report) => { if (saveThrows) throw new Error("503"); saved.push(report); return saveResult; },
  };
  return { quo, shopClient, sent, saved, asked };
}

test("a texted app problem is saved to the Fix list with the resolved RO and acknowledged with its ID", async () => {
  const f = owner({ saveResult: { id: 131, status: "open", title: "x" } });
  await handleIntakeResult({ result: { ownerQuestion: HEADLIGHT, snapshot: SNAPSHOT }, messageId: "owner-msg-1", phone: OWNER, ...f });
  assert.equal(f.saved.length, 1);
  assert.equal(f.saved[0].messageId, "owner-msg-1"); // the shop app uses it to ignore repeat deliveries
  assert.match(f.saved[0].context, /^RO #8 Antwon Test 2009 Chevrolet Impala$/);
  assert.equal(f.sent.length, 1);
  assert.match(f.sent[0].content, /Saved to the Fix list as #131 \(open\) for RO #8/);
  assert.doesNotMatch(f.sent[0].content, /#111/);
});

test("a failed save is reported as not saved, never as saved", async () => {
  const f = owner({ saveThrows: true });
  await handleIntakeResult({ result: { ownerQuestion: HEADLIGHT, snapshot: SNAPSHOT }, messageId: "owner-msg-2", phone: OWNER, ...f });
  assert.equal(f.sent.length, 1);
  assert.match(f.sent[0].content, /couldn't save that/);
});

test("the defect path still delivers customer work in the same result", async () => {
  const f = owner({ saveResult: { id: 5, status: "open" } });
  const outcomes = [];
  f.shopClient.dispatchOutbound = async (o) => { outcomes.push(o); return { recorded: true }; };
  delete process.env.AUTO_REPLIES_ENABLED;
  await handleIntakeResult({ result: { ownerQuestion: HEADLIGHT, snapshot: SNAPSHOT, autoReply: { key: "k", text: "t" } },
    messageId: "owner-msg-3", phone: OWNER, ...f });
  assert.deepEqual(outcomes, [{ key: "k", status: "skipped" }]);
});


test("two different ROs named: ask which, list both, nothing changed (QA on 6887684)", async () => {
  const twelve = { roId: 12, label: "RO #12", customer: "Dana Jeep", firstName: "Dana", vehicle: "2014 Jeep Compass", lines: [] };
  const focus = { how: "named_several", candidates: [RO8.ro, twelve] };
  assert.equal(whichJobReply(focus),
    "Which job do you mean: RO #8 (Antwon Test, 2009 Chevrolet Impala) or RO #12 (Dana Jeep, 2014 Jeep Compass)? Nothing was changed.");
  assert.equal(whichJobReply({ how: "named_several", candidates: [RO8.ro], missingRos: [404] }),
    "Did you mean RO #8 (Antwon Test, 2009 Chevrolet Impala)? I couldn't find RO #404. Nothing was changed.");

  const { quo, shopClient, sent, asked } = owner({ focus });
  await handleIntakeResult({ result: { ownerQuestion: "close out RO 8 or RO 12" }, messageId: "m-alt", phone: OWNER, quo, shopClient });
  assert.equal(sent.length, 1);
  assert.match(sent[0].content, /^Which job do you mean: RO #8 .* or RO #12 .*\? Nothing was changed\.$/);
  assert.deepEqual(asked, ["close out RO 8 or RO 12"]); // one deterministic lookup; the reply above is the only message
});
