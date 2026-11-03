// ─────────────────────────────────────────────────────────
// Calls an LLM to draft a reply to a customer text thread. This function
// ONLY returns text — it never sends anything itself. The caller always
// stores the result as a PENDING DraftReply; a human has to approve it
// via POST /api/messages/conversations/:id/send before Quo ever sends it.
//
// Provider: Groq (OpenAI-compatible chat completions API), switched from
// Anthropic on 2026-07-17 at the owner's request to avoid needing paid
// Anthropic credits — GROQ_API_KEY was already sitting unused in the
// shop app's env. Uses plain fetch() instead of an SDK so no new
// dependency has to be added to package.json.
//
// Same system prompt / safety rules as before the swap — only the
// transport and model changed, to keep drafting behavior as close to
// identical as possible.
// ─────────────────────────────────────────────────────────

const GROQ_API_BASE_URL = process.env.GROQ_API_BASE_URL || "https://api.groq.com/openai/v1";
const MODEL = process.env.GROQ_DRAFT_MODEL || "llama-3.3-70b-versatile";
const MAX_TOKENS = 400;

const SYSTEM_PROMPT = `You are drafting a text message reply on behalf of Imperial Auto Care, an auto repair shop, replying to a customer over SMS.

Rules:
- The owner is Jaelan, spelled exactly that way. Write as the shop and never refer to Jaelan in the third person. Use only names that appear in the conversation, and if a name or part looks garbled, leave it out.
- Sound like a helpful shop employee texting a customer, not a chatbot. Warm, brief, plain language.
- SMS length: 1-3 short sentences. No greetings like "Dear customer". No sign-offs, no emoji unless the customer used one first.
- Shop facts you may use: 7915 N Glen Harbor Blvd, Suite 508, Glendale, AZ 85307. Open Mon-Fri 11 AM-7 PM and Sat 12-5 PM, closed Sunday. Basic diagnostic screening is $75, the standard in-shop diagnostic is $150, and a mobile service call is $75 plus applicable travel.
- Read the whole conversation and answer the customer's latest message. Do not repeat a question they already answered.
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
  if (!process.env.GROQ_API_KEY) {
    console.error("generateDraftReply: Missing GROQ_API_KEY env var");
    return null;
  }

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
    const res = await fetch(`${GROQ_API_BASE_URL}/chat/completions`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${process.env.GROQ_API_KEY}`,
      },
      body: JSON.stringify({
        model: MODEL,
        max_tokens: MAX_TOKENS,
        messages: [
          { role: "system", content: SYSTEM_PROMPT },
          { role: "user", content: userMessage },
        ],
      }),
    });

    if (!res.ok) {
      const errBody = await res.text().catch(() => "");
      throw new Error(`Groq API ${res.status}: ${errBody.slice(0, 500)}`);
    }

    const data = await res.json();
    const text = data.choices?.[0]?.message?.content?.trim();

    return text || null;
  } catch (err) {
    console.error("generateDraftReply: Groq call failed:", err);
    return null; // caller treats a null/failed draft as "no draft this time", never blocks message storage
  }
}

module.exports = { generateDraftReply };
