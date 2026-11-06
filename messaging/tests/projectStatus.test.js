const test = require("node:test");
const assert = require("node:assert/strict");
const { projectTopic, answerProjectQuestion, checkedState, listedBlockers } = require("../src/lib/projectStatus");

function fakeGitHub({ unavailable = false, checksUnavailable = false } = {}) {
  const calls = [];
  const fetchImpl = async (url, options) => {
    calls.push({ url, method: options.method || "GET" });
    if (unavailable) return { ok: false, status: 401 };
    const prNumber = Number(url.match(/\/pulls\/(\d+)/)?.[1]);
    if (prNumber) return {
      ok: true, json: async () => ({
        number: prNumber, state: "open", draft: prNumber === 55,
        merged_at: null, title: "Synthetic project", updated_at: "2026-11-06T19:00:00Z",
        head: { sha: "a".repeat(40) },
        body: "## Blockers\n- Need verified preview alerts\n- Test appointment flow\n",
      }),
    };
    if (url.includes("/check-runs")) {
      if (checksUnavailable) return { ok: false, status: 403 };
      return {
        ok: true, json: async () => ({
          check_runs: [
            { name: "Shop tests", status: "completed", conclusion: "success", completed_at: "2026-11-06T19:00:00Z" },
            { name: "Quo tests", status: "completed", conclusion: "success", completed_at: "2026-11-06T19:00:00Z" },
          ],
        }),
      };
    }
    throw Error("Unexpected mock URL: " + url);
  };
  return { fetchImpl, calls };
}

test("natural English maps website/dashboard/finance/all without commands", () => {
  assert.equal(projectTopic("What's the update on the website?"), "website");
  assert.equal(projectTopic("How's the dashboard coming?"), "dashboard");
  assert.equal(projectTopic("Are we ready to launch the finance module?"), "finance");
  assert.equal(projectTopic("Did Claude finish those fixes?"), "all");
  assert.equal(projectTopic("What did we get done on our project?"), "all");
  assert.equal(projectTopic("What about the bookings?", [{ content: "What's the update on the website?" }]), "website");
  assert.equal(projectTopic("What's holding it up?", [{ content: "Website: preview testing" }]), "website");
  assert.equal(projectTopic("What's holding it up?", [{ content: "Dashboard development" }]), "dashboard");
  assert.equal(projectTopic("What about my vehicle warranty?"), null);
  assert.equal(projectTopic("What time do we open?"), null);
  assert.equal(projectTopic("How much is Bobby's quote?"), null);
  assert.equal(projectTopic("Can you text a customer?"), null);
});

test("feature is disabled unless owner explicitly enables it", async () => {
  const { fetchImpl, calls } = fakeGitHub();
  await withFlag(undefined, async () => {
    const result = await answerProjectQuestion({ question: "Website update?", token: "fixture", fetchImpl });
    assert.equal(result, null);
  });
  assert.equal(calls.length, 0);
});

async function withFlag(value, action) {
  const old = process.env.PROJECT_STATUS_ENABLED;
  if (value === undefined) delete process.env.PROJECT_STATUS_ENABLED;
  else process.env.PROJECT_STATUS_ENABLED = value;
  try { return await action(); }
  finally {
    if (old === undefined) delete process.env.PROJECT_STATUS_ENABLED;
    else process.env.PROJECT_STATUS_ENABLED = old;
  }
}

test("fetches live read-only GitHub PR and checks; never claims production live", async () => {
  const { fetchImpl, calls } = fakeGitHub();
  await withFlag("true", async () => {
    const output = await answerProjectQuestion({
      question: "What's the update on the website?", token: "test-only", fetchImpl,
    });
    assert.match(output, /Website update/);
    assert.match(output, /PR #52: open; not merged/);
    assert.match(output, /Checks: 2 passing/);
    assert.match(output, /Railway\/live release not verified/);
    assert.match(output, /github.com\/imperialauto\/shop\/pull\/52/);
    assert.ok(output.length <= 900);
  });
  assert.equal(calls.length, 2);
  assert.ok(calls.every((item) => item.method === "GET" && item.url.startsWith("https://api.github.com/repos/imperialauto/shop/")));
});

test("the natural dashboard question selects dashboard PR, not site", async () => {
  const { fetchImpl, calls } = fakeGitHub();
  await withFlag("true", async () => {
    const answer = await answerProjectQuestion({ question: "How's the dashboard coming?", token: "fixture", fetchImpl });
    assert.match(answer, /PR #55: draft; not merged/);
    assert.ok(calls.every((entry) => entry.url.includes("/pulls/55") || entry.url.includes("/check-runs")));
  });
});

test("overall project answer includes all three workstreams and incomplete uncertainty", async () => {
  const { fetchImpl } = fakeGitHub({ checksUnavailable: true });
  await withFlag("true", async () => {
    const answer = await answerProjectQuestion({ question: "Did Claude finish the build?", token: "fixture", fetchImpl });
    assert.match(answer, /Website: PR #52/);
    assert.match(answer, /Dashboard \/ lead system: PR #55/);
    assert.match(answer, /Finance: PR #51/);
    assert.match(answer, /checks could not be verified/i);
    assert.match(answer, /does not confirm a production release/);
    assert.ok(answer.length <= 900);
  });
});

test("source credentials or GitHub outage produce transparent failure, not fake completion", async () => {
  const { fetchImpl } = fakeGitHub({ unavailable: true });
  await withFlag("true", async () => {
    const noToken = await answerProjectQuestion({ question: "Dashboard update?", token: "", fetchImpl });
    assert.match(noToken, /isn't connected/);
    const unavailable = await answerProjectQuestion({ question: "Dashboard update?", token: "fixture", fetchImpl });
    assert.match(unavailable, /couldn't verify/);
    assert.doesNotMatch(unavailable, /passed|deployed|complete!/);
  });
});

test("GitHub check errors remain separate from actual passing checks", () => {
  assert.equal(checkedState({}), "Checks not available");
  assert.match(checkedState({ check_runs: [
    { name: "suite", status: "completed", conclusion: "failure" },
    { name: "docs", status: "completed", conclusion: "success" },
  ] }), /1 failing, 1 passing/);
  assert.deepEqual(listedBlockers("## Blockers\n- Confirm a setting\n- Fix alerts\n## Next"), [
    "Confirm a setting", "Fix alerts",
  ]);
});
