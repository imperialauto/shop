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
const { tokenMatches } = require("./lib/callEvents");

/**
 * Mounts the messaging module's routes onto an Express app.
 * Order matters: the webhook route MUST be mounted before any global
 * express.json() middleware runs on its path, since it needs the raw
 * request body for signature verification.
 */
function mountMessagingRoutes(app) {
  // Private Operations bridge: scoped service token, no dashboard-password reuse.
  const { createOperationsBridge } = require("./routes/operationsBridge");
  app.use("/api/operations", express.json({ limit: "24kb" }), createOperationsBridge({
    prisma: require("./db/prisma"), quo: require("./lib/quoClient"),
    generateDraftReply: require("./lib/generateDraftReply").generateDraftReply,
    shopClient: require("./lib/shopClient"),
  }));

  // Quo call events: only a hint to look the call up, so the URL token is enough.
  app.post("/webhooks/quo-calls/:token", express.json({ limit: "256kb" }), (req, res) => {
    const events = req.app.get("callEvents");
    if (!events || !tokenMatches(req.params.token, process.env.CALL_HOOK_TOKEN)) return res.status(404).end();
    res.status(200).json({ received: true });
    events.handle(req.body).catch((err) => console.error("Call event failed:", err.message));
  });

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

