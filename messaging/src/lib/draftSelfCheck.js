// Runs the live drafting model on made-up conversations and logs the results,
// so changes can be judged on real model output. Sends nothing.

const { generateDraftReply } = require("./generateDraftReply");

const at = (minute) => new Date(Date.UTC(2026, 0, 5, 18, minute));
const inbound = (minute, body, mediaUrls = []) => ({ direction: "INBOUND", body, mediaUrls, quoCreatedAt: at(minute) });
const outbound = (minute, body) => ({ direction: "OUTBOUND", body, mediaUrls: [], quoCreatedAt: at(minute) });
const dropOffCall = "Phone call at 2026-01-05T18:10:00Z:\nShop: If you want to drop that part off, one of my guys is at the shop.\nCustomer: I can be there in fifteen minutes.\nShop: That's perfect.";

const SCENARIOS = [
  ["part already dropped off", [inbound(0, "Can I drop off a part?"), inbound(40, "", ["photo"]), inbound(41, "I just put it in the trunk of the car")], dropOffCall],
  ["thanks only", [inbound(0, "What are your hours?"), outbound(1, "We're open Mon-Fri 11 AM-7 PM and Sat 12-5 PM."), inbound(2, "Okay thank you")], null],
  ["open question", [inbound(0, "Can I drop off a part today?")], null],
  ["customer waiting", [outbound(0, "See you at 11 AM."), inbound(50, "I'm here")], null],
  ["repair price", [inbound(0, "How much to replace the radiator on a 2005 Yukon?")], null],
  ["apology already sent", [inbound(0, "I waited an hour"), outbound(5, "I'm so sorry about that."), inbound(6, "It's fine, see you another day")], null],
  ["owner promised update", [inbound(0, "What about the Jeep?")], "Phone call at 2026-01-05T17:00:00Z:\nShop: I'll keep you posted once the part arrives.\nCustomer: Sounds good."],
];

async function runDraftSelfCheck() {
  if (process.env.OPENAI_API_KEY) {
    const res = await fetch("https://api.openai.com/v1/models", { headers: { Authorization: `Bearer ${process.env.OPENAI_API_KEY}` } });
    const body = await res.json().catch(() => ({}));
    const names = (body.data || []).map((m) => m.id).filter((id) => /^gpt-/.test(id) && !/audio|realtime|search|transcribe|tts|image/.test(id)).sort();
    console.log(`DRAFT_CHECK models ${res.status}: ${names.join(" ")}`.slice(0, 1500));
  }
  for (const [name, history, summary] of SCENARIOS) {
    const reply = await generateDraftReply({ history, jobContext: summary ? { summary } : null, allowNoReply: true });
    await new Promise((resolve) => setTimeout(resolve, 1500));
    console.log(`DRAFT_CHECK ${name} => ${String(reply).replace(/\s+/g, " ")}`);
  }
}

module.exports = { runDraftSelfCheck, SCENARIOS };
