// Revalidate source context immediately before an owner-approved customer SMS.
// Read-only Quo calls. If recent calls cannot be checked, fail closed.
// This is deliberately more conservative than sending an outdated promise.
async function verifyApprovedReplyContext({ quo, phone, messageId }) {
  if (!phone || !messageId || !quo?.listMessages || !quo?.listCalls) {
    return { ok: false, reason: "unavailable", notice: "couldn't verify current messages and calls" };
  }
  try {
    const { data: messages = [] } = await quo.listMessages({ participants: [phone], maxResults: 10 });
    if (!Array.isArray(messages) || !messages.length) {
      return { ok: false, reason: "unavailable", notice: "no current message record was found" };
    }
    const newest = [...messages].sort((a, b) => Date.parse(b.createdAt) - Date.parse(a.createdAt))[0];
    if (newest.id !== messageId) {
      return { ok: false, reason: "new_message", notice: "a newer text arrived" };
    }
    const basedOn = Date.parse(newest.createdAt);
    if (!Number.isFinite(basedOn)) {
      return { ok: false, reason: "unavailable", notice: "message time couldn't be verified" };
    }
    const { data: calls = [] } = await quo.listCalls({
      participants: [phone], maxResults: 10,
      createdAfter: new Date(basedOn).toISOString(),
    });
    if (!Array.isArray(calls)) {
      return { ok: false, reason: "unavailable", notice: "call history couldn't be verified" };
    }
    if (calls.some((call) => !Number.isFinite(Date.parse(call.createdAt)) ||
          Date.parse(call.createdAt) >= basedOn)) {
      return { ok: false, reason: "new_call", notice: "there has been a call since the draft's source message" };
    }
    return { ok: true, reason: "verified" };
  } catch (_) {
    return { ok: false, reason: "unavailable", notice: "current call or text history couldn't be checked" };
  }
}

// Dashboard drafts use the same message/call guard, plus a verified
// conversation-local source record. This never sends a customer message.
async function verifyDashboardDraftContext({ quo, prisma, conversation, draft }) {
  if (!conversation || !draft || draft.conversationId !== conversation.id ||
      draft.status !== "PENDING") {
    return { ok: false, reason: "wrong_draft", notice: "the draft is no longer pending in this conversation" };
  }
  const basis = draft.basedOnMessageId
    ? await prisma.message.findUnique({ where: { id: draft.basedOnMessageId } })
    : null;
  if (!basis?.quoMessageId || basis.conversationId !== conversation.id) {
    return { ok: false, reason: "unavailable", notice: "draft's source message cannot be verified" };
  }
  return verifyApprovedReplyContext({
    quo, phone: conversation.customerPhone, messageId: basis.quoMessageId,
  });
}

module.exports = { verifyApprovedReplyContext, verifyDashboardDraftContext };
