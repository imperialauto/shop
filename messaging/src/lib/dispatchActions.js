// The only dispatcher code that sends texts. Both switches are off by default:
//   AUTO_REPLIES_ENABLED  owner-approved replies to customers
//   OWNER_TEXTS_ENABLED   approvals, urgent alerts, nudges, daily summary
// A customer text is checked against Quo first and is never retried, so an
// uncertain send cannot reach a customer twice.

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

async function handleIntakeResult({ result, messageId, phone, quo, shopClient }) {
  if (result.ownerReply && enabled("OWNER_TEXTS_ENABLED")) {
    await quo.sendMessage({ to: phone, content: result.ownerReply });
  }
  if (result.autoReply) {
    await sendAutoReply({ autoReply: result.autoReply, messageId, phone, quo, shopClient });
  }
  await deliverOwnerMessages({ quo, shopClient });
}

module.exports = { handleIntakeResult, deliverOwnerMessages };
