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

// A manually composed dashboard reply has no AI draft to validate. Require
// a verified latest *local* message seen by the coordinator, then compare
// against the real Quo thread and calls. A stale UI must never send silently.
// This does not establish that the coordinator reviewed call content.
async function verifyManualComposeContext({ quo, prisma, conversation, lastSeenQuoMessageId }) {
  if (!conversation?.id || !conversation.customerPhone || !lastSeenQuoMessageId ||
      typeof lastSeenQuoMessageId !== "string" || lastSeenQuoMessageId.length > 128) {
    return { ok: false, reason: "missing_anchor",
      notice: "refresh this conversation before sending a manually typed reply" };
  }
  try {
    const localLatest = await prisma.message.findFirst({
      where: { conversationId: conversation.id },
      orderBy: { quoCreatedAt: "desc" },
    });
    if (!localLatest || localLatest.quoMessageId !== lastSeenQuoMessageId) {
      return { ok: false, reason: "stale_local",
        notice: "newer conversation activity was recorded; refresh before sending" };
    }
    return verifyApprovedReplyContext({
      quo, phone: conversation.customerPhone, messageId: lastSeenQuoMessageId,
    });
  } catch (_) {
    return { ok: false, reason: "unavailable",
      notice: "the latest conversation history could not be verified" };
  }
}

module.exports = { verifyApprovedReplyContext, verifyDashboardDraftContext, verifyManualComposeContext };
