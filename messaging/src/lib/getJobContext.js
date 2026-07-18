// ─────────────────────────────────────────────────────────
// Looks up the customer's real open repair order / invoice from the shop
// app (imperialauto/shop, Python/FastAPI) so the AI draft can reference
// actual job context ("your brake job is scheduled for...") instead of
// replying blind.
//
// Previously this was a no-op: this module's own Prisma schema doesn't
// have Estimate/RepairOrder models, and the real shop data lives in a
// separate Python/SQLAlchemy database. As of 2026-07-17 it calls the shop
// app's internal API (app/routes/internal_messaging.py) over HTTPS
// instead, using conversation.customerPhone as the lookup key — the shop
// app matches loosely on the last 10 digits.
//
// Degrades gracefully: if SHOP_APP_API_URL/INTERNAL_API_KEY aren't set,
// the call fails, or there's no matching customer, this returns null and
// draft generation proceeds without job context — same behavior as
// before, just wired to real data when it's available.
// ─────────────────────────────────────────────────────────

const shopClient = require("./shopClient");

/**
 * @param {import("@prisma/client").PrismaClient} prisma
 * @param {{ customerPhone: string }} conversation
 * @returns {Promise<null | { customerName?: string, openRepairOrder?: object, openInvoice?: object, summary: string|null }>}
 */
async function getJobContextForConversation(_prisma, conversation) {
  if (!conversation?.customerPhone) return null;

  const ctx = await shopClient.getContext(conversation.customerPhone).catch(() => null);
  if (!ctx?.found) return null;

  const parts = [];
  const vehicle = ctx.vehicles?.[0];
  const vehicleLabel = vehicle ? `${vehicle.year || ""} ${vehicle.make || ""} ${vehicle.model || ""}`.trim() : null;

  if (ctx.openRepairOrder) {
    const ro = ctx.openRepairOrder;
    parts.push(
      `Open repair order${vehicleLabel ? ` for their ${vehicleLabel}` : ""}, status: ${ro.status}${
        ro.concern ? ` — ${ro.concern}` : ""
      }${ro.promisedDate ? `, promised ${ro.promisedDate}` : ""}.`
    );
    if (ro.signedAt) {
      parts.push("Customer has already signed off on this repair order.");
    } else if (ro.hasSigningLink) {
      parts.push("There's an estimate/signing link on file for this repair order, not yet signed.");
    }
  }

  if (ctx.openInvoice) {
    const inv = ctx.openInvoice;
    parts.push(
      `Open invoice (status: ${inv.status})${inv.total != null ? ` for $${Number(inv.total).toFixed(2)}` : ""}${
        inv.signedAt ? ", signed" : ", awaiting signature"
      }.`
    );
  }

  return {
    customerName: ctx.customer?.name,
    openRepairOrder: ctx.openRepairOrder,
    openInvoice: ctx.openInvoice,
    summary: parts.length ? parts.join(" ") : null,
  };
}

module.exports = { getJobContextForConversation };
