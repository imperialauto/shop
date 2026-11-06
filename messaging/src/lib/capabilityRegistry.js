// Read-only source registry for Imperial's owner SMS assistant.
// "Configured" never means "successfully verified". Current tool permissions
// must be established separately for every provider before an action occurs.
const PROVIDERS = Object.freeze({
  quo: { title: "Quo / Sona", terms: /\b(quo|sona|openphone|phone calls?|texts?|voicemail|transcripts?)\b/i },
  imperial: { title: "Imperial Operations", terms: /\b(imperial (?:operations|app|crm)|shop records?|customer database|repair orders?)\b/i },
  github: { title: "GitHub", terms: /\b(github|pull requests?|prs|repository|commits?)\b/i },
  railway: { title: "Railway", terms: /\b(railway|deployments?|preview (?:server|environment))\b/i },
  claude: { title: "Claude", terms: /\b(claude|anthropic)\b/i },
  chatgpt: { title: "ChatGPT", terms: /\b(chatgpt|openai chat|previous chats?)\b/i },
  alldata: { title: "ALLDATA", terms: /\b(all\s?data|alldata)\b/i },
  autozone: { title: "AutoZonePro", terms: /\b(autozone(?:\s?pro)?)\b/i },
  oreilly: { title: "O'Reilly Professional", terms: /\b(o['’]?reilly(?:'s)?|oreilly)\b/i },
  quickbooks: { title: "QuickBooks", terms: /\b(quickbooks|intuit)\b/i },
  googledrive: { title: "Google Drive", terms: /\b(google drive|google docs|google sheets|my drive)\b/i },
  calendar: { title: "Calendar", terms: /\b(calendar|apple calendar|google calendar)\b/i },
  obsidian: { title: "Obsidian", terms: /\b(obsidian)\b/i },
});
const ACCESS_ASK = /\b(can (?:you|imperial|we) (?:access|read|see|use|connect(?: to)?|check)|do (?:you|we) have (?:access|a connection|an integration)|are (?:you|we) connected|which (?:apps|tools|systems|services)|what (?:apps|tools|systems|services) (?:can you|are you)|connected (?:apps|tools|systems|services))\b/i;

function capabilityIntent(question) {
  const q = String(question || "");
  if (!ACCESS_ASK.test(q)) return null;
  const match = Object.entries(PROVIDERS).find(([, info]) => info.terms.test(q));
  return match ? match[0] : "all";
}

function capabilityState(key, { snapshot, projectStatusConfigured = false } = {}) {
  switch (key) {
    case "quo": return "This conversation is through Quo. Calls/transcripts require a separate successful read; no unlimited access is assumed.";
    case "imperial": return snapshot && Array.isArray(snapshot.waitingOnYou)
      ? "Live dispatch queue and near-term app bookings are in my current snapshot. That does not include every task, invoice or website form."
      : "Shop data is not present in the current snapshot; I can't verify a live read right now.";
    case "github": return projectStatusConfigured
      ? "Read-only GitHub source is configured. Each question must still verify a fresh PR, issue or CI read."
      : "Imperial SMS needs a scoped, read-only GitHub credential to check private project status. ChatGPT's connection does not transfer automatically.";
    case "railway": return "Live Railway deployment and cost reads are not connected to the Imperial SMS project adapter yet.";
    case "claude": return "Claude session access is not connected. GitHub commits/PRs can show delivered code when the GitHub adapter works, but not uncommitted Claude activity.";
    case "chatgpt": return "Earlier ChatGPT conversations are not automatically shared with this Railway SMS assistant. Decisions need to be stored in Imperial's Knowledge Hub.";
    case "alldata": return "No verified ALLDATA adapter is connected to Imperial SMS. Requires supported authorized access before I can research repair data.";
    case "autozone": return "No verified AutoZonePro adapter is connected to Imperial SMS. I can't claim live parts prices from it.";
    case "oreilly": return "No verified O'Reilly Professional adapter is connected to Imperial SMS.";
    case "quickbooks": return "No verified QuickBooks adapter is connected to Imperial SMS. Owner-only access will be required.";
    case "googledrive": return "No verified Google Drive adapter is connected to Imperial SMS; files in ChatGPT are not automatically readable here.";
    case "calendar": return snapshot && Array.isArray(snapshot.calendarNext7Days)
      ? "A linked calendar is readable in this snapshot, but it may not include other calendars. Booking data is separate."
      : "Calendar coverage could not be verified from this snapshot.";
    case "obsidian": return "Obsidian is not connected to Imperial SMS. It is optional; Imperial's own Knowledge Hub should be the canonical source.";
    default: return "I can't verify that system's access from this request.";
  }
}

function answerCapabilityQuestion({ question, snapshot, projectStatusConfigured = false }) {
  const key = capabilityIntent(question);
  if (!key) return null;
  if (key === "all") {
    const shop = capabilityState("imperial", { snapshot });
    const git = capabilityState("github", { projectStatusConfigured });
    return [
      "Imperial's current source access:",
      "1) Quo owner texts: this conversation is active.",
      "2) Shop: " + shop,
      "3) GitHub: " + git,
      "4) Other systems require individual authorization/verification. Ask about any one by name.",
    ].join("\n").slice(0, 900);
  }
  return `${PROVIDERS[key].title}: ${capabilityState(key, { snapshot, projectStatusConfigured })}`.slice(0, 900);
}

module.exports = { PROVIDERS, capabilityIntent, capabilityState, answerCapabilityQuestion };
