// ─────────────────────────────────────────────────────────
// Quo (formerly OpenPhone) REST API wrapper.
//
// Confirmed against the live Quo API docs (quo.com/docs) as of this
// writing:
//   - Base URL: https://api.quo.com
//   - Auth: `Authorization: <API_KEY>` header — NOT a Bearer token.
//   - POST /v1/messages          — send a text message
//   - GET  /v1/messages          — list messages (requires phoneNumberId
//                                  + participants[]; used by the backfill
//                                  script, one customer thread at a time)
//   - GET  /v1/conversations     — list conversations (used by backfill
//                                  to discover which threads exist)
//   - POST /v1/conversations/:id/mark-as-read
//   - POST /v1/conversations/:id/mark-as-done
//   - POST /v1/conversations/:id/mark-as-open
//
// Node 18+ has global fetch, so no extra HTTP dependency is needed.
// ─────────────────────────────────────────────────────────

const QUO_API_BASE = process.env.QUO_API_BASE_URL || "https://api.quo.com";

function authHeaders() {
  const apiKey = process.env.QUO_API_KEY;
  if (!apiKey) throw new Error("Missing QUO_API_KEY env var");
  return { Authorization: apiKey, "Content-Type": "application/json" };
}

async function quoFetch(path, options = {}) {
  const res = await fetch(`${QUO_API_BASE}${path}`, {
    ...options,
    headers: { ...authHeaders(), ...(options.headers || {}) },
  });
  const json = await res.json().catch(() => null);
  if (!res.ok) {
    const err = new Error(json?.message || `Quo API error ${res.status}`);
    err.status = res.status;
    err.code = json?.code;
    err.body = json;
    throw err;
  }
  return json;
}

/**
 * Send a text message via Quo.
 * @param {{to: string, from?: string, content: string, userId?: string, setInboxDone?: boolean}} args
 * @returns The created message: { id, to, from, text, phoneNumberId, conversationId, direction, userId, status, createdAt, updatedAt }
 */
async function sendMessage({ to, from, content, userId, setInboxDone }) {
  const resolvedFrom = from || process.env.QUO_DEFAULT_FROM_NUMBER;
  if (!resolvedFrom) throw new Error("No `from` number given and QUO_DEFAULT_FROM_NUMBER is not set");

  const body = {
    content,
    from: resolvedFrom,
    to: Array.isArray(to) ? to : [to],
  };
  const resolvedUserId = userId || process.env.QUO_DEFAULT_USER_ID;
  if (resolvedUserId) body.userId = resolvedUserId;
  if (setInboxDone) body.setInboxStatus = "done";

  const { data } = await quoFetch("/v1/messages", { method: "POST", body: JSON.stringify(body) });
  return data;
}

/**
 * List messages with a single customer (one phone number thread).
 * Requires the Quo phone number ID the messages were sent/received on —
 * see listPhoneNumbers below, or set QUO_PHONE_NUMBER_ID in env if the
 * workspace only has one line.
 */
async function listMessages({ phoneNumberId, participants, maxResults = 50, pageToken, createdAfter, createdBefore } = {}) {
  const resolvedPhoneNumberId = phoneNumberId || process.env.QUO_PHONE_NUMBER_ID;
  if (!resolvedPhoneNumberId) throw new Error("phoneNumberId is required (or set QUO_PHONE_NUMBER_ID)");
  if (!participants?.length) throw new Error("participants (at least one customer phone number) is required");

  const params = new URLSearchParams();
  params.set("phoneNumberId", resolvedPhoneNumberId);
  participants.forEach((p) => params.append("participants", p));
  params.set("maxResults", String(Math.min(maxResults, 100)));
  if (pageToken) params.set("pageToken", pageToken);
  if (createdAfter) params.set("createdAfter", createdAfter);
  if (createdBefore) params.set("createdBefore", createdBefore);

  return quoFetch(`/v1/messages?${params.toString()}`); // { data, totalItems, nextPageToken }
}

/**
 * List conversations across the workspace (or scoped to specific Quo
 * phone number IDs / numbers). Used by the backfill script to discover
 * which threads exist before pulling each one's full message history.
 */
async function listConversations({ phoneNumbers, maxResults = 50, pageToken, updatedAfter } = {}) {
  const params = new URLSearchParams();
  (phoneNumbers || []).forEach((p) => params.append("phoneNumbers", p));
  params.set("maxResults", String(Math.min(maxResults, 100)));
  if (pageToken) params.set("pageToken", pageToken);
  if (updatedAfter) params.set("updatedAfter", updatedAfter);

  return quoFetch(`/v1/conversations?${params.toString()}`); // { data, totalItems, nextPageToken }
}

/** List the Quo phone numbers on this workspace — handy for finding phoneNumberId once, by hand or in a setup script. */
async function listPhoneNumbers() {
  return quoFetch("/v1/phone-numbers");
}

async function markConversationRead(conversationId) {
  return quoFetch(`/v1/conversations/${conversationId}/mark-as-read`, { method: "POST" });
}

async function markConversationDone(conversationId) {
  return quoFetch(`/v1/conversations/${conversationId}/mark-as-done`, { method: "POST" });
}

async function markConversationOpen(conversationId) {
  return quoFetch(`/v1/conversations/${conversationId}/mark-as-open`, { method: "POST" });
}

module.exports = {
  sendMessage,
  listMessages,
  listConversations,
  listPhoneNumbers,
  markConversationRead,
  markConversationDone,
  markConversationOpen,
};
