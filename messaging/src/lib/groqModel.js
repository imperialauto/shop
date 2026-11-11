// Groq shut down these model IDs for Free and Developer plans in 2026.
// Keep the owner's custom supported model, but do not let an old configured
// ID silently break owner SMS replies or customer draft preparation.
//
// https://console.groq.com/docs/deprecations
const DEFAULT_MODEL = "openai/gpt-oss-120b";
const RETIRED = Object.freeze({
  "llama-3.3-70b-versatile": DEFAULT_MODEL,
  "llama-3.1-8b-instant": "openai/gpt-oss-20b",
  "qwen/qwen3-32b": DEFAULT_MODEL,
  "qwen/qwen3.6-27b": "qwen/qwen3.8-27b",
  "meta-llama/llama-4-scout-17b-16e-instruct": DEFAULT_MODEL,
  "meta-llama/llama-4-maverick-17b-128e-instruct": DEFAULT_MODEL,
  "moonshotai/kimi-k2-instruct-0905": DEFAULT_MODEL,
});

function resolveGroqModel(configured = process.env.GROQ_DRAFT_MODEL) {
  const requested = typeof configured === "string" ? configured.trim() : "";
  return RETIRED[requested] || requested || DEFAULT_MODEL;
}

module.exports = { DEFAULT_MODEL, RETIRED, resolveGroqModel };
