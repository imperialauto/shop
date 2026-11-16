// Catches inbound messages the webhook missed by reading recent Quo history
// and re-offering them to the Operations queue. The queue ignores any message
// it already has, so repeats are safe. This module never sends a message.

const { lastConversationAt, missedCall, callContext } = require("./callIntake");
const { generateDraftReply } = require("./generateDraftReply");

const SOURCE = "quo_recovery";
const DEFAULT_LOOKBACK_HOURS = 6;
const DEFAULT_INTERVAL_MINUTES = 15;

function isInbound(message) {
  return /^(in|inbound|incoming)$/i.test(message.direction || "");
}

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function offerMissedCall({ quo, shopClient, contacts, phoneNumberId, conversation, since, messages, totals, draft, skip }) {
  const phone = conversation.participants?.[0];
  if (!phone) return;
  const call = await missedCall({ quo, phone, phoneNumberId, since, messages, skip });
  if (!call) return;
  const newest = [...messages].sort((a, b) => new Date(b.createdAt) - new Date(a.createdAt))[0];
  const result = await shopClient.dispatchIntake({
    conversationId: conversation.id,
    messageId: call.id,
    phone,
    body: call.body,
    recovery: true,
    kind: "call",
    callOutcome: call.body.startsWith("Voicemail:") || call.body.startsWith("Left a voicemail") ? "voicemail" : "missed",
    callAt: call.at,
    threadMessageId: newest?.id || "none",
    contactName: contacts ? await contacts.nameFor(phone) : null,
  });
  if (!result.recorded || result.owner) return;
  totals.calls += 1;
  if (contacts && process.env.CONTACT_SYNC_ENABLED === "true") {
    await contacts.syncContact({ phone, vehicleText: result.vehicleText, category: result.category })
      .catch((err) => console.error("Quo contact sync failed:", err.message));
  }
  const history = [...messages].reverse().map((m) => ({
    direction: isInbound(m) ? "INBOUND" : "OUTBOUND",
    body: m.text || m.body || "",
  }));
  history.push({ direction: "INBOUND", body: `[${call.body}]` });
  const summary = await callContext({ quo, phone, phoneNumberId });
  const body = await draft({ history, jobContext: summary ? { summary } : null, allowNoReply: true });
  if (body) await shopClient.dispatchDraft({ conversationId: conversation.id, messageId: call.id, body });
}

function mirrorBody(message) {
  const text = message.text || message.body || "";
  const photos = (message.media || []).length;
  if (!photos) return text;
  return `${text}${text ? " " : ""}[${photos} photo${photos === 1 ? "" : "s"}]`;
}

async function mirrorMessages({ shopClient, phone, messages, totals }) {
  if (!phone || typeof shopClient.logCommunication !== "function") return;
  for (const message of messages) {
    const body = mirrorBody(message);
    if (!body || !message.id) continue;
    const result = await shopClient.logCommunication({
      phone, direction: isInbound(message) ? "inbound" : "outbound", body, externalId: message.id, createdAt: message.createdAt,
    }).catch(() => null);
    if (result && result.logged && !result.duplicate) totals.mirrored += 1;
  }
}

// Drafts for customer messages the webhook path never drafted. Saves drafts only;
// a failure is recorded so the dashboard card shows it and the owner isn't left guessing.
async function draftWaiting({ quo, shopClient, phoneNumberId, draft, totals, pauseMs }) {
  if (typeof shopClient.draftsNeeded !== "function") return;
  const { items = [] } = await shopClient.draftsNeeded(5).catch(() => ({ items: [] }));
  for (const item of items) {
    await pause(pauseMs);
    let body = null;
    let reason = "";
    try {
      const { data: messages = [] } = await quo.listMessages({ phoneNumberId, participants: [item.phone], maxResults: 15 });
      const history = [...messages].sort((a, b) => new Date(a.createdAt) - new Date(b.createdAt)).map((m) => ({
        direction: isInbound(m) ? "INBOUND" : "OUTBOUND", body: m.text || m.body || (m.media?.length ? "[photo]" : ""),
      }));
      const summary = await callContext({ quo, phone: item.phone, phoneNumberId }).catch(() => null);
      body = await draft({ history, jobContext: summary ? { summary } : null, allowNoReply: true });
      if (!body) reason = "empty draft";
    } catch (err) {
      reason = String(err.message || err).slice(0, 120);
    }
    const saved = body
      ? await shopClient.dispatchDraft({ conversationId: item.conversationId, messageId: item.messageId, body }).catch(() => null)
      : null;
    if (saved && saved.recorded) {
      totals.drafted += 1;
    } else {
      totals.draftFailed += 1;
      await shopClient.draftFailed({ conversationId: item.conversationId, messageId: item.messageId,
        reason: reason || "draft not accepted" });
    }
  }
}

async function runRecovery({ quo, shopClient, contacts, phoneNumberId, now = Date.now, lookbackHours = DEFAULT_LOOKBACK_HOURS, pauseMs = 300, draft = generateDraftReply, skip = new Set() }) {
  const since = new Date(now() - lookbackHours * 3600 * 1000).toISOString();
  const totals = { conversations: 0, offered: 0, recorded: 0, failed: 0, calls: 0, mirrored: 0, drafted: 0, draftFailed: 0 };
  try {
    const { data: conversations = [] } = await quo.listConversations({ updatedAfter: since });
    for (const conversation of conversations) {
      totals.conversations += 1;
      await pause(pauseMs); // Quo rate-limits bursts, which would also block customer texts
      const { data: messages = [] } = await quo.listMessages({
        phoneNumberId,
        participants: conversation.participants,
        createdAfter: since,
      });
      const latest = [...messages].sort((a, b) => new Date(b.createdAt) - new Date(a.createdAt))[0];
      const callPhone = conversation.participants?.[0];
      // Every text, both directions, into the customer's history. Idempotent on the Quo
      // message ID with its original time, so repeats and webhook overlap add nothing.
      await mirrorMessages({ shopClient, phone: callPhone, messages, totals });
      if (latest && !isInbound(latest)) {
        await shopClient.dispatchHandled({ conversationId: conversation.id, at: latest.createdAt, phone: callPhone });
      }
        if (callPhone) {
        const { data: calls = [] } = await quo.listCalls({ phoneNumberId, participants: [callPhone], createdAfter: since, maxResults: 10 });
        const talkedAt = lastConversationAt(calls);
        if (talkedAt) await shopClient.dispatchHandled({ conversationId: conversation.id, at: talkedAt, phone: callPhone, byCall: true });
      }
      await offerMissedCall({ quo, shopClient, contacts, phoneNumberId, conversation, since, messages, totals, draft, skip });
      for (const message of messages.filter(isInbound)) {
        totals.offered += 1;
        const phone = message.from || conversation.participants?.[0];
        const result = await shopClient.dispatchIntake({
          conversationId: conversation.id,
          messageId: message.id,
          phone,
          contactName: contacts ? await contacts.nameFor(phone) : null,
          body: message.text || message.body || (message.media?.length ? "[Customer sent a photo]" : ""),
          recovery: true,
        });
        if (result.recorded) totals.recorded += 1;
        else if (!result.duplicate) totals.failed += 1;
      }
    }
    await draftWaiting({ quo, shopClient, phoneNumberId, draft, totals, pauseMs });
  } catch (err) {
    totals.failed += 1;
    console.error("Dispatch recovery failed:", err.message);
  }
  const detail = `checked ${totals.conversations}, recovered ${totals.recorded}, calls ${totals.calls}, `
    + `mirrored ${totals.mirrored}, drafted ${totals.drafted}, draft failures ${totals.draftFailed}, failed ${totals.failed}`;
  await shopClient.dispatchSync({ source: SOURCE, ok: totals.failed === 0, detail });
  return totals;
}

function startRecovery(options) {
  const minutes = Number(process.env.DISPATCH_RECOVERY_MINUTES) || DEFAULT_INTERVAL_MINUTES;
  const tick = () => runRecovery(options).catch((err) => console.error("Dispatch recovery crashed:", err.message));
  tick();
  return setInterval(tick, minutes * 60 * 1000);
}

module.exports = { runRecovery, startRecovery };
