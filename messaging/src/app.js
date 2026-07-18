// ─────────────────────────────────────────────────────────
// Express app for the messaging module — runs as its OWN standalone
// Railway service (see prisma/schema.prisma for why: the shop dashboard
// at imperialauto/shop is Python/FastAPI, not Node, so this can't be
// require()'d into it). Wires up:
//   - POST /webhooks/quo         (raw body, no auth — Quo signs it itself)
//   - /api/messages/*            (JSON body, behind requireDashboardAuth)
//   - GET  /                     (the dashboard page, also behind auth)
//   - GET  /healthz              (uptime check for Railway — no auth)
// ─────────────────────────────────────────────────────────

const path = require("path");
const express = require("express");

const quoWebhookRouter = require("./routes/quoWebhook");
const messagesApiRouter = require("./routes/messagesApi");
const { requireDashboardAuth } = require("./lib/basicAuth");

/**
 * Mounts the messaging module's routes onto an Express app.
 * Order matters: the webhook route MUST be mounted before any global
 * express.json() middleware runs on its path, since it needs the raw
 * request body for signature verification.
 */
function mountMessagingRoutes(app) {
  // 1. Webhook — raw body, no auth (Quo signs the request instead).
  app.use("/webhooks/quo", quoWebhookRouter);

  // 2. The dashboard page + its API, behind HTTP Basic Auth (see
  // lib/basicAuth.js — set DASHBOARD_USERNAME/DASHBOARD_PASSWORD in env).
  app.use("/api/messages", requireDashboardAuth, express.json(), messagesApiRouter);
  app.use("/", requireDashboardAuth, express.static(path.join(__dirname, "..", "public")));
}

function createApp() {
  const app = express();

  app.get("/healthz", (req, res) => res.status(200).json({ ok: true }));

  mountMessagingRoutes(app);

  app.use((err, req, res, next) => {
    console.error("Unhandled error:", err);
    res.status(500).json({ error: "Internal server error" });
  });

  return app;
}

module.exports = { createApp, mountMessagingRoutes };
