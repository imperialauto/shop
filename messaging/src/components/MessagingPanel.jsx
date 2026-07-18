// ─────────────────────────────────────────────────────────
// MessagingPanel — Quo texting inbox for the Cowork dashboard.
//
// Two-pane layout: conversation list on the left, thread + AI draft on
// the right. Talks only to the messagesApi.js endpoints:
//   GET  /api/messages/conversations?status=OPEN|DONE
//   GET  /api/messages/conversations/:id
//   POST /api/messages/conversations/:id/draft
//   POST /api/messages/conversations/:id/send
//   POST /api/messages/conversations/:id/status
//
// IMPORTANT: nothing in this component sends a text on its own. The
// compose box is always a manual action — clicking "Send" is the ONLY
// thing that calls the /send endpoint, whether the text came from an AI
// draft or was typed from scratch. AI drafts are review-only.
//
// Self-contained: ships its own <style>, no Tailwind / CSS file / UI kit
// dependency required. Drop this file in as-is; if your dashboard already
// uses Tailwind you're welcome to swap the classes, but it isn't required.
// ─────────────────────────────────────────────────────────

import { useState, useEffect, useRef, useCallback } from "react";

const API_BASE = "/api/messages";
const POLL_MS = 15000;

function formatRelativeTime(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  const diffSec = Math.round((Date.now() - date.getTime()) / 1000);
  if (diffSec < 45) return "just now";
  const diffMin = Math.round(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.round(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  const diffDay = Math.round(diffHr / 24);
  if (diffDay < 7) return `${diffDay}d ago`;
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function formatClockTime(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

async function apiFetch(path, options) {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data?.error || `Request failed (${res.status})`);
  return data;
}

export default function MessagingPanel() {
  const [conversations, setConversations] = useState([]);
  const [filter, setFilter] = useState("OPEN"); // OPEN | DONE | ALL
  const [listLoading, setListLoading] = useState(true);
  const [listError, setListError] = useState(null);

  const [selectedId, setSelectedId] = useState(null);
  const [thread, setThread] = useState(null);
  const [threadLoading, setThreadLoading] = useState(false);
  const [threadError, setThreadError] = useState(null);

  const [composeText, setComposeText] = useState("");
  const [activeDraftId, setActiveDraftId] = useState(null);
  const [sending, setSending] = useState(false);
  const [regenerating, setRegenerating] = useState(false);
  const [updatingStatus, setUpdatingStatus] = useState(false);
  const [actionError, setActionError] = useState(null);

  const messagesEndRef = useRef(null);
  const pollRef = useRef(null);

  const loadConversations = useCallback(async () => {
    try {
      const qs = filter === "ALL" ? "" : `?status=${filter}`;
      const data = await apiFetch(`/conversations${qs}`);
      setConversations(data);
      setListError(null);
    } catch (err) {
      setListError(err.message);
    } finally {
      setListLoading(false);
    }
  }, [filter]);

  const loadThread = useCallback(async (id, { silent } = {}) => {
    if (!id) return;
    if (!silent) setThreadLoading(true);
    try {
      const data = await apiFetch(`/conversations/${id}`);
      setThread(data);
      setThreadError(null);
      // Only prefill the compose box on a fresh (non-silent) load — a
      // silent poll refresh shouldn't stomp on text the coordinator is
      // mid-edit on.
      if (!silent) {
        const pendingDraft = data.drafts?.[0];
        setComposeText(pendingDraft?.body || "");
        setActiveDraftId(pendingDraft?.id || null);
      }
    } catch (err) {
      if (!silent) setThreadError(err.message);
    } finally {
      if (!silent) setThreadLoading(false);
    }
  }, []);

  // Initial + filter-change load, then poll the list.
  useEffect(() => {
    setListLoading(true);
    loadConversations();
    const t = setInterval(loadConversations, POLL_MS);
    return () => clearInterval(t);
  }, [loadConversations]);

  // Load the selected thread, then poll it quietly for new inbound texts.
  useEffect(() => {
    if (!selectedId) {
      setThread(null);
      return;
    }
    loadThread(selectedId);
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = setInterval(() => loadThread(selectedId, { silent: true }), POLL_MS);
    return () => clearInterval(pollRef.current);
  }, [selectedId, loadThread]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ block: "end" });
  }, [thread?.messages?.length]);

  function handleSelect(id) {
    setActionError(null);
    setSelectedId(id);
  }

  async function handleSend() {
    if (!selectedId || !composeText.trim() || sending) return;
    setSending(true);
    setActionError(null);
    try {
      await apiFetch(`/conversations/${selectedId}/send`, {
        method: "POST",
        body: JSON.stringify({ draftId: activeDraftId || undefined, body: composeText.trim() }),
      });
      setComposeText("");
      setActiveDraftId(null);
      await Promise.all([loadThread(selectedId), loadConversations()]);
    } catch (err) {
      setActionError(err.message);
    } finally {
      setSending(false);
    }
  }

  async function handleRegenerate() {
    if (!selectedId || regenerating) return;
    setRegenerating(true);
    setActionError(null);
    try {
      const draft = await apiFetch(`/conversations/${selectedId}/draft`, { method: "POST" });
      setComposeText(draft.body || "");
      setActiveDraftId(draft.id);
    } catch (err) {
      setActionError(err.message);
    } finally {
      setRegenerating(false);
    }
  }

  async function handleToggleStatus() {
    if (!selectedId || !thread || updatingStatus) return;
    const nextStatus = thread.status === "DONE" ? "OPEN" : "DONE";
    setUpdatingStatus(true);
    setActionError(null);
    try {
      await apiFetch(`/conversations/${selectedId}/status`, {
        method: "POST",
        body: JSON.stringify({ status: nextStatus }),
      });
      setThread((prev) => (prev ? { ...prev, status: nextStatus } : prev));
      loadConversations();
    } catch (err) {
      setActionError(err.message);
    } finally {
      setUpdatingStatus(false);
    }
  }

  const hasPendingDraft = Boolean(activeDraftId);

  return (
    <div className="qm-root">
      <style>{QM_STYLES}</style>

      <aside className="qm-sidebar">
        <div className="qm-sidebar-header">
          <h2 className="qm-title">Messages</h2>
          <div className="qm-tabs">
            {["OPEN", "DONE", "ALL"].map((f) => (
              <button
                key={f}
                type="button"
                className={`qm-tab ${filter === f ? "qm-tab-active" : ""}`}
                onClick={() => setFilter(f)}
              >
                {f === "OPEN" ? "Open" : f === "DONE" ? "Done" : "All"}
              </button>
            ))}
          </div>
        </div>

        {listError && <div className="qm-error">{listError}</div>}
        {listLoading ? (
          <div className="qm-empty">Loading…</div>
        ) : conversations.length === 0 ? (
          <div className="qm-empty">No conversations</div>
        ) : (
          <ul className="qm-conv-list">
            {conversations.map((c) => (
              <li key={c.id}>
                <button
                  type="button"
                  className={`qm-conv-item ${selectedId === c.id ? "qm-conv-item-active" : ""}`}
                  onClick={() => handleSelect(c.id)}
                >
                  <div className="qm-conv-row">
                    <span className="qm-conv-name">{c.customerName || c.customerPhone}</span>
                    <span className="qm-conv-time">{formatRelativeTime(c.lastMessage?.quoCreatedAt)}</span>
                  </div>
                  <div className="qm-conv-row">
                    <span className="qm-conv-preview">
                      {c.lastMessage?.direction === "OUTBOUND" ? "You: " : ""}
                      {c.lastMessage?.body || "(no messages)"}
                    </span>
                    <span className="qm-conv-badges">
                      {c.hasPendingDraft && <span className="qm-badge qm-badge-draft">Draft</span>}
                      {c.unreadCount > 0 && <span className="qm-badge qm-badge-unread">{c.unreadCount}</span>}
                    </span>
                  </div>
                </button>
              </li>
            ))}
          </ul>
        )}
      </aside>

      <main className="qm-thread">
        {!selectedId ? (
          <div className="qm-empty qm-empty-main">Select a conversation</div>
        ) : threadLoading && !thread ? (
          <div className="qm-empty qm-empty-main">Loading…</div>
        ) : threadError ? (
          <div className="qm-error qm-error-main">{threadError}</div>
        ) : thread ? (
          <>
            <div className="qm-thread-header">
              <div>
                <div className="qm-thread-name">{thread.customerName || thread.customerPhone}</div>
                {thread.customerName && <div className="qm-thread-phone">{thread.customerPhone}</div>}
              </div>
              <button type="button" className="qm-btn qm-btn-secondary" onClick={handleToggleStatus} disabled={updatingStatus}>
                {thread.status === "DONE" ? "Reopen" : "Mark as done"}
              </button>
            </div>

            <div className="qm-messages">
              {thread.messages.length === 0 && <div className="qm-empty">No messages yet</div>}
              {thread.messages.map((m) => (
                <div key={m.id} className={`qm-bubble-row ${m.direction === "OUTBOUND" ? "qm-row-out" : "qm-row-in"}`}>
                  <div className={`qm-bubble ${m.direction === "OUTBOUND" ? "qm-bubble-out" : "qm-bubble-in"}`}>
                    {m.body && <div className="qm-bubble-body">{m.body}</div>}
                    {(m.mediaUrls || []).map((url) => (
                      <a key={url} href={url} target="_blank" rel="noreferrer" className="qm-attachment">
                        📎 Attachment
                      </a>
                    ))}
                    <div className="qm-bubble-meta">
                      {formatClockTime(m.quoCreatedAt)}
                      {m.direction === "OUTBOUND" && m.quoStatus ? ` · ${m.quoStatus}` : ""}
                    </div>
                  </div>
                </div>
              ))}
              <div ref={messagesEndRef} />
            </div>

            {actionError && <div className="qm-error qm-error-compose">{actionError}</div>}

            <div className="qm-compose">
              {hasPendingDraft && <div className="qm-draft-label">AI draft — review before sending</div>}
              <textarea
                className="qm-textarea"
                rows={3}
                value={composeText}
                onChange={(e) => setComposeText(e.target.value)}
                placeholder="Type a reply…"
              />
              <div className="qm-compose-actions">
                <button
                  type="button"
                  className="qm-btn qm-btn-secondary"
                  onClick={handleRegenerate}
                  disabled={regenerating}
                >
                  {regenerating ? "Regenerating…" : "Regenerate draft"}
                </button>
                <button
                  type="button"
                  className="qm-btn qm-btn-primary"
                  onClick={handleSend}
                  disabled={sending || !composeText.trim()}
                >
                  {sending ? "Sending…" : "Send"}
                </button>
              </div>
            </div>
          </>
        ) : null}
      </main>
    </div>
  );
}

const QM_STYLES = `
.qm-root {
  display: flex;
  height: 100%;
  min-height: 480px;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  border: 1px solid #e2e2e7;
  border-radius: 10px;
  overflow: hidden;
  background: #fff;
  color: #1a1a1f;
}
.qm-sidebar { width: 300px; flex-shrink: 0; border-right: 1px solid #e2e2e7; display: flex; flex-direction: column; background: #fafafc; }
.qm-sidebar-header { padding: 14px 14px 8px; border-bottom: 1px solid #ececef; }
.qm-title { margin: 0 0 8px; font-size: 15px; font-weight: 600; }
.qm-tabs { display: flex; gap: 4px; }
.qm-tab { border: none; background: transparent; padding: 5px 10px; border-radius: 6px; font-size: 12.5px; cursor: pointer; color: #6b6b76; }
.qm-tab:hover { background: #ececef; }
.qm-tab-active { background: #1a1a1f; color: #fff; }
.qm-tab-active:hover { background: #1a1a1f; }
.qm-conv-list { list-style: none; margin: 0; padding: 4px; overflow-y: auto; flex: 1; }
.qm-conv-item { width: 100%; text-align: left; border: none; background: transparent; padding: 10px 10px; border-radius: 8px; cursor: pointer; display: block; margin-bottom: 2px; }
.qm-conv-item:hover { background: #f0f0f3; }
.qm-conv-item-active { background: #eef0ff; }
.qm-conv-row { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
.qm-conv-name { font-size: 13.5px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.qm-conv-time { font-size: 11px; color: #9a9aa4; flex-shrink: 0; }
.qm-conv-preview { font-size: 12.5px; color: #6b6b76; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1; }
.qm-conv-badges { display: flex; gap: 4px; flex-shrink: 0; }
.qm-badge { font-size: 10.5px; font-weight: 600; padding: 1px 6px; border-radius: 999px; }
.qm-badge-draft { background: #eef0ff; color: #4c4cd6; }
.qm-badge-unread { background: #1a1a1f; color: #fff; }
.qm-thread { flex: 1; display: flex; flex-direction: column; min-width: 0; }
.qm-thread-header { display: flex; justify-content: space-between; align-items: center; padding: 12px 16px; border-bottom: 1px solid #ececef; }
.qm-thread-name { font-size: 14.5px; font-weight: 600; }
.qm-thread-phone { font-size: 12px; color: #9a9aa4; }
.qm-messages { flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 8px; }
.qm-bubble-row { display: flex; }
.qm-row-out { justify-content: flex-end; }
.qm-row-in { justify-content: flex-start; }
.qm-bubble { max-width: 70%; padding: 8px 12px; border-radius: 14px; }
.qm-bubble-in { background: #f0f0f3; border-bottom-left-radius: 4px; }
.qm-bubble-out { background: #4c4cd6; color: #fff; border-bottom-right-radius: 4px; }
.qm-bubble-body { font-size: 13.5px; line-height: 1.4; white-space: pre-wrap; word-break: break-word; }
.qm-attachment { display: block; font-size: 12px; margin-top: 4px; color: inherit; }
.qm-bubble-meta { font-size: 10.5px; opacity: 0.65; margin-top: 4px; }
.qm-compose { border-top: 1px solid #ececef; padding: 12px 16px; }
.qm-draft-label { font-size: 11.5px; font-weight: 600; color: #4c4cd6; margin-bottom: 6px; }
.qm-textarea { width: 100%; box-sizing: border-box; border: 1px solid #d8d8de; border-radius: 8px; padding: 8px 10px; font-size: 13.5px; font-family: inherit; resize: vertical; }
.qm-textarea:focus { outline: none; border-color: #4c4cd6; }
.qm-compose-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 8px; }
.qm-btn { border: none; border-radius: 8px; padding: 7px 14px; font-size: 12.5px; font-weight: 600; cursor: pointer; }
.qm-btn:disabled { opacity: 0.5; cursor: default; }
.qm-btn-secondary { background: #ececef; color: #1a1a1f; }
.qm-btn-secondary:hover:not(:disabled) { background: #e2e2e7; }
.qm-btn-primary { background: #4c4cd6; color: #fff; }
.qm-btn-primary:hover:not(:disabled) { background: #3f3fc0; }
.qm-empty { padding: 24px 16px; text-align: center; font-size: 12.5px; color: #9a9aa4; }
.qm-empty-main { flex: 1; display: flex; align-items: center; justify-content: center; }
.qm-error { margin: 8px 12px; padding: 8px 10px; background: #fdeceb; color: #b3261e; border-radius: 6px; font-size: 12px; }
.qm-error-main { margin: 16px; }
.qm-error-compose { margin: 0 0 8px; }
`;
