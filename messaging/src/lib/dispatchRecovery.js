// Catches inbound messages the webhook missed by reading recent Quo history
// and re-offering them to the Operations queue. The queue ignores any message
// it already has, so repeats are safe. This module never sends a message.

const SOURCE = "quo_recovery";
const DEFAULT_LOOKBACK_HOURS = 6;
const DEFAULT_INTERVAL_MINUTES = 15;

function isInbound(message) {
  return /^(in|inbound|incoming)$/i.test(message.direction || "");
}

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function runRecovery({ quo, shopClient, contacts, phoneNumberId, now = Date.now, lookbackHours = DEFAULT_LOOKBACK_HOURS, pauseMs = 300 }) {
  const since = new Date(now() - lookbackHours * 3600 * 1000).toISOString();
  const totals = { conversations: 0, offered: 0, recorded: 0, failed: 0 };
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
      for (const message of messages.filter(isInbound)) {
        totals.offered += 1;
        const phone = message.from || conversation.participants?.[0];
        const result = await shopClient.dispatchIntake({
          conversationId: conversation.id,
          messageId: message.id,
          phone,
          contactName: contacts ? await contacts.nameFor(phone) : null,
          body: message.text || message.body || "",
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
  const detail = `checked ${totals.conversations}, recovered ${totals.recorded}, failed ${totals.failed}`;
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
