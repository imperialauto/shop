// Entry point for this standalone Railway service.

require("dotenv").config();
const { createApp } = require("./src/app"); // was "./app" — app.js actually lives in src/, that path never resolved

const REQUIRED_ENV_VARS = [
  "QUO_API_KEY",
  "QUO_DEFAULT_FROM_NUMBER",
  "QUO_WEBHOOK_SIGNING_SECRET",
  "GROQ_API_KEY",
  "DATABASE_URL",
  "DASHBOARD_USERNAME",
  "DASHBOARD_PASSWORD",
];

const missing = REQUIRED_ENV_VARS.filter((name) => !process.env[name]);
if (missing.length) {
  console.warn(`Warning: missing env vars: ${missing.join(", ")} — related features will fail at runtime.`);
}

const app = createApp();
const port = process.env.PORT || 3000;

app.listen(port, () => {
  console.log(`Messaging module listening on port ${port}`);
});

// Off unless explicitly enabled. Reads Quo and records work; never sends.
if (process.env.DISPATCH_RECOVERY_ENABLED === "true") {
  const quo = require("./src/lib/quoClient");
  const shopClient = require("./src/lib/shopClient");
  const { startRecovery } = require("./src/lib/dispatchRecovery");
  const { createContactLookup } = require("./src/lib/quoContacts");
  const options = { quo, shopClient, contacts: createContactLookup({ quo }), phoneNumberId: process.env.QUO_PHONE_NUMBER_ID,
    skip: new Set((process.env.CONTACT_SYNC_SKIP || "").split(",").filter(Boolean).map((n) => n.replace(/\D/g, "").slice(-10))) };
  startRecovery(options);

  // Real-time call alerts. Timed recovery above stays on as the safety net.
  if (process.env.CALL_HOOK_TOKEN && process.env.PUBLIC_URL) {
    const { runRecovery } = require("./src/lib/dispatchRecovery");
    const { deliverOwnerMessages } = require("./src/lib/dispatchActions");
    const { createCallEvents, ensureCallWebhook } = require("./src/lib/callEvents");
    app.set("callEvents", createCallEvents({
      ...options,
      runPass: () => runRecovery(options),
      deliver: () => deliverOwnerMessages({ quo, shopClient }),
    }));
    ensureCallWebhook({ quo, url: `${process.env.PUBLIC_URL}/webhooks/quo-calls/${process.env.CALL_HOOK_TOKEN}`, phoneNumberId: options.phoneNumberId })
      .then((state) => console.log(`Quo call webhook ${state}`))
      .catch((err) => console.error(`Quo call webhook setup failed: ${err.status || ""} ${err.code || ""} ${err.message || ""}`.trim()));
  }
}

// Off unless explicitly enabled. Approvals, nudges and the morning summary.
if (process.env.OWNER_TEXTS_ENABLED === "true") {
  const quo = require("./src/lib/quoClient");
  const shopClient = require("./src/lib/shopClient");
  const { deliverOwnerMessages } = require("./src/lib/dispatchActions");
  const tick = () => deliverOwnerMessages({ quo, shopClient }).catch((err) => console.error("Owner texts failed:", err.message));
  tick();
  setInterval(tick, 5 * 60 * 1000);
}

// Off unless explicitly enabled. Logs drafts for made-up conversations; sends nothing.
if (process.env.DRAFT_SELF_CHECK === "true") {
  require("./src/lib/draftSelfCheck").runDraftSelfCheck().catch((err) => console.error("Draft check failed:", err.message));
}
