// The only dispatcher code that sends texts. Both switches are off by default:
//   AUTO_REPLIES_ENABLED  owner-approved replies to customers
//   OWNER_TEXTS_ENABLED   approvals, urgent alerts, nudges, daily summary
// A customer text is checked against Quo first and is never retried, so an
// uncertain send cannot reach a customer twice.

const { answerOwner, recentTurns } = require("./ownerAssistant");

function enabled(name) {
  return process.env[name] === "true";
}

async function latestMessageId(quo, phone) {
  const { data = [] } = await quo.listMessages({ participants: [phone], maxResults: 10 });
  const newest = [...data].sort((a, b) => new Date(b.createdAt) - new Date(a.createdAt))[0];
  return newest?.id;
}

async function sendAutoReply({ autoReply, messageId, phone, quo, shopClient }) {
  if (!enabled("AUTO_REPLIES_ENABLED")) {
    return shopClient.dispatchOutbound({ key: autoReply.key, status: "skipped" });
  }
  let newest;
  try {
    newest = await latestMessageId(quo, phone);
  } catch (err) {
    console.error("Auto reply check failed:", err.message);
    return shopClient.dispatchOutbound({ key: autoReply.key, status: "skipped" });
  }
  if (newest !== messageId) {
    return shopClient.dispatchOutbound({ key: autoReply.key, status: "skipped" });
  }
  try {
    await quo.sendMessage({ to: phone, content: autoReply.text });
    return shopClient.dispatchOutbound({ key: autoReply.key, status: "sent" });
  } catch (err) {
    console.error("Auto reply send failed:", err.message);
    return shopClient.dispatchOutbound({ key: autoReply.key, status: "failed" });
  }
}

async function deliverOwnerMessages({ quo, shopClient }) {
  if (!enabled("OWNER_TEXTS_ENABLED")) return { sent: 0 };
  const { ownerPhone, messages = [] } = await shopClient.dispatchTick();
  let sent = 0;
  for (const message of messages) {
    try {
      await quo.sendMessage({ to: ownerPhone, content: message.text });
      await shopClient.dispatchOutbound({ key: message.key, status: "sent" });
      sent += 1;
    } catch (err) {
      console.error("Owner text failed:", err.message);
      await shopClient.dispatchOutbound({ key: message.key, status: "failed" });
    }
  }
  return { sent };
}

// An owner-approved reply. It stops if anything newer is in the thread, including
// a reply sent from Quo, and a failed send is never retried automatically.
async function sendApprovedReply({ reply, ownerPhone, quo, shopClient }) {
  let status = "skipped";
  let notice = `#${reply.itemId} was not sent. There's a newer message in that thread, so check Quo.`;
  try {
    if (((await latestMessageId(quo, reply.phone)) || "none") === reply.messageId) {
      status = "failed";
      notice = `#${reply.itemId} failed to send. Please reply in Quo.`;
      await quo.sendMessage({ to: reply.phone, content: reply.text });
      status = "sent";
      notice = `Sent #${reply.itemId} to the customer.`;
      await shopClient.logCommunication({ phone: reply.phone, direction: "outbound", body: reply.text, externalId: null });
    }
  } catch (err) {
    console.error("Approved reply failed:", err.message);
  }
  await shopClient.dispatchOutbound({ key: reply.key, status });
  await quo.sendMessage({ to: ownerPhone, content: notice }).catch((err) => console.error("Owner notice failed:", err.message));
  return status === "sent";
}

async function handleIntakeResult({ result, messageId, phone, quo, shopClient }) {
  if (result.ownerReply && enabled("OWNER_TEXTS_ENABLED")) {
    try {
      await quo.sendMessage({ to: phone, content: result.ownerReply });
    } catch (err) {
      console.error("Owner reply failed:", err.message); // pending alerts must still go out
    }
  }
  if (result.ownerQuestion && enabled("OWNER_TEXTS_ENABLED")) {
    const thread = await quo.listMessages({ participants: [phone], maxResults: 12 }).catch(() => ({ data: [] }));
    const content = await answerOwner({
      question: result.ownerQuestion,
      snapshot: result.snapshot,
      history: recentTurns(thread.data || [], messageId),
    });
    await quo.sendMessage({ to: phone, content }).catch((err) => console.error("Owner answer failed:", err.message));
  }
  let customerSent = false;
  if (result.customerReply) {
    if (enabled("OWNER_TEXTS_ENABLED")) {
      customerSent = await sendApprovedReply({ reply: result.customerReply, ownerPhone: phone, quo, shopClient });
    } else {
      await shopClient.dispatchOutbound({ key: result.customerReply.key, status: "skipped" });
    }
  }
  if (result.autoReply) {
    await sendAutoReply({ autoReply: result.autoReply, messageId, phone, quo, shopClient });
  }
  return { customerSent };
}

module.exports = { handleIntakeResult, deliverOwnerMessages };
