"use strict";

const phoenixDate = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/Phoenix", year: "numeric", month: "2-digit", day: "2-digit",
});
const phoenixDisplay = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/Phoenix", weekday: "short", month: "short", day: "numeric",
  hour: "numeric", minute: "2-digit",
});

function parsePhoenixDue(value, now) {
  const match = /^(today|tomorrow|\d{4}-\d{2}-\d{2}) at (\d{1,2})(?::(\d{2}))?\s*(am|pm)$/i.exec(value.trim());
  if (!match) return null;
  const [, date, hourText, minuteText = "00", period] = match;
  const hour = Number(hourText);
  const minute = Number(minuteText);
  if (hour < 1 || hour > 12 || minute > 59) return null;
  const parts = Object.fromEntries(phoenixDate.formatToParts(now).map(({ type, value }) => [type, value]));
  const calendarDate = date === "today" || date === "tomorrow"
    ? `${parts.year}-${parts.month}-${parts.day}` : date;
  const day = new Date(`${calendarDate}T00:00:00Z`);
  if (!Number.isFinite(day.valueOf()) || day.toISOString().slice(0, 10) !== calendarDate) return null;
  const days = date === "tomorrow" ? 1 : 0;
  const phoenixHour = hour % 12 + (period.toLowerCase() === "pm" ? 12 : 0);
  const due = new Date(day.valueOf() + ((days * 24 + phoenixHour + 7) * 60 + minute) * 60000);
  return due > now ? due.toISOString() : null;
}

function taskReceipt(task) {
  const due = task.dueAt ? ` · due ${phoenixDisplay.format(new Date(task.dueAt))} Phoenix time` : "";
  const duplicate = task.duplicate ? " · already saved" : "";
  return `Imperial task #${task.id} · ${task.status === "done_verified" ? "done" : task.status}${due}${duplicate}\n${task.title}`;
}

function recentTaskId(history) {
  const latestReply = [...history].reverse().find(turn => turn.role === "assistant");
  return /^Imperial task #(\d+) · /.exec(latestReply?.content || "")?.[1];
}

async function answerOwnerTaskCommand({
  question, messageId, history, taskWriter, taskChanger, taskReader, now = new Date(),
}) {
  const text = question.replace(/^imperial[,! ]+\s*/i, "").replace(/^please\s+/i, "").trim();
  const reminder = /^remind me\b/i.test(text);
  const lookup = /^(?:status of|show) task #?(\d+)[?.!]?$/i.exec(text);
  const change = /^(mark|complete|reopen|move|reschedule)\s+(?:task\s+#?(\d+)|(that))\b(.*)$/i.exec(text);
  if (!reminder && !change && !lookup) return null;
  if (process.env.IMPERIAL_TASKS_ENABLED !== "true" || !messageId) {
    return "Imperial task storage is unavailable. No task change was confirmed.";
  }

  const dateHelp = "Use a future Phoenix time, like tomorrow at 11 AM or 2026-11-20 at 2 PM.";
  try {
    if (lookup) return taskReceipt(await taskReader(lookup[1]));
    if (reminder) {
      const parts = /^remind me (.+?) to (.{4,200})$/i.exec(text);
      const dueAt = parts && parsePhoenixDue(parts[1].toLowerCase(), now);
      if (!dueAt) return `When should this task be due? ${dateHelp}`;
      const task = await taskWriter({ messageId, title: parts[2], dueAt });
      return task?.recorded ? taskReceipt(task) : "I couldn't confirm that the task was saved.";
    }

    const [, verb, explicitId, , tail] = change;
    const taskId = explicitId || recentTaskId(history);
    if (!taskId) return "Which Imperial task? Include its ID, like task #42.";
    const action = verb.toLowerCase();
    const suffix = tail.trim().replace(/[.!]$/, "").toLowerCase();
    let operation;
    let dueAt;
    if (action === "move" || action === "reschedule") {
      operation = "reschedule";
      dueAt = parsePhoenixDue(suffix.replace(/^to\s+/, "").toLowerCase(), now);
      if (!dueAt) return `When should task #${taskId} be due? ${dateHelp}`;
    } else if ((action === "mark" && suffix === "done") || (action === "complete" && !suffix)) {
      operation = "complete";
    } else if (action === "reopen" && !suffix) {
      operation = "reopen";
    } else {
      return "Try “mark task #42 done,” “reopen task #42,” or “move task #42 to tomorrow at 11 AM.”";
    }
    const task = await taskChanger(taskId, { messageId, operation, dueAt });
    return task?.id ? taskReceipt(task) : "I couldn't confirm the task change.";
  } catch (_) {
    return "I couldn't confirm the task change. Check Master Tasks for its current status.";
  }
}

module.exports = { answerOwnerTaskCommand, parsePhoenixDue, taskReceipt };
