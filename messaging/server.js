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
  startRecovery({ quo, shopClient, contacts: createContactLookup({ quo }), phoneNumberId: process.env.QUO_PHONE_NUMBER_ID });
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
