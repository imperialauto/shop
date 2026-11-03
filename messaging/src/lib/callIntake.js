// Turns missed calls and voicemails into queue items, and gives drafts the
// recent call context for that same phone number. Reads Quo only.

const NOT_LEADS = /google (business|listing)|business listing|business insurance|past due|delinquent|your (car'?s )?extended warranty/i;

function isMissed(call) {
  if (call.direction !== "incoming") return false;
  if (["missed", "no-answer", "abandoned"].includes(call.status)) return true;
  return call.status === "completed" && !call.duration && !call.answeredAt;
}

function isTollFree(phone) {
  return /^\+?1?8(00|33|44|55|66|77|88)/.test(String(phone || ""));
}

function callBody(voicemail) {
  const text = voicemail?.transcript?.trim();
  if (text) return `Voicemail: ${text}`;
  return voicemail ? "Left a voicemail with no transcript." : "Missed call, no voicemail.";
}

async function missedCall({ quo, phone, phoneNumberId, since, skip = new Set() }) {
  if (skip.has(String(phone).replace(/\D/g, "").slice(-10))) return null;
  const { data = [] } = await quo.listCalls({ phoneNumberId, participants: [phone], createdAfter: since, maxResults: 10 });
  const newest = data.filter(isMissed).sort((a, b) => new Date(b.createdAt) - new Date(a.createdAt))[0];
  if (!newest) return null;
  const voicemail = await quo.getVoicemail(newest.id).catch(() => null);
  if (!voicemail && isTollFree(phone)) return null;
  const body = callBody(voicemail);
  return NOT_LEADS.test(body) ? null : { id: newest.id, body };
}

async function callContext({ quo, phone, phoneNumberId }) {
  try {
    const { data = [] } = await quo.listCalls({ phoneNumberId, participants: [phone], maxResults: 3 });
    const lines = [];
    for (const call of data) {
      const voicemail = await quo.getVoicemail(call.id).catch(() => null);
      if (voicemail?.transcript) lines.push(`Voicemail ${call.createdAt}: ${voicemail.transcript}`);
      const transcript = await quo.getTranscript(call.id).catch(() => null);
      const spoken = (transcript?.dialogue || []).map((d) => d.content).filter(Boolean).join(" ");
      if (spoken) lines.push(`Call ${call.createdAt}: ${spoken.slice(0, 1200)}`);
    }
    return lines.join("\n") || null;
  } catch (err) {
    console.error("Call context failed:", err.message);
    return null;
  }
}

module.exports = { missedCall, callContext, isMissed };
