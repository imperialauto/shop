// Read-only, plain-English Imperial project status for the owner's Quo thread.
// This module never sends messages or writes to GitHub/Railway.
// It deliberately refuses to invent a status when authenticated GitHub reads fail.
const REPO = "imperialauto/shop";
const PRS = Object.freeze({
  website: { number: 52, label: "Website" },
  dashboard: { number: 55, label: "Dashboard / lead system" },
  finance: { number: 51, label: "Finance" },
});
const MAX_SMS = 900;

function projectTopic(question, history = []) {
  const q = String(question || "").toLowerCase().trim();
  if (!q) return null;
  const askedForStatus = /\b(update|progress|status|ready|launch|launched|finished|done|built|working|fixed|fixes|issues?|bugs?|test(?:ing|s)?|block(?:ed|ers|ing)?|next|left|remaining|how(?:'s| is)|what(?:'s| is)|did|can we)\b/.test(q);
  const context = (Array.isArray(history) ? history.slice(-4) : [])
    .map((turn) => String(turn?.content || "").toLowerCase())
    .join(" ");
  const previousProject = /\b(website|preview|dashboard|imperial project|github|development|finance module|pull request|pr #?5[125])\b/.test(context);
  // Domain objects such as "dashboard" are only project questions when
  // connected to status/progress wording. Avoid swallowing actual shop inquiries.
  if (/\b(website|web site|preview|public site|domain|pr\s*#?52)\b/.test(q)) return "website";
  if (/\b(dashboard|operations (?:app|platform|system)|lead (?:system|pipeline|tracking)|pr\s*#?55)\b/.test(q)) return "dashboard";
  if (/\b(financ(?:e|es|ial) (?:module|system|dashboard|project|pr)|reconciliation project|pr\s*#?51)\b/.test(q)) return "finance";
  if (/\b(bookings?|appointments?)\b/.test(q) && previousProject && askedForStatus) {
    return /\b(dashboard|operations (?:app|platform)|lead (?:system|pipeline))\b/.test(context) &&
      !/\b(website|preview|public site)\b/.test(context) ? "dashboard" : "website";
  }
  if (/\b(our project|imperial (?:project|development)|development|github|pull requests?|all projects?|claude|the build|the launch)\b/.test(q) && askedForStatus) return "all";
  if (/^(?:what(?:'s| is)|how(?:'s| is)|any|so what|did we|can we|when).{0,70}\b(?:done|new|left|next|blocked|holding|ready|finished|complete|update|progress|launch)\b/.test(q) && previousProject) {
    const recentUserTopic = [...(Array.isArray(history) ? history : [])].reverse().find((turn) => {
      const s = String(turn?.content || "").toLowerCase();
      return /\b(website|preview|dashboard|finance module|imperial project)\b/.test(s);
    });
    if (recentUserTopic) {
      const s = String(recentUserTopic.content || "").toLowerCase();
      if (/\b(website|preview)\b/.test(s)) return "website";
      if (/\bdashboard\b/.test(s)) return "dashboard";
      if (/\bfinance module\b/.test(s)) return "finance";
    }
    return "all";
  }
  return null;
}

function checkedState(payload) {
  const checks = payload?.check_runs;
  if (!Array.isArray(checks) || !checks.length) return "Checks not available";
  // GitHub may report older attempts: keep only the newest check with each name.
  const latest = new Map();
  for (const check of checks) {
    const name = String(check.name || "");
    const time = Date.parse(check.completed_at || check.started_at || "1970-01-01");
    if (!latest.has(name) || time > latest.get(name).time) latest.set(name, { check, time });
  }
  const items = [...latest.values()].map((row) => row.check);
  const failed = items.filter((x) => x.status === "completed" && x.conclusion !== "success" && x.conclusion !== "skipped" && x.conclusion !== "neutral");
  const pending = items.filter((x) => x.status !== "completed");
  const pass = items.filter((x) => x.conclusion === "success");
  if (failed.length) return `Checks: ${failed.length} failing, ${pass.length} passing`;
  if (pending.length) return `Checks: ${pending.length} running, ${pass.length} passing`;
  return `Checks: ${pass.length} passing`;
}

function listedBlockers(body) {
  const lines = String(body || "").split("\n");
  const start = lines.findIndex((line) => /^#{2,3}\s*(?:blockers?|remaining|outstanding)\b/i.test(line));
  if (start < 0) return [];
  const found = [];
  for (const line of lines.slice(start + 1)) {
    if (/^#{1,4}\s/.test(line)) break;
    const match = /^\s*[-*]\s+(.+)/.exec(line);
    if (match) found.push(match[1].replace(/`/g, "").replace(/\[[^\]]+\]\([^)]*\)/g, "linked work"));
    if (found.length === 2) break;
  }
  return found;
}

function safeDate(value) {
  const d = new Date(value);
  if (!Number.isFinite(d.getTime())) return "unknown";
  return d.toLocaleString("en-US", { timeZone: "America/Phoenix", month: "short", day: "numeric",
    hour: "numeric", minute: "2-digit", hour12: true });
}

async function githubGet(path, { token, fetchImpl }) {
  const response = await fetchImpl(`https://api.github.com/repos/${REPO}${path}`, {
    headers: { Authorization: `Bearer ${token}`, Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "imperial-project-status" },
    signal: AbortSignal.timeout(9000),
  });
  if (!response.ok) throw new Error(`GitHub returned ${response.status}`);
  return response.json();
}

async function loadPr(key, options) {
  const entry = PRS[key];
  const pr = await githubGet(`/pulls/${entry.number}`, options);
  let checks = "Checks not available";
  if (pr?.head?.sha && /^[0-9a-f]{40}$/i.test(pr.head.sha)) {
    try {
      const result = await githubGet(`/commits/${pr.head.sha}/check-runs?per_page=100`, options);
      checks = checkedState(result);
    } catch (_) {
      checks = "Checks could not be verified";
    }
  }
  return {
    key, number: entry.number, label: entry.label, title: pr.title || "",
    status: pr.merged_at ? "merged (production not verified)" :
      pr.state === "closed" ? "closed without merge" :
      pr.draft ? "draft; not merged" : "open; not merged",
    updated: safeDate(pr.updated_at), checks, blockers: listedBlockers(pr.body),
    url: `https://github.com/${REPO}/pull/${entry.number}`,
  };
}

function renderOne(item, question) {
  const q = String(question || "").toLowerCase();
  const lines = [
    `${item.label} update (GitHub):`,
    `1) PR #${item.number}: ${item.status}.`,
    `2) ${item.checks} on latest commit.`,
    `3) PR last updated ${item.updated} AZ.`,
  ];
  if (/\b(block|holding|need|left|next|issues?|fixes|remaining)\b/.test(q)) {
    lines.push(item.blockers.length
      ? `PR lists (may be outdated): ${item.blockers.join("; ").slice(0, 170)}.`
      : "No verified current blockers listed; review needed.");
  }
  lines.push("Railway/live release not verified by this check.");
  lines.push(item.url);
  return lines.join("\n").slice(0, MAX_SMS);
}

function renderAll(items) {
  const lines = ["Imperial project update (GitHub):"];
  for (const item of items) {
    lines.push(`- ${item.label}: PR #${item.number} ${item.status}; ${item.checks.toLowerCase()}.`);
  }
  lines.push("GitHub status does not confirm a production release.");
  lines.push("Tell me which part you want to dig into.");
  return lines.join("\n").slice(0, MAX_SMS);
}

async function answerProjectQuestion({ question, history = [], token = process.env.PROJECT_STATUS_GITHUB_TOKEN,
  fetchImpl = fetch } = {}) {
  if (process.env.PROJECT_STATUS_ENABLED !== "true") return null;
  const topic = projectTopic(question, history);
  if (!topic) return null;
  if (!token) {
    return "I understand your project question, but live GitHub access isn't connected to Imperial yet. I won't guess at progress. Read-only project access needs to be configured.";
  }
  const targets = topic === "all" ? ["website", "dashboard", "finance"] : [topic];
  const results = await Promise.allSettled(targets.map((key) => loadPr(key, { token, fetchImpl })));
  const good = results.filter((x) => x.status === "fulfilled").map((x) => x.value);
  if (!good.length) return `I couldn't verify the latest ${topic} status from GitHub. I haven't marked anything complete. Please check the repository connection.`;
  if (topic !== "all") return renderOne(good[0], question);
  let rendered = renderAll(good);
  if (good.length !== targets.length) rendered += "\nSome project records could not be read.";
  return rendered.slice(0, MAX_SMS);
}

module.exports = { answerProjectQuestion, projectTopic, checkedState, listedBlockers, loadPr };
