// ─────────────────────────────────────────────────────────
// One-time (safe to re-run) import of existing Quo conversation + message
// history into the dashboard DB, so the messaging panel doesn't start
// empty on launch day.
//
// Read-only against Quo (GET /v1/conversations, GET /v1/messages) — this
// script never sends a message and never touches AI draft generation.
// It only writes Conversation/Message rows to your own database.
//
// Usage:
//   node src/scripts/backfillQuoHistory.js
//   node src/scripts/backfillQuoHistory.js --since=2025-01-01T00:00:00Z
//   node src/scripts/backfillQuoHistory.js --phone-number-id=PN123abc
//
// Idempotent: Conversation rows are upserted by quoConversationId, Message
// rows by quoMessageId, so re-running only fills in anything new.
// ─────────────────────────────────────────────────────────

require("dotenv").config();
const prisma = require("../db/prisma");
const quo = require("../lib/quoClient");

function parseArgs(argv) {
  const args = {};
  for (const arg of argv.slice(2)) {
    const [key, value] = arg.replace(/^--/, "").split("=");
    args[key] = value ?? true;
  }
  return args;
}

async function resolvePhoneNumberIds(explicitId) {
  if (explicitId) return [explicitId];
  if (process.env.QUO_PHONE_NUMBER_ID) return [process.env.QUO_PHONE_NUMBER_ID];

  const { data: numbers } = await quo.listPhoneNumbers();
  if (!numbers?.length) throw new Error("No Quo phone numbers found on this workspace.");
  console.log(
    `No --phone-number-id given; using all ${numbers.length} phone number(s) on the workspace: ${numbers
      .map((n) => `${n.id} (${n.number || n.name || "unnamed"})`)
      .join(", ")}`
  );
  return numbers.map((n) => n.id);
}

async function* paginate(fetchPage) {
  let pageToken;
  do {
    const page = await fetchPage(pageToken);
    for (const item of page.data || []) yield item;
    pageToken = page.nextPageToken || null;
  } while (pageToken);
}

async function importConversation(conv, phoneNumberId, since) {
  // Single-recipient conversations are the common case for a shop line;
  // group threads (participants.length > 1) are imported under their
  // first participant with a console warning — revisit if you actually
  // use group texting.
  const primaryParticipant = conv.participants?.[0];
  if (!primaryParticipant) return { conversation: null, messageCount: 0 };
  if (conv.participants.length > 1) {
    console.warn(`Conversation ${conv.id} has ${conv.participants.length} participants; importing under ${primaryParticipant} only.`);
  }

  const existingCustomer = await prisma.customer.findFirst({ where: { phone: primaryParticipant } }).catch(() => null);

  const conversation = await prisma.conversation.upsert({
    where: { quoConversationId: conv.id },
    update: { customerId: existingCustomer?.id },
    create: {
      quoConversationId: conv.id,
      customerPhone: primaryParticipant,
      customerId: existingCustomer?.id,
      lastMessageAt: conv.lastActivityAt ? new Date(conv.lastActivityAt) : new Date(),
      unreadCount: 0, // historical import — don't fabricate unread badges
    },
  });

  let messageCount = 0;
  let latestMessageAt = null;

  for await (const msg of paginate((pageToken) =>
    quo.listMessages({
      phoneNumberId,
      participants: [primaryParticipant],
      maxResults: 100,
      pageToken,
      createdAfter: since,
    })
  )) {
    const direction = msg.direction === "incoming" ? "INBOUND" : "OUTBOUND";
    await prisma.message.upsert({
      where: { quoMessageId: msg.id },
      update: { quoStatus: msg.status },
      create: {
        conversationId: conversation.id,
        quoMessageId: msg.id,
        direction,
        body: msg.text || "",
        // The REST list-messages endpoint doesn't return attachment URLs
        // (only the live webhook payload does) — backfilled messages
        // won't have mediaUrls populated. Fine for text history; flag if
        // you need historical MMS attachments too.
        mediaUrls: [],
        quoStatus: msg.status,
        quoCreatedAt: new Date(msg.createdAt),
      },
    });
    messageCount += 1;
    const createdAt = new Date(msg.createdAt);
    if (!latestMessageAt || createdAt > latestMessageAt) latestMessageAt = createdAt;
  }

  if (latestMessageAt && latestMessageAt > conversation.lastMessageAt) {
    await prisma.conversation.update({ where: { id: conversation.id }, data: { lastMessageAt: latestMessageAt } });
  }

  return { conversation, messageCount };
}

async function main() {
  const args = parseArgs(process.argv);
  const since = args.since || null;
  const phoneNumberIds = await resolvePhoneNumberIds(args["phone-number-id"]);

  let totalConversations = 0;
  let totalMessages = 0;

  for (const phoneNumberId of phoneNumberIds) {
    for await (const conv of paginate((pageToken) =>
      quo.listConversations({ phoneNumbers: [phoneNumberId], maxResults: 100, pageToken, updatedAfter: since })
    )) {
      try {
        const { messageCount } = await importConversation(conv, phoneNumberId, since);
        totalConversations += 1;
        totalMessages += messageCount;
        console.log(`Imported conversation ${conv.id}: ${messageCount} message(s).`);
      } catch (err) {
        console.error(`Failed to import conversation ${conv.id}:`, err.message);
      }
    }
  }

  console.log(`\nDone. ${totalConversations} conversation(s), ${totalMessages} message(s) imported/updated.`);
  await prisma.$disconnect();
}

main().catch(async (err) => {
  console.error("Backfill failed:", err);
  await prisma.$disconnect();
  process.exit(1);
});
