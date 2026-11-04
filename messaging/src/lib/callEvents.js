// Real-time call events from Quo. An event is only a hint: the call ID is looked
// up in Quo, so nothing in the request body is trusted. Never texts a customer.

const crypto = require("crypto");
const { generateDraftReply } = require("./generateDraftReply");
const { lastConversationAt } = require("./callIntake");

const MISSED_DELAY_MS = 30000; // lets the voicemail transcript land first
const SEEN_LIMIT = 500;
const SPAM = /automated (\w+ ){0,3}(message|call|recording)|robocall|telemarket|business (loan|funding)|loan offer|extended warranty|google (business|listing)|business listing/i;

function tokenMatches(given, expected) {
  if (!given || !expected) return false;
  const a = Buffer.from(String(given));
  const b = Buffer.from(String(expected));
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

function parseEvent(body) {
  const data = body?.data || {};
  const resource = data.resource || data.object || {};
  return { id: body?.id, type: body?.type, callId: resource.callId || resource.id };
}

function textList(value) {
  if (Array.isArray(value)) return value.filter(Boolean).join(" ");
  return value ? String(value) : "";
}

function summaryBody(summary) {
  const fields = (summary.jobs || []).flatMap((job) => job.result?.data || [])
    .filter((row) => row.name && row.value != null && row.value !== "")
    .map((row) => `${row.name}: ${row.value}`);
  const parts = [textList(summary.summary), textList(summary.nextSteps) && `Next: ${textList(summary.nextSteps)}`, fields.join("; ")];
  return `AI answered a call. ${parts.filter(Boolean).join(" ")}`.slice(0, 3500);
}

function createCallEvents({ quo, shopClient, contacts, runPass, deliver, phoneNumberId, draft = generateDraftReply, schedule = setTimeout, ownNumber = process.env.QUO_DEFAULT_FROM_NUMBER, skip = new Set() }) {
  const seen = new Set();
  let running = false;
  let again = false;

  // A caller may have no text thread yet, which is fine: the alert still goes out.
  async function conversationFor(phone) {
    try {
      const { data = [] } = await quo.listMessages({ phoneNumberId, participants: [phone], maxResults: 1 });
      return data[0]?.conversationId || null;
    } catch (err) {
      console.error("Conversation lookup failed:", err.message);
      return null;
    }
  }

  async function pass() {
    if (running) {
      again = true;
      return;
    }
    running = true;
    try {
      do {
        again = false;
        await runPass();
        await deliver();
      } while (again);
    } catch (err) {
      console.error("Call event pass failed:", err.message);
    } finally {
      running = false;
    }
  }

  async function offerSummary(callId) {
    const call = await quo.getCall(callId);
    if (!call || call.direction !== "incoming") return;
    if (phoneNumberId && call.phoneNumberId && call.phoneNumberId !== phoneNumberId) return;
    if (!call.aiHandled) return; // calls that reached a person or voicemail have their own path
    const phone = (call.participants || []).find((p) => p !== ownNumber);
    if (!phone || skip.has(phone.replace(/\D/g, "").slice(-10))) return;
    const summary = await quo.getCallSummary(callId);
    if (!textList(summary?.summary) || SPAM.test(textList(summary.summary))) return;
    const conversationId = (await conversationFor(phone)) || `CALL${phone.replace(/\D/g, "")}`;
    const body = summaryBody(summary);
    const result = await shopClient.dispatchIntake({
      conversationId, messageId: `summary:${callId}`, phone, body, recovery: true, kind: "call",
      threadMessageId: "none", contactName: contacts ? await contacts.nameFor(phone) : null,
    });
    if (result.recorded && !result.owner) {
      const reply = await draft({ history: [{ direction: "INBOUND", body: `[${body}]` }], jobContext: { summary: body }, allowNoReply: true });
      if (reply) await shopClient.dispatchDraft({ conversationId, messageId: `summary:${callId}`, body: reply });
    }
    await deliver();
  }

  // A call you took or made counts as answering, the moment it ends.
  async function closeIfTalked(callId) {
    const call = await quo.getCall(callId);
    const at = call && lastConversationAt([call]);
    const phone = (call?.participants || []).find((p) => p !== ownNumber);
    if (!at || !phone) return;
    const conversationId = await conversationFor(phone);
    if (conversationId) await shopClient.dispatchHandled({ conversationId, at });
  }

  async function handle(body) {
    const event = parseEvent(body);
    if (!event.type || !event.callId) return;
    const key = event.id || `${event.type}:${event.callId}`;
    if (seen.has(key)) return;
    seen.add(key);
    if (seen.size > SEEN_LIMIT) seen.delete(seen.values().next().value);
    if (event.type === "call.summary.completed") return offerSummary(event.callId);
    if (event.type === "call.voicemail.completed") return pass();
    if (event.type === "call.completed") await closeIfTalked(event.callId);
    if (event.type === "call.missed" || event.type === "call.completed") {
      schedule(() => pass(), MISSED_DELAY_MS);
    }
  }

  return { handle };
}

async function ensureCallWebhook({ quo, url, phoneNumberId }) {
  const existing = await quo.listWebhooks();
  if (existing.some((hook) => hook.url === url)) return "exists";
  await quo.createCallWebhook({ url, phoneNumberId });
  return "created";
}

module.exports = { createCallEvents, ensureCallWebhook, parseEvent, summaryBody, tokenMatches };
