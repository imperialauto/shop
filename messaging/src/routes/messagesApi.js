// ─────────────────────────────────────────────────────────
// /api/messages/* — backs src/components/MessagingPanel.jsx
//
// Mount behind your existing auth/cookie middleware, e.g.:
//   app.use("/api/messages", requireAuth, express.json(), messagesApiRouter);
//
// Routes:
//   GET  /api/messages/conversations?status=OPEN
//   GET  /api/messages/conversations/:id
//   POST /api/messages/conversations/:id/draft   (regenerate draft)
//   POST /api/messages/conversations/:id/send    (approve & send)
//   POST /api/messages/conversations/:id/status  (mark OPEN/DONE — also
//                                                 syncs Quo's own inbox
//                                                 view via mark-as-done/
//                                                 mark-as-open)
// ─────────────────────────────────────────────────────────

const express = require("express");
const prisma = require("../db/prisma");
const quo = require("../lib/quoClient");
const shopClient = require("../lib/shopClient");
const { generateDraftReply } = require("../lib/generateDraftReply");
const { getJobContextForConversation } = require("../lib/getJobContext");

const router = express.Router();

const DRAFT_HISTORY_LIMIT = 15;

// Express 4 doesn't catch rejected promises from async handlers on its
// own — an unhandled rejection here would hang the request instead of
// returning an error. Wrap every handler so a thrown/rejected error
// becomes a clean 500 instead of a stuck connection.
function asyncHandler(fn) {
  return (req, res, next) => Promise.resolve(fn(req, res, next)).catch(next);
}

// GET /api/messages/conversations?status=OPEN
router.get("/conversations", asyncHandler(async (req, res) => {
  const status = req.query.status === "DONE" ? "DONE" : req.query.status === "OPEN" ? "OPEN" : undefined;

  const conversations = await prisma.conversation.findMany({
    where: status ? { status } : undefined,
    orderBy: { lastMessageAt: "desc" },
    include: {
      customer: true,
      messages: { orderBy: { quoCreatedAt: "desc" }, take: 1 },
      drafts: { where: { status: "PENDING" }, take: 1 },
    },
    take: 100,
  });

  res.json(
    conversations.map((c) => ({
      id: c.id,
      status: c.status,
      customerPhone: c.customerPhone,
      customerName: c.customer?.name || null,
      unreadCount: c.unreadCount,
      lastMessage: c.messages[0] || null,
      hasPendingDraft: c.drafts.length > 0,
    }))
  );
}));

// GET /api/messages/conversations/:id
router.get("/conversations/:id", asyncHandler(async (req, res) => {
  const conversation = await prisma.conversation.findUnique({
    where: { id: req.params.id },
    include: {
      customer: true,
      messages: { orderBy: { quoCreatedAt: "asc" } },
      drafts: { where: { status: "PENDING" }, orderBy: { createdAt: "desc" }, take: 1 },
    },
  });

  if (!conversation) return res.status(404).json({ error: "Conversation not found" });

  // Opening a conversation clears its unread badge and tells Quo it's read.
  if (conversation.unreadCount > 0) {
    await prisma.conversation.update({ where: { id: conversation.id }, data: { unreadCount: 0 } });
    quo.markConversationRead(conversation.quoConversationId).catch((err) =>
      console.error("Failed to mark Quo conversation read:", err)
    );
  }

  res.json({
    id: conversation.id,
    status: conversation.status,
    customerPhone: conversation.customerPhone,
    customerName: conversation.customer?.name || null,
    messages: conversation.messages,
    drafts: conversation.drafts,
  });
}));

// POST /api/messages/conversations/:id/status — mark OPEN or DONE.
// Body: { status: "OPEN" | "DONE" }
// Also mirrors the change to Quo's own inbox via mark-as-done/mark-as-open
// so the coordinator's view in the Quo app itself doesn't drift from this
// dashboard's view of the same conversation.
router.post("/conversations/:id/status", asyncHandler(async (req, res) => {
  const { status } = req.body || {};
  if (status !== "OPEN" && status !== "DONE") {
    return res.status(400).json({ error: 'status must be "OPEN" or "DONE"' });
  }

  const conversation = await prisma.conversation.findUnique({ where: { id: req.params.id } });
  if (!conversation) return res.status(404).json({ error: "Conversation not found" });

  const updated = await prisma.conversation.update({ where: { id: conversation.id }, data: { status } });

  const quoSync = status === "DONE" ? quo.markConversationDone : quo.markConversationOpen;
  quoSync(conversation.quoConversationId).catch((err) =>
    console.error(`Failed to sync status=${status} to Quo for conversation ${conversation.id}:`, err)
  );

  res.json({ id: updated.id, status: updated.status });
}));

// POST /api/messages/conversations/:id/draft — regenerate the AI draft
router.post("/conversations/:id/draft", asyncHandler(async (req, res) => {
  const conversation = await prisma.conversation.findUnique({
    where: { id: req.params.id },
    include: { customer: true },
  });
  if (!conversation) return res.status(404).json({ error: "Conversation not found" });

  const history = await prisma.message.findMany({
    where: { conversationId: conversation.id },
    orderBy: { quoCreatedAt: "desc" },
    take: DRAFT_HISTORY_LIMIT,
  });
  const jobContext = await getJobContextForConversation(prisma, conversation).catch(() => null);

  const body = await generateDraftReply({
    history: history.reverse(),
    customerName: conversation.customer?.name,
    jobContext,
  });

  // generateDraftReply returns null (rather than throwing) when Claude
  // call fails or there's nothing to draft from — leave the existing
  // pending draft (if any) untouched rather than clobbering it with
  // nothing, and tell the UI so it can show a "couldn't generate" state.
  if (!body) {
    return res.status(502).json({ error: "Draft generation failed — see server logs for details" });
  }

  // Discard any prior pending draft for this thread, then store the new one.
  await prisma.draftReply.updateMany({
    where: { conversationId: conversation.id, status: "PENDING" },
    data: { status: "DISCARDED" },
  });

  const draft = await prisma.draftReply.create({
    data: { conversationId: conversation.id, body, status: "PENDING" },
  });

  res.json(draft);
}));

// POST /api/messages/conversations/:id/signing-link — look up this
// customer's open repair order in the shop app and stage a PENDING draft
// with a link to sign the estimate. This does NOT send anything — same
// as /draft, it only creates a draft the coordinator still has to review
// and approve via /send. Requires SHOP_APP_API_URL + INTERNAL_API_KEY to
// be configured on this service.
router.post("/conversations/:id/signing-link", asyncHandler(async (req, res) => {
  if (!shopClient.configured()) {
    return res.status(503).json({ error: "Shop app integration not configured (SHOP_APP_API_URL / INTERNAL_API_KEY)" });
  }

  const conversation = await prisma.conversation.findUnique({ where: { id: req.params.id } });
  if (!conversation) return res.status(404).json({ error: "Conversation not found" });

  const ctx = await shopClient.getContext(conversation.customerPhone);
  if (!ctx.found || !ctx.openRepairOrder) {
    return res.status(404).json({ error: "No matching customer with an open repair order found in the shop app" });
  }

  const link = await shopClient.getSigningLink(ctx.openRepairOrder.id);

  // Discard any prior pending draft for this thread, then store the new one.
  await prisma.draftReply.updateMany({
    where: { conversationId: conversation.id, status: "PENDING" },
    data: { status: "DISCARDED" },
  });

  const draft = await prisma.draftReply.create({
    data: { conversationId: conversation.id, body: link.suggestedText, status: "PENDING" },
  });

  res.json({ draft, repairOrderId: link.repairOrderId, signingUrl: link.signingUrl });
}));

// POST /api/messages/conversations/:id/send — approve & send.
// THIS IS THE ONLY PATH IN THIS MODULE THAT ACTUALLY SENDS A TEXT — it
// only runs when a human hits "Send" in the dashboard UI, never
// automatically. Body: { draftId?: string, body: string }
router.post("/conversations/:id/send", asyncHandler(async (req, res) => {
  const { draftId, body } = req.body || {};
  if (!body || !body.trim()) return res.status(400).json({ error: "Message body is required" });

  const conversation = await prisma.conversation.findUnique({ where: { id: req.params.id } });
  if (!conversation) return res.status(404).json({ error: "Conversation not found" });

  let originalDraft = null;
  if (draftId) {
    originalDraft = await prisma.draftReply.findUnique({ where: { id: draftId } });
  }

  // TODO: derive `from` (shop's Quo number) from your phone-number config
  // if you run more than one line; quoClient defaults to QUO_DEFAULT_FROM_NUMBER.
  const sent = await quo.sendMessage({ to: conversation.customerPhone, content: body });

  const message = await prisma.message.create({
    data: {
      conversationId: conversation.id,
      quoMessageId: sent?.id || null,
      direction: "OUTBOUND",
      body,
      quoStatus: sent?.status || "queued",
      sentByUserId: req.user?.id || null, // set by your auth middleware
      quoCreatedAt: sent?.createdAt ? new Date(sent.createdAt) : new Date(),
    },
  });

  if (draftId && originalDraft) {
    // SENT if the coordinator approved the draft body verbatim, EDITED if
    // they changed it first — both mean "a human approved this."
    const wasEditedBeforeSend = originalDraft.body !== body;
    await prisma.draftReply.update({
      where: { id: draftId },
      data: {
        status: wasEditedBeforeSend ? "EDITED" : "SENT",
        sentMessageId: message.id,
        approvedByUserId: req.user?.id || null,
        approvedAt: new Date(),
      },
    });
  }

  await prisma.conversation.update({
    where: { id: conversation.id },
    data: { lastMessageAt: message.quoCreatedAt },
  });

  // Best-effort mirror into the shop app's native Communication log so
  // this text shows up on the customer's RO detail page. Fire-and-forget
  // — never let this failing block the response, since the message has
  // already actually been sent.
  shopClient
    .logCommunication({
      phone: conversation.customerPhone,
      direction: "outbound",
      body,
      externalId: sent?.id || null,
    })
    .catch((err) => console.error("Failed to mirror outbound SMS to shop app:", err));

  res.json({ message, sent });
}));

// Router-level error handler — catches anything asyncHandler forwarded via
// next(err) so callers always get JSON back, even if this router is
// mounted into a host app that doesn't have its own JSON error handler.
router.use((err, req, res, next) => {
  console.error("messagesApi error:", err);
  if (res.headersSent) return next(err);
  res.status(err.status && err.status < 500 ? err.status : 500).json({ error: err.message || "Internal server error" });
});

module.exports = router;
