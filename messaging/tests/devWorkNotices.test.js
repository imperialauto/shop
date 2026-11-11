const test = require("node:test");
const assert = require("node:assert/strict");
const { deliverOwnerMessages } = require("../src/lib/dispatchActions");

async function withFlags(vars, fn) {
  const old = Object.fromEntries(Object.keys(vars).map(k => [k, process.env[k]]));
  for (const [k, v] of Object.entries(vars)) v === null ? delete process.env[k] : process.env[k] = v;
  try { return await fn(); }
  finally { for (const [k, v] of Object.entries(old)) v === undefined ? delete process.env[k] : process.env[k] = v; }
}

test("development notice is disabled independently even when owner texts are enabled", async () => {
  await withFlags({ OWNER_TEXTS_ENABLED: "true", DEV_WORK_NOTICES_ENABLED: null }, async () => {
    const quo = { sendMessage: async () => { throw Error("must not send"); } };
    const shopClient = {
      dispatchTick: async () => ({ ownerPhone: "+16025550100", messages: [], customerReplies: [] }),
      claimDevelopmentNotice: async () => { throw Error("must not claim"); },
      ackDevelopmentNotice: async () => { throw Error("must not ack"); },
    };
    assert.deepEqual(await deliverOwnerMessages({ quo, shopClient }), { sent: 0 });
  });
});

test("claimed development update goes ONLY to the server-verified owner number", async () => {
  await withFlags({ OWNER_TEXTS_ENABLED: "true", DEV_WORK_NOTICES_ENABLED: "true" }, async () => {
    const sent = [], acks = [];
    const shopClient = {
      dispatchTick: async () => ({ ownerPhone: "+16025550100", messages: [], customerReplies: [] }),
      claimDevelopmentNotice: async () => ({ notice: { id: 8, claimNonce: "f".repeat(32),
        text: "Imperial development #5: review needed. Draft PR ready." } }),
      ackDevelopmentNotice: async (x) => { acks.push(x); return { recorded: true }; },
    };
    const quo = { sendMessage: async (x) => sent.push(x) };
    const result = await deliverOwnerMessages({ quo, shopClient });
    assert.deepEqual(result, { sent: 1 });
    assert.equal(sent.length, 1);
    assert.equal(sent[0].to, "+16025550100");
    assert.match(sent[0].content, /development #5/);
    assert.deepEqual(acks, [{ id: 8, claimNonce: "f".repeat(32), status: "sent" }]);
  });
});

test("uncertain Quo send is marked failed and never automatically retried", async () => {
  await withFlags({ OWNER_TEXTS_ENABLED: "true", DEV_WORK_NOTICES_ENABLED: "true" }, async () => {
    let claims = 0, attempts = 0, status = null;
    const shopClient = {
      dispatchTick: async () => ({ ownerPhone: "+16025550100", messages: [], customerReplies: [] }),
      claimDevelopmentNotice: async () => {
        claims++;
        return { notice: claims === 1 ? { id: 13, claimNonce: "a".repeat(32), text: "Blocked task" } : null };
      },
      ackDevelopmentNotice: async (x) => { status = x.status; return { recorded: true }; },
    };
    const quo = { sendMessage: async () => { attempts++; throw Error("simulated failure"); } };
    assert.equal((await deliverOwnerMessages({ quo, shopClient })).sent, 0);
    assert.equal(status, "failed");
    assert.equal((await deliverOwnerMessages({ quo, shopClient })).sent, 0);
    assert.equal(attempts, 1);
  });
});

test("missing configured owner does not claim or send any development notice", async () => {
  await withFlags({ OWNER_TEXTS_ENABLED: "true", DEV_WORK_NOTICES_ENABLED: "true" }, async () => {
    const shopClient = {
      dispatchTick: async () => ({ ownerPhone: null, messages: [], customerReplies: [] }),
      claimDevelopmentNotice: async () => { throw Error("must not claim"); },
    };
    assert.equal((await deliverOwnerMessages({
      quo: { sendMessage: async () => { throw Error("must not send"); } }, shopClient,
    })).sent, 0);
  });
});
