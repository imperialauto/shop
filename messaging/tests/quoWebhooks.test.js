const test = require("node:test");
const assert = require("node:assert/strict");

test("listWebhooks sees hooks made with the 2026-03-30 API, so restarts don't duplicate them", async () => {
  process.env.QUO_API_KEY = "test-key";
  const realFetch = global.fetch;
  const calls = [];
  global.fetch = async (url, opts) => {
    calls.push([url, opts.headers["Quo-Api-Version"] || null]);
    const versioned = url.endsWith("/webhooks") && !url.includes("/v1/");
    const data = versioned ? [{ id: "7", url: "https://x/hook" }] : [];
    return { ok: true, json: async () => ({ data }) };
  };
  try {
    const { listWebhooks } = require("../src/lib/quoClient");
    const { ensureCallWebhook } = require("../src/lib/callEvents");
    const hooks = await listWebhooks();
    assert.deepEqual(hooks.map((h) => h.url), ["https://x/hook"]);
    assert.ok(calls.some(([u, v]) => u.endsWith("/webhooks") && v === "2026-03-30"));
    let created = 0;
    const quo = { listWebhooks, createCallWebhook: async () => { created += 1; } };
    assert.equal(await ensureCallWebhook({ quo, url: "https://x/hook" }), "exists");
    assert.equal(created, 0);
  } finally {
    global.fetch = realFetch;
  }
});
