// Deterministic checks around owner texts, run before and after the AI.
//
// - A text reporting that the app can't do something is a Fix list entry, not a
//   question for the AI. The reply quotes the saved ID only when the shop app
//   confirms it was stored.
// - Approval numbers (#111), repair orders (RO #8) and tasks (task #118) are
//   different records. An answer must not attach an approval number the owner
//   didn't name to a different customer.

const APP_SUBJECT = /\b(app|dashboard|site|page|screen|button|option|tab|upload|uploading|photo|photos|picture|pictures|attach|mark|complete|completion|link|form|menu|setting|settings|imperial|system)\b/i;
const APP_FAILURE = /\b(not able to|unable to|can'?t|cannot|can not|won'?t let|doesn'?t let|isn'?t letting|no (?:option|way|button|place)|there'?s no|missing|broken|doesn'?t work|does not work|isn'?t working|not working|glitch|bug|error|stuck|disappeared|gone)\b/i;
const NOT_A_REPORT = /^\s*(yes|no|done|later|edit|old|stop|resume|send)\b/i;

function isAppDefectReport(text) {
  const value = String(text || "");
  if (value.length < 25 || NOT_A_REPORT.test(value)) return false;
  return APP_SUBJECT.test(value) && APP_FAILURE.test(value);
}

function focusLine(focus) {
  const ro = focus && focus.ro;
  if (!ro) return "";
  return `${ro.label}, ${ro.customer}${ro.vehicle ? ` (${ro.vehicle})` : ""}`;
}

function defectReply(saved, focus) {
  if (!saved || !saved.id) {
    return "I couldn't save that to the Fix list, so it isn't recorded yet. Text it again or use Report a problem in the dashboard.";
  }
  const where = focusLine(focus);
  const status = saved.status === "open" ? "open" : String(saved.status || "saved").replace(/_/g, " ");
  return `Saved to the Fix list as #${saved.id} (${status})${where ? ` for ${where}` : ""}. Nothing on the job was changed.`;
}

// Keep only recent turns that belong to the record this text is about, so an
// earlier topic (another customer's approval number) doesn't carry over.
const ITEM_NUMBER = /(?:^|[\s(])#?\s*\d{2,5}\b/;

function historyForFocus(history, focus, question) {
  if (!focus || !focus.ro || ITEM_NUMBER.test(question || "")) return history;
  const name = String(focus.ro.firstName || "").toLowerCase();
  return history.filter((turn) => !ITEM_NUMBER.test(turn.content) || (name && turn.content.toLowerCase().includes(name)));
}

// Removes "#N" approval references the owner did not name when that item
// belongs to someone other than the record in focus, and labels the RO.
// Only bare "#N" and "approval #N" are approval references: "RO #8", "task #118",
// "Fix list #23", "invoice #4" are other records and are never relabelled.
const OTHER_TYPE = /(?:\bRO|\bR\.O\.?|\brepair order|\bwork order|\bjob|\btasks?|\bto-?do|\bfix(?: list)?|\breport|\bproblem|\binvoice|\bestimate|\bappointment|\bappt)\s*(?:number|no\.?|num)?\s*$/i;
const NAMED_NUMBER = /(?:#\s*|\b(?:approval|item|task|ro|job|fix(?: list)?|invoice|estimate)\s*(?:number|no\.?|#)?\s*)(\d{1,5})\b/gi;

function namedNumbers(question) {
  return new Set([...String(question || "").matchAll(NAMED_NUMBER)].map((m) => Number(m[1])));
}

function guardAnswer(answer, { focus, snapshot = {}, question = "" }) {
  const text = String(answer || "");
  const ro = focus && focus.ro;
  if (!ro) return text;
  const named = namedNumbers(question);
  const items = [...(snapshot.waitingOnYou || []), ...(snapshot.otherOpenMessages || []), ...(snapshot.approvedAwaitingFollowUp || [])];
  const first = String(ro.firstName || "").toLowerCase();
  return text.replace(/(approval\s+)?#\s*(\d+)(\s+with\s+[A-Z][a-z]+)?/g, (match, approval, num, withName, offset) => {
    const id = Number(num);
    if (!approval && OTHER_TYPE.test(text.slice(Math.max(0, offset - 20), offset))) return match; // another record type
    if (named.has(id)) return match;
    const item = items.find((i) => i.id === id);
    const belongsElsewhere = !item || !first || !String(item.who || "").toLowerCase().includes(first);
    if (!belongsElsewhere) return `approval #${id}${withName || ""}`;
    return `${ro.label}${withName || ""}`;
  });
}

module.exports = { isAppDefectReport, defectReply, historyForFocus, guardAnswer, focusLine };
