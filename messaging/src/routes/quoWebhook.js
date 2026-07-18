// ─────────────────────────────────────────────────────────
// POST /webhooks/quo
//
// Receives Quo (legacy OpenPhone-compatible) webhook events, verifies the
// `openphone-signature` header, and stores inbound/outbound messages.
// On a genuinely new inbound customer message, it also generates an AI
// draft reply and stores it as a PENDING DraftReply — nothing is ever
// sent automatically.
//
// Mount this route BEFORE any global express.json() middleware, or make
// sure express.json() doesn't run on this path — it needs the raw body:
//   app.use("/webhooks/quo", require("./src/routes/quoWebhook"));
//   app.use(express.json()); // fine to apply globally after this line
//
// Quo event shape (legacy `message.received` example):
// {
//   "id": "EVxxxx", "object": "event", "apiVersion": "v2",
//   "createdAt": "...", "type": "message.received",
//   "data": { "object": {
//     "id": "ACxxxx", "object": "message",
//     "from": "+14155550100", "to": "+13105550199",
//     "direction": "incoming", "body": "Hello",
//     "media": [{ "url": "...", "type": "image/jpeg" }],
//     "status": "received", "createdAt": "...",
//     "userId": "USxxxx", "phoneNumberId": "PNxxxx",
//     "conversationId": "CNxxxx"
//   }}
// }
//
// NOTE: Quo is rolling out a newer "beta" webhook scheme (Standard
// Webhooks-compatible headers: webhook-id / webhook-timestamp /
// webhook-signature, whsec_... secrets, and a data.resource/data.context
// payload shape) that is NOT compatible with the legacy hmac;version;
// timestamp;signature format. verifyQuoWebhookSignature.js auto-detects
// and handles BOTH schemes — see that file for details, and set
// QUO_WEBHOOK_SCHEME=legacy|standard in env if you'd rather force one.
// ─────────────────────────────────────────────────────────

const express = require("express");
const prisma = require("../db/prisma");
const { verifyQuoWebhook } = require("../lib/verifyQuoWebhookSignature");
const { generateDraftReply } = require("../lib/generateDraftReply");
const { getJobContextForConversation } = require("../lib/getJobContext");
const shopClient = require("../lib/shopClient");

const router = express.Router();

// How many prior messages to hand the AI as thread context.
const DRAFT_HISTORY_LIMIT = 15;

// IMPORTANT: raw body parser, not express.json() — signature verification
// needs the exact bytes Quo sent. verifyQuoWebhook parses JSON itself once
// the signature checks out and sets req.body.
router.post("/", express.raw({ type: "*/*" }), verifyQuoWebhook, async (req, res) => {
  const event = req.body;

  try {
    switch (event?.type) {
      case "message.received":
      case "message.delivered":
      case "message.sent":
        await handleMessageEvent(event);
        break;
      default:
        // Unhandled event type (e.g. call.* events) — accept and ignore
        // so Quo doesn't retry/disable the webhook for events we don't
        // care about yet.
        console.log(`Quo webhook: ignoring unhandled event type "${event?.type}"`);
    }

    // Always 200 quickly — Quo retries on non-2xx and will eventually
    // disable a webhook that fails repeatedly.
    res.status(200).json({ received: true });
  } catch (err) {
    console.error("Quo webhook handler error:", err);
    // Still ack with 200 once we've verified the signature and logged the
    // failure, UNLESS you want Quo's retry behavior to help you recover
    // from transient DB errors — in that case return 500 instead.
    res.status(200).json({ received: true, error: "processing_failed" });
  }
});

async function handleMessageEvent(event) {
  const msg = event.data?.object;
  if (!msg?.id) return;

  const direction = msg.direction === "incoming" ? "INBOUND" : "OUTBOUND";
  const customerPhone = direction === "INBOUND" ? msg.from : msg.to;

  const customer = await prisma.customer.findFirst({ where: { phone: customerPhone } });

  const conversation = await prisma.conversation.upsert({
    where: { quoConversationId: msg.conversationId },
    update: {
      lastMessageAt: new Date(msg.createdAt),
      customerId: customer?.id,
      // A new inbound message re-opens a conversation that was marked DONE.
      ...(direction === "INBOUND" ? { status: "OPEN", unreadCount: { increment: 1 } } : {}),
    },
    create: {
      quoConversationId: msg.conversationId,
      customerPhone,
      customerId: customer?.id,
      lastMessageAt: new Date(msg.createdAt),
      unreadCount: direction === "INBOUND" ? 1 : 0,
    },
  });

  const stored = await prisma.message.upsert({
    where: { quoMessageId: msg.id },
    update: { quoStatus: msg.status },
    create: {
      conversationId: conversation.id,
      quoMessageId: msg.id,
      direction,
      body: msg.body || "",
      mediaUrls: (msg.media || []).map((m) => (typeof m === "string" ? m : m.url)),
      quoStatus: msg.status,
      quoCreatedAt: new Date(msg.createdAt),
    },
  });

  // Only generate a draft for a genuinely new inbound message (not a
  // status-update replay of one we've already processed), and only if
  // there isn't already a PENDING draft waiting for review.
  const isNewInbound = direction === "INBOUND" && event.type === "message.received";
  if (!isNewInbound) return;

  // Best-effort mirror into the shop app's native Communication log, so
  // an inbound text shows up on the customer's RO detail page too.
  // Fire-and-forget — never let this block draft generation.
  shopClient
    .logCommunication({
      phone: customerPhone,
      direction: "inbound",
      body: msg.body || "",
      externalId: msg.id,
    })
    .catch((err) => console.error("Failed to mirror inbound SMS to shop app:", err));

  const existingPending = await prisma.draftReply.findFirst({
    where: { conversationId: conversation.id, status: "PENDING" },
  });
  if (existingPending) return;

  try {
    const history = await prisma.message.findMany({
      where: { conversationId: conversation.id },
      orderBy: { quoCreatedAt: "desc" },
      take: DRAFT_HISTORY_LIMIT,
    });
    const jobContext = await getJobContextForConversation(prisma, conversation).catch(() => null);

    const draftBody = await generateDraftReply({
      history: history.reverse(),
      customerName: customer?.name,
      jobContext,
    });

    if (draftBody) {
      await prisma.draftReply.create({
        data: {
          conversationId: conversation.id,
          body: draftBody,
          basedOnMessageId: stored.id,
          status: "PENDING",
        },
      });
    }
  } catch (err) {
    // A failed AI draft should never block message storage or the webhook
    // ack — the coordinator can still hit "Regenerate draft" manually.
    console.error("Draft generation failed for conversation", conversation.id, err);
  }
}

module.exports = router;
