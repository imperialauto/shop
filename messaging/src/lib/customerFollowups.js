const { verifyApprovedReplyContext } = require("./replyContext");

const OPT_OUT = /^\s*(?:stop(?:\s+all)?|stopall|unsubscribe|cancel|end|quit|opt\s*out|remove me)\s*[.!]?\s*$|\b(?:do not|don't|please stop)\s+(?:text|contact|messag)|\b(?:no thanks|not interested|already (?:fixed|repaired|handled|taken care of)|went (?:with|to) another)\b/i;

async function currentThread(quo, reminder) {
  const participants = [reminder.phone];
  let pageToken;
  const seenPages = new Set();
  do {
    const result = await quo.listMessages({ participants, maxResults: 100, pageToken });
    if (!Array.isArray(result?.data) ||
        (result.data.some(message => /^in/i.test(message.direction || "") && OPT_OUT.test(message.text || message.body || "")))) {
      return false;
    }
    if (reminder.callId && result.data.length) return false;
    if (result.nextPageToken && seenPages.has(result.nextPageToken)) return false;
    pageToken = result.nextPageToken;
    if (pageToken) seenPages.add(pageToken);
  } while (pageToken);
  if (reminder.messageId) {
    return (await verifyApprovedReplyContext({ quo, phone: reminder.phone, messageId: reminder.messageId })).ok;
  }
  if (!reminder.callId) return false;
  const { data: calls } = await quo.listCalls({ participants, maxResults: 10 });
  if (!Array.isArray(calls) || calls.some(call => !Number.isFinite(Date.parse(call.createdAt)))) return false;
  const newest = [...calls].sort((a, b) => Date.parse(b.createdAt) - Date.parse(a.createdAt))[0];
  return newest?.id === reminder.callId && /^in/i.test(newest.direction || "");
}

async function sendCustomerFollowup({ reminder, quo, shopClient }) {
  let status = "skipped";
  try {
    if (await currentThread(quo, reminder) && (await shopClient.checkCustomerFollowup(reminder))?.ok) {
      status = "failed";  // Never retry an ambiguous provider result.
      const sent = await quo.sendMessage({ to: reminder.phone, content: reminder.text });
      if (!sent?.id) throw Error("Quo did not return a message ID");
      status = "sent";
      await shopClient.logCommunication({ phone: reminder.phone, direction: "outbound", body: reminder.text,
        externalId: sent?.id, createdAt: sent?.createdAt });
    }
  } catch (error) {
    console.error("Customer follow-up:", error.message);
  }
  await shopClient.dispatchOutbound({ key: reminder.key, status });
}

module.exports = { sendCustomerFollowup };
