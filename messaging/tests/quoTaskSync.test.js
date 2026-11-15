const { test } = require("node:test");
const assert = require("node:assert/strict");
const { runQuoTaskSync } = require("../src/lib/quoTaskSync");

test("reads every page, posts tasks, completes what Imperial finished, never texts", async () => {
  const calls = { completed: [], acked: null, posted: null, sends: 0 };
  const quo = {
    listTasks: async ({ pageToken }) => pageToken
      ? { data: [{ id: "TK2", title: "[CALL BACK] b", status: "open" }] }
      : { data: [{ id: "TK1", title: "[REPLY DRAFT] a", status: "open" }], nextPageToken: "p2" },
    completeTask: async (id) => { if (id === "TKgone") { const e = new Error("nf"); e.status = 404; throw e; } calls.completed.push(id); },
    sendMessage: async () => { calls.sends += 1; },
  };
  const shopClient = {
    syncQuoTasks: async (tasks) => { calls.posted = tasks; return { added: 2, closed: 0, complete_in_quo: ["TK1", "TKgone"] }; },
    ackQuoTasks: async (ids) => { calls.acked = ids; },
  };
  const summary = await runQuoTaskSync({ quo, shopClient, pauseMs: 0 });
  assert.equal(calls.posted.length, 2);
  assert.deepEqual(calls.completed, ["TK1"]);
  assert.deepEqual(calls.acked, ["TK1", "TKgone"]);
  assert.equal(calls.sends, 0);
  assert.equal(summary.completedInQuo, 2);
});

test("a failed Quo completion is not acknowledged, so it is retried next time", async () => {
  let acked = null;
  const quo = {
    listTasks: async () => ({ data: [] }),
    completeTask: async () => { const e = new Error("busy"); e.status = 429; throw e; },
  };
  const shopClient = { syncQuoTasks: async () => ({ complete_in_quo: ["TK9"] }), ackQuoTasks: async (ids) => { acked = ids; } };
  await runQuoTaskSync({ quo, shopClient, pauseMs: 0 });
  assert.equal(acked, null);
});
