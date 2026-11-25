// The only dispatcher code that sends texts. Both switches are off by default:
//   AUTO_REPLIES_ENABLED  owner-approved replies to customers
//   OWNER_TEXTS_ENABLED   approvals, urgent alerts, nudges, daily summary
// A customer text is checked against Quo first and is never retried, so an
// uncertain send cannot reach a customer twice.

const { answerOwner, interpretOwner, recentTurns, customerDetail, leaksPrivateAcquisitionCost } = require("./ownerAssistant");
const { answerProjectQuestion } = require("./projectStatus");
const { answerImperialBrain } = require("./imperialBrain");
const { isAppDefectReport, defectReply, historyForFocus, guardAnswer, whichJobReply } = require("./ownerFocus");
const { callContext } = require("./callIntake");
const { verifyApprovedReplyContext } = require("./replyContext");
const { sendCustomerFollowup } = require("./customerFollowups");

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
  const ownerTexts = enabled("OWNER_TEXTS_ENABLED");
  if (!ownerTexts && !enabled("CUSTOMER_FOLLOWUP_WORKER_ENABLED")) return { sent: 0 };
  const tick = ownerTexts ? await shopClient.dispatchTick() : await shopClient.dispatchTick({ ownerTexts: false });
  const { ownerPhone, messages = [], customerReplies = [], customerFollowups = [] } = tick;
  let sent = 0;
  for (const reply of customerReplies) {
    await sendApprovedReply({ reply, ownerPhone, quo, shopClient });
  }
  for (const reminder of customerFollowups) {
    await sendCustomerFollowup({ reminder, quo, shopClient });
  }
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
  let messageId = null;
  let notice = `#${reply.itemId} was not sent. There's a newer message in that thread, so check Quo.`;
  try {
    // Always revalidate BOTH messages and calls immediately before any
    // owner-approved customer send; a stale draft cannot bypass this via flag.
    const current = await verifyApprovedReplyContext({
      quo, phone: reply.phone, messageId: reply.messageId,
    });
    if (!current.ok) {
      notice = `#${reply.itemId} NOT sent: ${current.notice || "customer conversation has changed"}.
Refresh the customer history and draft.`;
    } else {
      // A previously saved bad draft might still be waiting for YES. Inspect
      // the current owner's Quo history at send-time, not just when drafting.
      if (!ownerPhone) throw Error("Owner identity unavailable");
      const { data: ownerRows = [] } = await quo.listMessages({
        participants: [ownerPhone], maxResults: 100,
      });
      if (!Array.isArray(ownerRows)) throw Error("Owner history unavailable");
      const ownerHistory = ownerRows.filter(m => /^in/i.test(m.direction || ""))
        .map(m => ({ role: "user", content: m.text || m.body || "" }));
      const draft = String(reply.text || "");
      if (/\b(?:my|our|shop|supplier|vendor|wholesale)\s+cost\b/i.test(draft)
          || leaksPrivateAcquisitionCost("", draft, ownerHistory)) {
        notice = `#${reply.itemId} BLOCKED: draft repeats a private shop/vendor cost.
Confirm the customer selling price and make a new draft.`;
        status = "skipped";
      } else {
        status = "failed";
        notice = `#${reply.itemId} failed to send. Please reply in Quo.`;
        const sent = await quo.sendMessage({ to: reply.phone, content: reply.text });
        // Only Quo's own id for the text counts as sent; anything else is reported as not sent.
        if (!sent || !sent.id) throw Error("Quo did not confirm the text");
        messageId = String(sent.id);
        status = "sent";
        notice = `Sent #${reply.itemId} to the customer.`;
        await shopClient.logCommunication({
          phone: reply.phone, direction: "outbound", body: reply.text,
          externalId: sent?.id, createdAt: sent?.createdAt,
        });
      }
    }
  } catch (err) {
    console.error("Approved reply failed:", err.message);
    notice = `#${reply.itemId} NOT sent: the current customer or owner context could not be verified.`;
  }
  await shopClient.dispatchOutbound(messageId ? { key: reply.key, status, messageId } : { key: reply.key, status });
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
    // Resolve the record first, without AI: "eight for Antoine Chevy Impala" is RO #8.
    // A lookup failure leaves focus empty: the text is still answered, just without a resolved RO.
    const focus = await Promise.resolve()
      .then(() => shopClient.ownerFocus(result.ownerQuestion))
      .then((found) => found || {}, () => ({}));
    if (isAppDefectReport(result.ownerQuestion)) {
      // An app problem goes to the Fix list. The reply confirms only what was stored.
      const where = focus.ro ? `${focus.ro.label} ${focus.ro.customer} ${focus.ro.vehicle}`.trim() : "owner text";
      const saved = await Promise.resolve()
        .then(() => shopClient.saveOwnerProblem({ messageId, text: result.ownerQuestion, context: where }))
        .catch((err) => { console.error("Owner problem save failed:", err.message); return null; });
      await quo.sendMessage({ to: phone, content: defectReply(saved, focus) })
        .catch((err) => console.error("Owner answer failed:", err.message));
      return finishCustomerWork({ result, messageId, phone, quo, shopClient });
    }
    if (focus.how === "named_several" && (focus.candidates || []).length) {
      // "RO 8 or RO 12": he named two different jobs. Ask which; never pick one or let the AI guess.
      await quo.sendMessage({ to: phone, content: whichJobReply(focus) })
        .catch((err) => console.error("Owner answer failed:", err.message));
      return finishCustomerWork({ result, messageId, phone, quo, shopClient });
    }
    const thread = await quo.listMessages({ participants: [phone], maxResults: 12 }).catch(() => ({ data: [] }));
    const history = historyForFocus(recentTurns(thread.data || [], messageId), focus, result.ownerQuestion);
    // Project progress is sourced from current GitHub data, not from the
    // customer queue or LLM guesses. It is strictly read-only and flag-gated.
    const brainReply = await answerImperialBrain({
      question: result.ownerQuestion, snapshot: result.snapshot, history,
      messageId,
      taskWriter: shopClient.captureOwnerTask,
      taskLister: shopClient.openOwnerTasks,
      taskChanger: shopClient.changeOwnerTask,
      taskReader: shopClient.readOwnerTask,
      developmentWriter: shopClient.submitDevelopmentWork,
      developmentLister: shopClient.listDevelopmentWork,
    });
    const projectReply = brainReply || await answerProjectQuestion({ question: result.ownerQuestion, history });
    const snapshot = projectReply ? result.snapshot : {
      ...result.snapshot,
      ...(focus.ro ? { focusRecord: focus.ro } : {}),
      ...(focus.candidates ? { focusCandidates: focus.candidates } : {}),
      ...(focus.records && focus.records.length ? { namedRecords: focus.records } : {}),
      ...(focus.missing ? { missingRecord: { type: focus.missingType || "ro", id: focus.missing } } : {}),
      ...(focus.missingRos ? { missingRos: focus.missingRos } : {}),
      customerDetail: await customerDetail({
        question: result.ownerQuestion, snapshot: result.snapshot, quo, callContext,
      }),
    };
    const action = projectReply ? null : await interpretOwner({ question: result.ownerQuestion, snapshot, history });
    let ownerActionReply = null;
    if (!projectReply && action) {
      const request = { ...action, ownerPhone: phone, ownerMessageId: messageId };
      let outcome = await shopClient.dispatchOwnerAction(request);
      if (outcome.customerThreadPhone) {
        // The app has found a priced, fitment-verified RO. Fetch the real Quo
        // customer thread before asking it to queue the signing link.
        const customerPhone = outcome.customerThreadPhone;
        try {
          const { data: messages = [] } = await quo.listMessages({ participants: [customerPhone], maxResults: 10 });
          const newest = [...messages].sort((a, b) => Date.parse(b.createdAt) - Date.parse(a.createdAt))[0];
          const checked = newest?.id
            ? await verifyApprovedReplyContext({ quo, phone: customerPhone, messageId: newest.id })
            : { ok: false, notice: "no current Quo customer text was found" };
          if (!checked.ok) {
            outcome = { ownerReply: "No approval link queued: " + checked.notice + ". Review the RO in the dashboard." };
          } else {
            outcome = await shopClient.dispatchOwnerAction({ ...request, customerMessageId: newest.id });
          }
        } catch (_) {
          outcome = { ownerReply: "No approval link queued: current customer messages could not be verified." };
        }
      }
      ownerActionReply = outcome.ownerReply;
    }
    const answer = projectReply || ownerActionReply || await answerOwner({
      question: result.ownerQuestion,
      snapshot,
      history,
    });
    const content = projectReply || ownerActionReply ? answer : guardAnswer(answer, { focus, snapshot, question: result.ownerQuestion });
    await quo.sendMessage({ to: phone, content }).catch((err) => console.error("Owner answer failed:", err.message));
  }
  return finishCustomerWork({ result, messageId, phone, quo, shopClient });
}

async function finishCustomerWork({ result, messageId, phone, quo, shopClient }) {
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

