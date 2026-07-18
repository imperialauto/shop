// ─────────────────────────────────────────────────────────
// Thin client for the shop app's internal service-to-service API
// (imperialauto/shop, app/routes/internal_messaging.py — the FastAPI
// "web" Railway service). This is how this Node service reads real
// customer / repair-order / invoice data and gets e-signature links,
// without owning a copy of that database.
//
// IMPORTANT: none of these calls ever send an SMS. They only fetch
// context or mint a signing link. The one and only path that sends a
// text to a customer remains POST /api/messages/conversations/:id/send
// in messagesApi.js, gated on a human clicking "Send" in the dashboard.
//
// Env:
//   SHOP_APP_API_URL   base URL of the "web" Railway service, e.g.
//                      https://web-production-94989.up.railway.app
//   INTERNAL_API_KEY   shared secret, must match the same var on "web"
// ─────────────────────────────────────────────────────────

const BASE_URL = process.env.SHOP_APP_API_URL;
const API_KEY = process.env.INTERNAL_API_KEY;

function configured() {
  return Boolean(BASE_URL && API_KEY);
}

async function request(path, options = {}) {
  if (!configured()) {
    throw new Error("shopClient: SHOP_APP_API_URL / INTERNAL_API_KEY not configured");
  }
  const res = await fetch(`${BASE_URL}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-Internal-Key": API_KEY,
      ...(options.headers || {}),
    },
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`shopClient: ${options.method || "GET"} ${path} -> ${res.status}: ${body.slice(0, 300)}`);
  }
  return res.json();
}

/**
 * Looks up a customer, their vehicle(s), open repair order, and open
 * invoice by phone number. Returns { found: false } if there's no match
 * (e.g. a lead who hasn't been entered as a customer yet) rather than
 * throwing, so callers can degrade gracefully.
 */
async function getContext(phone) {
  if (!configured() || !phone) return { found: false };
  try {
    return await request(`/internal/messaging/context?phone=${encodeURIComponent(phone)}`);
  } catch (err) {
    console.error("shopClient.getContext failed:", err);
    return { found: false };
  }
}

/**
 * Ensures the given repair order has a public signing link and returns
 * it plus shop-suggested SMS text. Does NOT send anything — the caller
 * stages this as a PENDING draft for human approval.
 */
async function getSigningLink(repairOrderId) {
  return request(`/internal/messaging/repair-orders/${repairOrderId}/signing-link`, { method: "POST" });
}

/**
 * Best-effort mirror of an SMS (either direction) into the shop app's
 * native Communication log, so it shows up on the customer's repair
 * order detail page without a separate screen. Never throws — a failure
 * here should never block message storage or the webhook ack.
 */
async function logCommunication({ phone, direction, body, externalId }) {
  if (!configured()) return { logged: false, reason: "not_configured" };
  try {
    return await request("/internal/messaging/communications", {
      method: "POST",
      body: JSON.stringify({ phone, direction, body, externalId }),
    });
  } catch (err) {
    console.error("shopClient.logCommunication failed:", err);
    return { logged: false, reason: "error" };
  }
}

module.exports = { configured, getContext, getSigningLink, logCommunication };
