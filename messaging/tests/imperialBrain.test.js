const test = require("node:test");
const assert = require("node:assert/strict");
const { answerImperialBrain, shopTaskSummary, shopTasksIntent,
  developmentTasksIntent, taskWriteIntent } = require("../src/lib/imperialBrain");
const { answerDevelopmentTasks } = require("../src/lib/projectStatus");
const { capabilityIntent } = require("../src/lib/capabilityRegistry");

const snapshot = {
  waitingOnYou: [
    { id: 5, who: "Test Fusion", urgency: "normal", nextStep: "Verify shop slot" },
    { id: 9, who: "Test U-Haul roadside", urgency: "urgent", nextStep: "Call about no-start" },
  ],
  otherOpenMessages: [{ id: 12, who: "Test Altima", urgency: "normal", nextStep: "Review oil change" }],
  approvedAwaitingFollowUp: [{ id: 14, who: "Test Explorer", urgency: "normal", nextStep: "Confirm estimate" }],
};

async function withFlags(vars, task) {
  const old = Object.fromEntries(Object.keys(vars).map(k => [k, process.env[k]]));
  for (const [k, v] of Object.entries(vars)) v === null ? delete process.env[k] : process.env[k] = v;
  try { return await task(); }
  finally {
    for (const [k, v] of Object.entries(old)) v === undefined ? delete process.env[k] : process.env[k] = v;
  }
}

test("no generic brain routing until explicitly enabled", async () => {
  await withFlags({ IMPERIAL_BRAIN_ENABLED: null }, async () => {
    assert.equal(await answerImperialBrain({ question: "What do I need to do today?", snapshot }), null);
  });
});

test("plain-English shop task summary uses real snapshot and orders urgent first", async () => {
  await withFlags({ IMPERIAL_BRAIN_ENABLED: "true" }, async () => {
    const answer = await answerImperialBrain({ question: "What do I need to do today?", snapshot });
    assert.match(answer, /Needs owner: 2; inbox: 1; approved follow-ups: 1/);
    assert.ok(answer.indexOf("#9 Test U-Haul") < answer.indexOf("#5 Test Fusion"));
    assert.match(answer, /not the full Quo, project or finance task list/);
    assert.ok(answer.length <= 900);
  });
});

test("missing snapshot does not hallucinate open tasks or deadlines", () => {
  assert.match(shopTaskSummary({}), /can't verify your open shop tasks/);
  assert.equal(shopTasksIntent("What's going on with my shop tasks?"), true);
  assert.equal(developmentTasksIntent("What's left on the website checklist?"), true);
  assert.equal(taskWriteIntent("Add test the transmission to my to-do list"), true);
});

test("specific project questions and follow-ups use project adapter with no actions", async () => {
  let called = 0;
  await withFlags({ IMPERIAL_BRAIN_ENABLED: "true" }, async () => {
    const answer = await answerImperialBrain({
      question: "What's the update on the website?", snapshot, projectAnswer: async (arg) => {
        called++; assert.equal(arg.question, "What's the update on the website?");
        return "PR #52: open, not live";
      },
    });
    assert.equal(answer, "PR #52: open, not live");
    assert.equal(called, 1);
  });
});

test("developer tasks fetch GitHub issues, not the shop queue", async () => {
  await withFlags({ IMPERIAL_BRAIN_ENABLED: "true" }, async () => {
    const answer = await answerImperialBrain({
      question: "What development tasks are left?", snapshot,
      developmentAnswer: async ({ question }) => {
        assert.match(question, /development tasks/);
        return "Issue #54 open";
      },
    });
    assert.equal(answer, "Issue #54 open");
  });
});

test("plain-English internal task save requires verified record ID and is idempotent", async () => {
  let writes = 0;
  await withFlags({ IMPERIAL_BRAIN_ENABLED: "true", IMPERIAL_TASKS_ENABLED: "true" }, async () => {
    const writer = async ({ messageId, title, projectKey }) => {
      writes++;
      assert.equal(messageId, "owner-message-1");
      assert.equal(title, "verify website booking confirmation");
      assert.equal(projectKey, "website");
      return { recorded: true, duplicate: writes > 1, id: 44, title };
    };
    const call = () => answerImperialBrain({
      question: "Add verify website booking confirmation to my to-do list",
      snapshot, messageId: "owner-message-1", taskWriter: writer,
    });
    assert.match(await call(), /Saved to Imperial tasks as #44/);
    assert.match(await call(), /Already captured; no duplicate/);
    assert.equal(writes, 2);
  });
});

test("cannot falsely confirm a task when its write failed or feature is off", async () => {
  await withFlags({ IMPERIAL_BRAIN_ENABLED: "true", IMPERIAL_TASKS_ENABLED: "true" }, async () => {
    const answer = await answerImperialBrain({
      question: "Create a task to check shop printer ink", snapshot, messageId: "owner-msg",
      taskWriter: async () => ({ recorded: false }),
    });
    assert.match(answer, /couldn't verify/);
  });
  await withFlags({ IMPERIAL_BRAIN_ENABLED: "true", IMPERIAL_TASKS_ENABLED: null }, async () => {
    const answer = await answerImperialBrain({
      question: "Add check shop printer ink to my list", snapshot, messageId: "owner-msg",
      taskWriter: async () => { throw Error("Must not run"); },
    });
    assert.match(answer, /Nothing was saved/);
  });
});

test("saved owner to-do list stays distinct from incomplete communication task snapshot", async () => {
  await withFlags({ IMPERIAL_BRAIN_ENABLED: "true", IMPERIAL_TASKS_ENABLED: "true" }, async () => {
    const answer = await answerImperialBrain({
      question: "What's on my to-do list?", snapshot,
      taskLister: async () => ({ tasks: [{ id: 21, title: "Order test shop parts", status: "open" }] }),
    });
    assert.match(answer, /Saved Imperial tasks:/);
    assert.match(answer, /#21 Order test shop parts/);
    assert.match(answer, /Shop communication queue:/);
  });
});

test("a task assignment cannot be claimed as done without a task writer", async () => {
  await withFlags({ IMPERIAL_BRAIN_ENABLED: "true" }, async () => {
    const answer = await answerImperialBrain({ question: "Have my mother handle that estimate", snapshot });
    assert.match(answer, /haven't created or assigned anything/);
  });
});

test("provider access questions identify missing adapters, not invented access", async () => {
  await withFlags({ IMPERIAL_BRAIN_ENABLED: "true", PROJECT_STATUS_GITHUB_TOKEN: null }, async () => {
    assert.equal(capabilityIntent("Can you access ALLDATA?"), "alldata");
    const answer = await answerImperialBrain({ question: "Can you access ALLDATA?", snapshot });
    assert.match(answer, /No verified ALLDATA adapter/);
    const github = await answerImperialBrain({ question: "Can you access GitHub?", snapshot });
    assert.match(github, /credential/);
    const connected = await answerImperialBrain({ question: "Which systems can you access?", snapshot });
    assert.match(connected, /Quo owner texts/);
    assert.match(connected, /Shop:/);
  });
});

test("shop question is not silently routed to GitHub project-status", async () => {
  await withFlags({ IMPERIAL_BRAIN_ENABLED: "true" }, async () => {
    const answer = await answerImperialBrain({
      question: "Which customers need follow-up today?", snapshot,
      projectAnswer: async () => { throw Error("Project adapter should not run"); },
    });
    assert.match(answer, /Test U-Haul/);
  });
});

test("GitHub issues read excludes pull requests, ranks P0 and reports first-page coverage", async () => {
  const captured = [];
  const fetchImpl = async (url, opts) => {
    captured.push({ url, method: opts.method || "GET" });
    return { ok: true, json: async () => [
      { number: 44, title: "P2: Optional docs", updated_at: "2026-11-06T18:00:00Z" },
      { number: 53, title: "PR-shaped object", pull_request: { url: "https://x" } },
      { number: 56, title: "P1: Imperial owner updates", updated_at: "2026-11-06T17:00:00Z" },
      { number: 54, title: "P0: Unified Sona lead intake", updated_at: "2026-11-06T16:00:00Z" },
    ]};
  };
  await withFlags({ PROJECT_STATUS_ENABLED: "true" }, async () => {
    const reply = await answerDevelopmentTasks({ question: "What development tasks are open?", token: "fixture", fetchImpl });
    assert.match(reply, /3 open GitHub issues/);
    assert.ok(reply.indexOf("#54") < reply.indexOf("#56"));
    assert.doesNotMatch(reply, /PR-shaped object/);
    assert.match(reply, /not Quo customer tasks/);
  });
  assert.equal(captured.length, 1);
  assert.match(captured[0].url, /issues\?state=open/);
  assert.equal(captured[0].method, "GET");
});

test("GitHub issue read failure is explicit, not a fake task list", async () => {
  await withFlags({ PROJECT_STATUS_ENABLED: "true" }, async () => {
    const missing = await answerDevelopmentTasks({ token: "", question: "Project tasks?" });
    assert.match(missing, /read-only GitHub credential/);
    const failed = await answerDevelopmentTasks({
      token: "fixture", question: "Project tasks?",
      fetchImpl: async () => ({ ok: false, status: 403 }),
    });
    assert.match(failed, /couldn't verify/);
    assert.doesNotMatch(failed, /3 open|complete/);
  });
});
