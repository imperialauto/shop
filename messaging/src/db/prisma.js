// ─────────────────────────────────────────────────────────
// Prisma client singleton.
//
// If your Cowork dashboard already has one of these (most Express/Prisma
// apps do, usually at something like src/db/client.js or lib/prisma.js),
// DELETE this file and point the requires in quoWebhook.js / messagesApi.js
// / getJobContext.js / backfillQuoHistory.js at your existing one instead —
// having two PrismaClient instances against the same DATABASE_URL works
// but wastes connections.
//
// The `global` caching pattern below only matters for dev hot-reload
// (nodemon/ts-node-dev/etc. re-running this module without restarting the
// process would otherwise open a new connection pool every reload). In
// production (NODE_ENV=production) it's skipped and just returns a plain
// singleton per process.
// ─────────────────────────────────────────────────────────

const { PrismaClient } = require("@prisma/client");

const globalForPrisma = globalThis;

const prisma =
  globalForPrisma.__messagingModulePrisma ||
  new PrismaClient({
    log: process.env.NODE_ENV === "production" ? ["error", "warn"] : ["error", "warn", "query"],
  });

if (process.env.NODE_ENV !== "production") {
  globalForPrisma.__messagingModulePrisma = prisma;
}

module.exports = prisma;
