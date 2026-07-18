// ─────────────────────────────────────────────────────────
// Looks up the customer's open repair order / estimate so the AI draft
// can reference real job context ("your brake job is scheduled for...")
// instead of replying blind.
//
// STATUS: currently a graceful no-op. This module's own Prisma schema
// (prisma/schema.prisma) doesn't define Estimate/RepairOrder models — the
// real shop data lives in imperialauto/shop's Python/FastAPI + SQLAlchemy
// database, a completely separate system from this Node service. Rather
// than guess at a schema this service doesn't own, `prisma.estimate` /
// `prisma.repairOrder` are simply undefined here, and the optional
// chaining below makes that return `null` cleanly — draft generation
// proceeds without job context instead of erroring.
//
// To make this real, pick one:
//   (a) Point this service at the shop app's DATABASE_URL read-only and
//       add matching Prisma models once you confirm the real table/column
//       names (e.g. is it `estimates.total`, `total_amount`, `total_cents`?).
//   (b) Add a small internal read endpoint to the shop app (e.g.
//       GET /internal/customers/:phone/open-job) and call it with fetch()
//       from here instead of Prisma.
// Either way, keep the try/catch-and-return-null fallback so a broken
// integration degrades the AI draft's context instead of breaking replies.
// ─────────────────────────────────────────────────────────

/**
 * @param {import("@prisma/client").PrismaClient} prisma
 * @param {{ customerId?: string|null }} conversation
 * @returns {Promise<null | { customerName?: string, vehicle?: string, openEstimate?: object, openRepairOrder?: object, summary: string }>}
 */
async function getJobContextForConversation(prisma, conversation) {
  if (!conversation?.customerId) return null;

  // Guarded with try/catch per-model: if your dashboard doesn't have one
  // of these models (or names it differently), this degrades to partial
  // context instead of failing the whole draft.
  const [customer, openEstimate, openRepairOrder] = await Promise.all([
    prisma.customer.findUnique({ where: { id: conversation.customerId } }).catch(() => null),
    prisma.estimate
      ?.findFirst({
        where: { customerId: conversation.customerId, status: { in: ["OPEN", "PENDING", "SENT"] } },
        orderBy: { createdAt: "desc" },
      })
      .catch(() => null) ?? null,
    prisma.repairOrder
      ?.findFirst({
        where: { customerId: conversation.customerId, status: { in: ["OPEN", "IN_PROGRESS", "SCHEDULED"] } },
        orderBy: { createdAt: "desc" },
        include: { vehicle: true },
      })
      .catch(() => null) ?? null,
  ]);

  if (!customer && !openEstimate && !openRepairOrder) return null;

  const parts = [];
  if (openRepairOrder) {
    const vehicle = openRepairOrder.vehicle
      ? `${openRepairOrder.vehicle.year || ""} ${openRepairOrder.vehicle.make || ""} ${openRepairOrder.vehicle.model || ""}`.trim()
      : null;
    parts.push(
      `Open repair order${vehicle ? ` for their ${vehicle}` : ""}, status: ${openRepairOrder.status}${
        openRepairOrder.description ? ` — ${openRepairOrder.description}` : ""
      }.`
    );
  }
  if (openEstimate) {
    // See TODO above — `total` is a guess pending schema confirmation.
    const total = openEstimate.total ?? openEstimate.totalAmount ?? openEstimate.grandTotal;
    parts.push(
      `Open estimate (status: ${openEstimate.status})${total != null ? ` for approximately $${Number(total).toFixed(2)}` : ""}.`
    );
  }

  return {
    customerName: customer?.name,
    openEstimate,
    openRepairOrder,
    summary: parts.length ? parts.join(" ") : null,
  };
}

module.exports = { getJobContextForConversation };
