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
  const body = await draft({ history, jobContext: summary ? { summary } : null });
  if (body) await shopClient.dispatchDraft({ conversationId: conversation.id, messageId: call.id, body });
}

async function runRecovery({ quo, shopClient, contacts, phoneNumberId, now = Date.now, lookbackHours = DEFAULT_LOOKBACK_HOURS, pauseMs = 300, draft = generateDraftReply, skip = new Set() }) {
  const since = new Date(now() - lookbackHours * 3600 * 1000).toISOString();
  const totals = { conversations: 0, offered: 0, recorded: 0, failed: 0, calls: 0 };
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
      if (latest && !isInbound(latest)) await shopClient.dispatchHandled({ conversationId: conversation.id, at: latest.createdAt });
      const callPhone = conversation.participants?.[0];
      if (callPhone) {
        const { data: calls = [] } = await quo.listCalls({ phoneNumberId, participants: [callPhone], createdAfter: since, maxResults: 10 });
        const talkedAt = lastConversationAt(calls);
        if (talkedAt) await shopClient.dispatchHandled({ conversationId: conversation.id, at: talkedAt });
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
  } catch (err) {
    totals.failed += 1;
    console.error("Dispatch recovery failed:", err.message);
  }
  const detail = `checked ${totals.conversations}, recovered ${totals.recorded}, calls ${totals.calls}, failed ${totals.failed}`;
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
