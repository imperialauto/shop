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
    \`Needs owner: \${waiting.length}; inbox: \${routine.length}; approved follow-ups: \${approved.length}.\`,
  ];
  for (const task of all.slice(0, 4)) {
    lines.push(\`#\${task.id} \${task.who} (\${task.bucket}) - \${task.nextStep}\`);
  }
  if (!all.length) lines.push("No items shown in this dispatch snapshot.");
  lines.push("This is the dispatch queue, not the full Quo, project or finance task list. Due dates not verified.");
  return lines.join("\n").slice(0, 900);
}

async function answerImperialBrain({
  question, snapshot, history = [], projectAnswer = answerProjectQuestion,
  developmentAnswer = answerDevelopmentTasks, capabilityAnswer = answerCapabilityQuestion,
} = {}) {
  if (process.env.IMPERIAL_BRAIN_ENABLED !== "true") return null;
  const q = String(question || "").trim();
  if (!q) return null;
  const capability = capabilityAnswer({
    question: q, snapshot, projectStatusConfigured: !!process.env.PROJECT_STATUS_GITHUB_TOKEN,
  });
  if (capability) return capability;
  // Never pretend that an unimplemented task action was saved.
  if (taskWriteIntent(q)) {
    return "I understand the assignment, but the unified task write/verification service isn't active yet. I haven't created or assigned anything. I'll need that connected before I can confirm completion.";
  }
  if (developmentTasksIntent(q)) return developmentAnswer({ question: q });
  // An explicit shop task question uses the current queue, not stale GitHub work.
  if (shopTasksIntent(q) && !/\b(website|github|development|claude|code|build|software project)\b/i.test(q)) {
    return shopTaskSummary(snapshot);
  }
  // Project follow-ups use the actual source, not guessed chat context.
  if (projectTopic(q, history)) return projectAnswer({ question: q, history });
  return null; // Other customer/vehicle details stay on the existing owner assistant.
}

module.exports = {
  developmentTasksIntent, shopTasksIntent, taskWriteIntent,
  shopTaskSummary, answerImperialBrain,
};
