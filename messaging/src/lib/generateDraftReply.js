// ─────────────────────────────────────────────────────────
// Calls Claude to draft a reply to a customer text thread. This function
// ONLY returns text — it never sends anything itself. The caller always
// stores the result as a PENDING DraftReply; a human has to approve it
// via POST /api/messages/conversations/:id/send before Quo ever sends it.
// ─────────────────────────────────────────────────────────

const Anthropic = require("@anthropic-ai/sdk");

let _client;
function client() {
  if (!_client) {
    if (!process.env.ANTHROPIC_API_KEY) throw new Error("Missing ANTHROPIC_API_KEY env var");
    _client = new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY });
  }
  return _client;
}

const MODEL = process.env.CLAUDE_DRAFT_MODEL || "claude-sonnet-5";
const MAX_TOKENS = 400;

const SYSTEM_PROMPT = `You are drafting a text message reply on behalf of Imperial Auto Care, an auto repair shop, replying to a customer over SMS.

Rules:
- Sound like a helpful shop employee texting a customer, not a chatbot. Warm, brief, plain language.
- SMS length: 1-3 short sentences. No greetings like "Dear customer". No sign-offs, no emoji unless the customer used one first.
- Never invent a price, date, or repair detail that isn't given to you in the job context below. If you don't have the info, say you'll check and follow up, rather than guessing.
- Never confirm a refund, discount, or free service — that requires a human's judgment call. Flag it for the human ("I'll need to check on that") instead of promising it.
- Output ONLY the message text to send. No quotation marks, no preamble, no "Here's a draft:".`;

/**
 * @param {{
 *   history: Array<{direction: "INBOUND"|"OUTBOUND", body: string, quoCreatedAt: Date}>,
 *   customerName?: string|null,
 *   jobContext?: { summary?: string|null } | null,
 * }} args
 * @returns {Promise<string|null>} draft text, or null if nothing useful could be generated
 */
async function generateDraftReply({ history, customerName, jobContext }) {
  if (!history?.length) return null;

  const transcript = history
    .map((m) => `${m.direction === "INBOUND" ? customerName || "Customer" : "Shop"}: ${m.body}`)
    .join("\n");

  const contextLines = [
    customerName ? `Customer name: ${customerName}` : null,
    jobContext?.summary ? `Job context: ${jobContext.summary}` : "Job context: none on file.",
  ]
    .filter(Boolean)
    .join("\n");

  const userMessage = `${contextLines}\n\nConversation so far (oldest first):\n${transcript}\n\nDraft the shop's next reply.`;

  try {
    const response = await client().messages.create({
      model: MODEL,
      max_tokens: MAX_TOKENS,
      system: SYSTEM_PROMPT,
      messages: [{ role: "user", content: userMessage }],
    });

    const text = response.content
      ?.filter((block) => block.type === "text")
      .map((block) => block.text)
      .join("\n")
      .trim();

    return text || null;
  } catch (err) {
    console.error("generateDraftReply: Claude call failed:", err);
    return null; // caller treats a null/failed draft as "no draft this time", never blocks message storage
  }
}

module.exports = { generateDraftReply };
