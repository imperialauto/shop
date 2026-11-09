// End-to-end *synthetic* owner SMS through the real messaging client,
// FastAPI task API, SQLite DB, and owner dashboard. No Quo network calls.
const assert = require("node:assert/strict");
const { handleIntakeResult } = require("../messaging/src/lib/dispatchActions");
const shopClient = require("../messaging/src/lib/shopClient");

(async () => {
  const base = "http://127.0.0.1:18765";
  assert.equal(process.env.SHOP_APP_API_URL, base, "CI must use loopback only");
  assert.equal(process.env.INTERNAL_API_KEY, "local-integration-fixture-key");
  const fakeOwnerPhone = "+16025550199";
  const delivered = [];
  const quo = {
    listMessages: async () => ({ data: [] }),
    sendMessage: async ({ to, content }) => {
      assert.equal(to, fakeOwnerPhone, "never send a test SMS to a customer");
      delivered.push(content);
      return { id: "local-synthetic-send", status: "sent" };
    },
  };
  const snapshot = {
    waitingOnYou: [], otherOpenMessages: [], approvedAwaitingFollowUp: [],
  };
  const makeResult = async (ownerQuestion, messageId) =>
    handleIntakeResult({
      result: { ownerQuestion, snapshot },
      phone: fakeOwnerPhone, messageId, quo, shopClient,
    });

  const initial = await fetch(base + "/healthz").then(r => r.json());
  assert.equal(initial.mode, "active");

  // Owner SMS creates a persistent task through real internal HTTP.
  await makeResult("Add verify website booking flow to my to-do list", "synthetic-owner-msg-1");
  assert.match(delivered.at(-1), /Saved to Imperial tasks as #\d+/);
  const id = Number(delivered.at(-1).match(/#(\d+)/)[1]);

  // Replayed Quo webhook is idempotent (no second task).
  await makeResult("Add verify website booking flow to my to-do list", "synthetic-owner-msg-1");
  assert.match(delivered.at(-1), /Already captured; no duplicate/);

  // Owner SMS asks the same source of truth for the list.
  await makeResult("What's on my to-do list?", "synthetic-owner-msg-2");
  assert.match(delivered.at(-1), new RegExp("#" + id + " verify website booking flow"));
  assert.match(delivered.at(-1), /Shop communication queue/);

  const stored = await shopClient.openOwnerTasks();
  assert.equal(stored.tasks.length, 1);
  assert.equal(stored.tasks[0].id, id);

  // Real authenticated dashboard reads the saved task, not a second store.
  const login = await fetch(base + "/login", {
    method: "POST", redirect: "manual",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      username: "sms-fixture-owner", password: "synthetic-owner-password",
    }),
  });
  assert.ok([302, 303].includes(login.status), "fixture owner must log in");
  const cookie = login.headers.get("set-cookie")?.split(";")[0];
  assert.ok(cookie, "expected a real session cookie");
  const dashboard = await fetch(base + "/dashboard", {
    headers: { Cookie: cookie },
  });
  assert.equal(dashboard.status, 200);
  const html = await dashboard.text();
  assert.match(html, /Saved to-do tasks/);
  assert.match(html, /verify website booking flow/);

  // The task route must reject unauthorized calls even on this fake server.
  const unauthorized = await fetch(base + "/internal/imperial/tasks/open");
  assert.equal(unauthorized.status, 401);

  console.log("PASS: simulated owner SMS -> persistent task -> SMS lookup -> dashboard; dedupe and auth checked. No real SMS sent.");
})().catch(err => {
  console.error(err);
  process.exitCode = 1;
});
