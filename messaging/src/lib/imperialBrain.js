// Imperial's first read-only question router. Existing owner SMS is the
// entrypoint; source reads are explicit and auditable in tests. This is not a
// general autonomous-action agent and never performs a state-changing tool call.
const { answerProjectQuestion, answerDevelopmentTasks, projectTopic } = require("./projectStatus");
const { answerCapabilityQuestion } = require("./capabilityRegistry");

function developmentTasksIntent(question) {
  const q = String(question || "").toLowerCase();
  const task = /\b(tasks?|to.?do|backlog|checklist|remaining|priorit(?:y|ies)|what(?:'s| is) next)\b/.test(q);
  const dev = /\b(website|preview|github|development|claude|code|build|software|project|sprint|launch|financ(?:e|es|ial) (?:module|project)|dashboard)\b/.test(q);
  return task && dev;
}

function shopTasksIntent(question) {
  const q = String(question || "").toLowerCase().trim();
  if (/\b(?:what(?:'s| is)|show|list|tell me|anything|any)\b.{0,30}\b(?:my|our)\s+(?:to.?do\s*list|task\s*list|list of tasks)\b/.test(q)) return true;
  if (/\b(create|add|assign|delegate|make|schedule|remind)\b/.test(q) &&
      /\b(a task|to my (?:list|tasks)|to.?do|someone|my mom|mother)\b/.test(q)) return false;
  if (/\b(customers?|service|shop|staff|estimates?|appointments?|repairs?|inbox|follow.?ups?)\b/.test(q) &&
      /\b(waiting|need|today|tasks?|to.?do|follow.?ups?|open|next|priority|what(?:'s| is)|update)\b/.test(q)) return true;
  return /\b(?:what(?:'s| is| do i have)?|show(?: me)?|any|which|list|how many|prioriti[sz]e)\s{0,5}(?:my|our|the|all)?\s{0,5}(?:to.?do(?: list)?|tasks?|workload|queue|things to do)\b/.test(q) ||
    /\bwhat (?:should|do) (?:i|we) (?:work on|do|need to do) today\b/.test(q);
}

function taskWriteIntent(question) {
  const q = String(question || "").toLowerCase();
  return /\b(add|create|make|assign|delegate|put|save)\b.{0,65}\b(task|to.?do|checklist|reminder|my list)\b/.test(q) ||
    /\b(have|ask|tell) (?:my )?(?:mom|mother|advisor|claude)\b.{0,70}\b(handle|work on|take care of|fix|follow up)\b/.test(q);
}

// Deliberate, internal-only task capture. Vague delegation, customer sends,
// approvals, and deadline/reminder instructions are not silently converted
// into an execution request. They need role/approval/time confirmation.
function taskCaptureTitle(question) {
  const q = String(question || "").trim();
  const patterns = [
    /^(?:please\s+)?(?:add|put)\s+(.+?)\s+(?:to|on)\s+(?:my|the|our)\s+(?:task(?:s)?|to.?do(?:\s+list)?|checklist|list)[.!]?$/i,
    /^(?:please\s+)?(?:create|make)\s+(?:a\s+)?(?:task|to.?do)\s+(?:to|for)\s+(.+?)[.!]?$/i,
  ];
  const match = patterns.map(p => q.match(p)).find(Boolean);
  if (!match) return null;
  const title = match[1].trim().replace(/^["'“”]+|["'“”]+$/g, "").trim();
  return title.length >= 4 && title.length <= 200 ? title : null;
}

function inferredProjectKey(title) {
  if (/\b(website|web site|preview|booking page)\b/i.test(title)) return "website";
  if (/\b(dashboard|imperial app|task engine|lead system)\b/i.test(title)) return "operations";
  if (/\b(finance|reconcil|quickbooks|ledger)\b/i.test(title)) return "finance";
  return null;
}

async function internalTaskSummary(snapshot, taskLister) {
  const queue = shopTaskSummary(snapshot);
  if (process.env.IMPERIAL_TASKS_ENABLED !== "true" || typeof taskLister !== "function") {
    return queue + "\nSaved to-do list not yet connected.";
  }
  try {
    const result = await taskLister(10);
    if (!Array.isArray(result?.tasks)) throw Error("Invalid task list");
    const tasks = result.tasks.map(t =>
      `#${Number(t.id)} ${String(t.title || "Untitled").replace(/[\r\n\t]/g, " ").slice(0, 85)} (${t.status})`);
    return [
      "Saved Imperial tasks:", ...(tasks.length ? tasks : ["None recorded."]),
      "Shop communication queue:",
      queue.replace("Imperial shop queue (current app snapshot):\n", ""),
    ].join("\n").slice(0, 900);
  } catch (_) {
    return queue + "\nSaved to-do list could not be verified; the task source may be unavailable.";
  }
}

function filteredTask(item) {
  return {
    id: Number(item?.id),
    who: String(item?.who || "Unknown").replace(/[\r\n\t]/g, " ").slice(0, 65),
    urgency: ["normal", "urgent", "safety"].includes(item?.urgency) ? item.urgency : "normal",
    nextStep: String(item?.nextStep || "Review the request").replace(/[\r\n\t]/g, " ").slice(0, 90),
  };
}

function shopTaskSummary(snapshot = {}) {
  const waiting = Array.isArray(snapshot.waitingOnYou) ? snapshot.waitingOnYou : null;
  if (!waiting) return "The Imperial dispatch snapshot is unavailable, so I can't verify your open shop tasks. I haven't marked anything completed.";
  const routine = Array.isArray(snapshot.otherOpenMessages) ? snapshot.otherOpenMessages : [];
  const approved = Array.isArray(snapshot.approvedAwaitingFollowUp) ? snapshot.approvedAwaitingFollowUp : [];
  const all = [
    ...waiting.map(x => ({...filteredTask(x), bucket: "needs owner"})),
    ...routine.map(x => ({...filteredTask(x), bucket: "inbox"})),
    ...approved.map(x => ({...filteredTask(x), bucket: "follow-up"})),
  ];
  all.sort((a, b) => ({ safety: 0, urgent: 1, normal: 2 }[a.urgency] -
                       { safety: 0, urgent: 1, normal: 2 }[b.urgency]));
  const lines = [
    "Imperial shop queue (current app snapshot):",
    `Needs owner: ${waiting.length}; inbox: ${routine.length}; approved follow-ups: ${approved.length}.`,
  ];
  for (const task of all.slice(0, 4)) {
    lines.push(`#${task.id} ${task.who} (${task.bucket}) - ${task.nextStep}`);
  }
  if (!all.length) lines.push("No items shown in this dispatch snapshot.");
  lines.push("This is the dispatch queue, not the full Quo, project or finance task list. Due dates not verified.");
  return lines.join("\n").slice(0, 900);
}

async function answerImperialBrain({
  question, snapshot, history = [], projectAnswer = answerProjectQuestion,
  developmentAnswer = answerDevelopmentTasks, capabilityAnswer = answerCapabilityQuestion,
  messageId = null, taskWriter = null, taskLister = null,
} = {}) {
  if (process.env.IMPERIAL_BRAIN_ENABLED !== "true") return null;
  const q = String(question || "").trim();
  if (!q) return null;
  const capability = capabilityAnswer({
    question: q, snapshot, projectStatusConfigured: !!process.env.PROJECT_STATUS_GITHUB_TOKEN,
  });
  if (capability) return capability;
  // Only a clear 'add this to my list' creates an internal task. A request
  // to contact customers, delegate or change software remains approval-gated.
  if (taskWriteIntent(q)) {
    const title = taskCaptureTitle(q);
    if (!title) return "I understand the assignment, but haven't created or assigned anything. I can save a specific internal task when you say 'Add [task] to my list'; delegation and external actions still need approval.";
    if (process.env.IMPERIAL_TASKS_ENABLED !== "true" || !messageId || typeof taskWriter !== "function") {
      return "I understood the to-do, but task storage hasn't been activated yet. Nothing was saved.";
    }
    try {
      const result = await taskWriter({ messageId, title, projectKey: inferredProjectKey(title) });
      if (!result?.recorded || !Number.isSafeInteger(result.id) || result.id < 1) throw Error("Unverified write");
      return `Saved to Imperial tasks as #${result.id}: ${result.title || title}. ${result.duplicate ? "(Already captured; no duplicate.)" : "Pending until verified complete."}`.slice(0, 900);
    } catch (_) {
      return "I couldn't verify that the task was saved. I have NOT marked it created. Check the task connection before trying again.";
    }
  }
  if (developmentTasksIntent(q)) return developmentAnswer({ question: q });
  // An explicit shop task question uses the current queue, not stale GitHub work.
  if (shopTasksIntent(q) && !/\b(website|github|development|claude|code|build|software project)\b/i.test(q)) {
    return internalTaskSummary(snapshot, taskLister);
  }
  // Project follow-ups use the actual source, not guessed chat context.
  if (projectTopic(q, history)) return projectAnswer({ question: q, history });
  return null; // Other customer/vehicle details stay on the existing owner assistant.
}

module.exports = {
  developmentTasksIntent, shopTasksIntent, taskWriteIntent,
  shopTaskSummary, taskCaptureTitle, inferredProjectKey, internalTaskSummary, answerImperialBrain,
};
