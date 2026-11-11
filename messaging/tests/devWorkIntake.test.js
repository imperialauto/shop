const test = require("node:test");
const assert = require("node:assert/strict");
const {
  answerImperialBrain, developmentWorkIntent, developmentWorkStatusIntent,
} = require("../src/lib/imperialBrain");

async function withFlags(vars, run) {
  const original = Object.fromEntries(Object.keys(vars).map(key => [key, process.env[key]]));
  for (const [k,v] of Object.entries(vars)) v === null ? delete process.env[k] : process.env[k] = v;
  try { return await run(); }
  finally { for (const [k,v] of Object.entries(original)) v === undefined ? delete process.env[k] : process.env[k] = v; }
}

test("explicit continue-work requests are scoped; casual language is not", () => {
  assert.equal(developmentWorkIntent("Imperial, continue working on the website").projectKey, "website");
  assert.equal(developmentWorkIntent("Resume work on financial reconciliation").projectKey, "finance");
  assert.equal(developmentWorkIntent("start working on the Operations dashboard").projectKey, "operations");
  assert.equal(developmentWorkIntent("continue working on integrations").projectKey, "integrations");
  assert.equal(developmentWorkIntent("Continue working on something").projectKey, null);
  assert.equal(developmentWorkIntent("Why isn't the website working?"), null);
  assert.equal(developmentWorkIntent("Tell the customer to wait"), null);
  assert.ok(developmentWorkStatusIntent("What's the development work status?"));
});

test("disabled development queue never calls writer or pretends to start a worker", async () => {
  await withFlags({IMPERIAL_BRAIN_ENABLED:"true", DEV_COORDINATOR_CAPTURE_ENABLED:null}, async () => {
    const reply = await answerImperialBrain({
      question:"Continue working on the website", messageId:"owner-fixture",
      developmentWriter: async()=>{ throw Error("must not call"); },
    });
    assert.match(reply, /not activated/);
    assert.match(reply, /NOT started or queued/);
  });
});

test("owner development command queues exactly once per request and reports no worker", async () => {
  let writes = 0;
  await withFlags({IMPERIAL_BRAIN_ENABLED:"true", DEV_COORDINATOR_CAPTURE_ENABLED:"true"}, async () => {
    const writer = async ({messageId, projectKey, instruction}) => {
      writes++;
      assert.equal(messageId, "owner-message-fixture");
      assert.equal(projectKey, "website");
      assert.match(instruction, /website/);
      return {recorded:true, duplicate:writes>1, id:73, projectKey, status:"queued",workerConnected:false, executionStarted:false};
    };
    const call = () => answerImperialBrain({
      question:"Imperial, continue working on the website", messageId:"owner-message-fixture",
      developmentWriter:writer,
    });
    const first=await call(), again=await call();
    assert.match(first, /Saved development job #73/);
    assert.match(first, /no AI coding run started/);
    assert.match(again, /Already saved development job #73/);
    assert.equal(writes,2);
  });
});

test("unscoped, unauthorized or failed job requests never report completion", async () => {
  await withFlags({IMPERIAL_BRAIN_ENABLED:"true", DEV_COORDINATOR_CAPTURE_ENABLED:"true"}, async () => {
    const missingProject=await answerImperialBrain({
      question:"Continue working on something", messageId:"dev-fixture",
      developmentWriter:async()=>{throw Error("must not call");},
    });
    assert.match(missingProject, /Which project/);
    const missingId=await answerImperialBrain({
      question:"Continue working on the website",
      developmentWriter:async()=>{throw Error("must not call");},
    });
    assert.match(missingId, /Nothing was confirmed/);
    const notSaved=await answerImperialBrain({
      question:"Continue working on the website", messageId:"owner-fixture",
      developmentWriter:async()=>({recorded:false,id:101}),
    });
    assert.match(notSaved, /couldn't confirm/);
    const review=await answerImperialBrain({
      question:"Continue working on the website and deploy it",messageId:"owner-fixture",
      developmentWriter:async()=>({recorded:true,duplicate:false,id:101,projectKey:"website",status:"needs_approval"}),
    });
    assert.match(review, /Needs owner approval/);
  });
});

test("development job status distinguishes saved items from active executions", async () => {
  await withFlags({IMPERIAL_BRAIN_ENABLED:"true", DEV_COORDINATOR_CAPTURE_ENABLED:"true"}, async () => {
    const reply=await answerImperialBrain({
      question:"What's the development work status?",
      developmentLister:async()=>({jobs:[{id:19,projectKey:"website",status:"queued"}],workerConnected:false}),
    });
    assert.match(reply, /no active execution confirmed/);
    assert.match(reply, /#19 website: queued/);
    assert.match(reply, /not activated/);
    const missing=await answerImperialBrain({question:"What's the development work status?"});
    assert.match(missing, /not connected/);
  });
});

test("configured free worker status is accurate for claimed and review-needed jobs", async () => {
  await withFlags({IMPERIAL_BRAIN_ENABLED:"true", DEV_COORDINATOR_CAPTURE_ENABLED:"true"}, async () => {
    const running = await answerImperialBrain({
      question:"What's the development work status?",
      developmentLister:async()=>({
        workerConfigured:true,
        jobs:[{id:42, projectKey:"website", status:"running"}],
      }),
    });
    assert.match(running, /running job claimed/);
    assert.match(running, /#42 website: running/);
    assert.doesNotMatch(running, /not activated/);
    const review = await answerImperialBrain({
      question:"What's the development work status?",
      developmentLister:async()=>({
        workerConfigured:true,
        jobs:[{id:42, projectKey:"website", status:"review_needed",
          resultUrl:"https://github.com/imperialauto/shop/pull/1234"}],
      }),
    });
    assert.match(review, /no active execution confirmed/);
    assert.match(review, /review_needed/);
    assert.match(review, /github.com\/imperialauto\/shop\/pull\/1234/);
    const save = await answerImperialBrain({
      question:"Imperial, continue working on the website", messageId:"owner-200",
      developmentWriter:async()=>({recorded:true,id:43,projectKey:"website",
        status:"queued",workerConfigured:true}),
    });
    assert.match(save,/Queued; no AI coding run started/);
    assert.match(save,/Free worker configured/);
  });
});
