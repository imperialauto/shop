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

// Off until CONTACT_HOOK_TOKEN is set. A contact renamed in Quo renames the
// matching dashboard customer, live by webhook and again in an hourly catch-up.
// Never texts anyone and never writes to Quo.
if (process.env.CONTACT_HOOK_TOKEN && process.env.PUBLIC_URL) {
  const quo = require("./src/lib/quoClient");
  const { createContactEvents, ensureContactWebhook } = require("./src/lib/contactEvents");
  const skip = new Set((process.env.CONTACT_SYNC_SKIP || "").split(",").filter(Boolean).map((n) => n.replace(/\D/g, "").slice(-10)));
  const contactEvents = createContactEvents({ quo, shopClient: require("./src/lib/shopClient"), skip, writeBack: process.env.QUO_NAME_WRITEBACK === "1" });
  app.set("contactEvents", contactEvents);
  const catchUp = () => contactEvents.catchUp()
    .then((r) => console.log(`Quo contact catch-up: ${r.contacts} checked, ${r.renamed} renamed, ${r.renamedInQuo} renamed in Quo`))
    .catch((err) => console.error("Quo contact catch-up failed:", err.message));
  setTimeout(catchUp, 60 * 1000);
  setInterval(catchUp, 60 * 60 * 1000);
  ensureContactWebhook({ quo, url: `${process.env.PUBLIC_URL}/webhooks/quo-contacts/${process.env.CONTACT_HOOK_TOKEN}` })
    .then((state) => console.log(`Quo contact webhook ${state}`))
    .catch((err) => console.error(`Quo contact webhook setup failed: ${err.status || ""} ${err.code || ""} ${err.message || ""}`.trim()));
}

// Off unless explicitly enabled. Quo tasks <-> Imperial Master Tasks. Never sends a text.
if (process.env.QUO_TASK_SYNC_ENABLED === "true") {
  const { startQuoTaskSync } = require("./src/lib/quoTaskSync");
  startQuoTaskSync({ quo: require("./src/lib/quoClient"), shopClient: require("./src/lib/shopClient") });
}

// Run the same dispatch loop for owner notices or customer follow-up scheduling.
if (process.env.OWNER_TEXTS_ENABLED === "true" || process.env.CUSTOMER_FOLLOWUP_WORKER_ENABLED === "true"
    || process.env.BOOKING_CONFIRMATIONS_ENABLED === "true") {
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
