// Keeps Imperial's Master Tasks in step with Quo's task list.
// Reads every Quo task page, posts them to the shop app, then marks done in Quo
// the tasks the owner finished in Imperial. Never sends a text or deletes a task.

const DEFAULT_INTERVAL_MINUTES = 5;
const MAX_PAGES = 20;
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function runQuoTaskSync({ quo, shopClient, pauseMs = 300 }) {
  const tasks = [];
  let pageToken;
  for (let page = 0; page < MAX_PAGES; page += 1) {
    const result = await quo.listTasks({ maxResults: 100, pageToken });
    tasks.push(...(result?.data || []));
    pageToken = result?.nextPageToken;
    if (!pageToken) break;
    await pause(pauseMs); // stay under Quo's rate limit
  }
  const saved = await shopClient.syncQuoTasks(tasks);
  const completed = [];
  for (const taskId of saved?.complete_in_quo || []) {
    try {
      await quo.completeTask(taskId);
      completed.push(taskId);
    } catch (err) {
      // 404: deleted in Quo, nothing left to finish.
      if (err.status === 404) completed.push(taskId);
      else console.error(`Quo task ${taskId} not marked done: ${err.status || ""} ${err.message}`.trim());
    }
    await pause(pauseMs);
  }
  if (completed.length) await shopClient.ackQuoTasks(completed);
  const summary = { read: tasks.length, added: saved?.added || 0, closed: saved?.closed || 0, completedInQuo: completed.length };
  return summary;
}

function startQuoTaskSync(options) {
  const minutes = Number(process.env.QUO_TASK_SYNC_MINUTES) || DEFAULT_INTERVAL_MINUTES;
  const tick = () => runQuoTaskSync(options)
    .then((s) => { if (s.added || s.closed || s.completedInQuo) console.log(`quo_task_sync ${JSON.stringify(s)}`); })
    .catch((err) => console.error("Quo task sync failed:", err.message));
  tick();
  return setInterval(tick, minutes * 60 * 1000);
}

module.exports = { runQuoTaskSync, startQuoTaskSync };
