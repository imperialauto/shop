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
