/* Reply desk for the Needs-you queue (Home and the Needs-you page).
   - [data-queue-refresh]: re-reads the rows every 25 s so new texts and fresh drafts appear without a reload.
     A row the owner is working in (typed, dictating, or focused) is never replaced; it gets a
     "new text came in" note instead, and Send on the old row is refused by the server anyway.
   - Dictate: the browser's own speech input fills the rough box, where the browser supports it.
   - Clean up: rewrites the rough box into a clean text in the reply box.
   Nothing here sends a text. Only the form's Send button does. */
(function () {
  "use strict";
  var REFRESH_MS = 25000;
  var Speech = window.SpeechRecognition || window.webkitSpeechRecognition;

  function rowOf(el) { return el && el.closest ? el.closest("[data-queue-item]") : null; }
  function markBusy(el) { var row = rowOf(el); if (row) row.setAttribute("data-busy", "1"); }

  function busy(row) {
    if (!row) return false;
    if (row.getAttribute("data-busy") === "1" || row.getAttribute("data-listening") === "1") return true;
    return !!(document.activeElement && row.contains(document.activeElement) &&
              /^(TEXTAREA|INPUT|SELECT)$/.test(document.activeElement.tagName));
  }

  document.addEventListener("input", function (event) {
    if (event.target.matches("[data-reply-box], [data-rough-box]")) markBusy(event.target);
  });
  // A quick-reply chip fills the box too, so the row is being worked on.
  document.addEventListener("click", function (event) {
    if (event.target.closest("[data-quick-reply]")) markBusy(event.target);
  });

  // ── Dictate ──────────────────────────────────────────────────────────────
  function setupMics(root) {
    if (!Speech) return;
    root.querySelectorAll("[data-mic]").forEach(function (button) { button.hidden = false; });
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-mic]");
    if (!button || !Speech) return;
    var row = rowOf(button);
    var box = row && row.querySelector("[data-rough-box]");
    var status = row && row.querySelector("[data-rewrite-status]");
    if (!box) return;
    if (button._recognizer) { button._recognizer.stop(); return; }
    var recognizer = new Speech();
    recognizer.lang = "en-US";
    recognizer.interimResults = false;
    recognizer.continuous = false;
    var before = box.value ? box.value.replace(/\s+$/, "") + " " : "";
    recognizer.onresult = function (e) {
      var said = "";
      for (var i = 0; i < e.results.length; i++) said += e.results[i][0].transcript;
      box.value = before + said.trim();
      markBusy(box);
    };
    recognizer.onerror = function (e) {
      if (status) status.textContent = e.error === "not-allowed" ? "Microphone is blocked for this site." : "Didn't catch that. Try again or type it.";
    };
    recognizer.onend = function () {
      button._recognizer = null;
      button.setAttribute("aria-pressed", "false");
      button.textContent = "Dictate";
      if (row) row.removeAttribute("data-listening");
      if (status && status.textContent === "Listening…") status.textContent = box.value ? "Got it. Tap Clean up." : "";
    };
    button._recognizer = recognizer;
    button.setAttribute("aria-pressed", "true");
    button.textContent = "Stop";
    row.setAttribute("data-listening", "1");
    markBusy(button);
    if (status) status.textContent = "Listening…";
    recognizer.start();
  });

  // ── Clean up ─────────────────────────────────────────────────────────────
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-clean-up]");
    if (!button) return;
    var row = rowOf(button);
    var rough = row && row.querySelector("[data-rough-box]");
    var reply = row && row.querySelector("[data-reply-box]");
    var status = row && row.querySelector("[data-rewrite-status]");
    if (!rough || !reply) return;
    if (!rough.value.trim()) { if (status) status.textContent = "Type or dictate what you want to say first."; return; }
    if (!window.fetch || !window.FormData) return;
    var body = new FormData();
    body.append("rough", rough.value);
    button.disabled = true;
    if (status) status.textContent = "Cleaning up…";
    markBusy(button);
    fetch(button.getAttribute("data-clean-up"), {
      method: "POST", body: body, credentials: "same-origin", headers: { Accept: "application/json" }
    })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (data) {
        reply.value = data.text || reply.value;
        reply.focus();
        if (status) status.textContent = "Done. Check it, then tap Send. Nothing has been sent.";
      })
      .catch(function () { if (status) status.textContent = "Couldn't clean it up. Your words are still in the box."; })
      .then(function () { button.disabled = false; });
  });

  // ── Live refresh ─────────────────────────────────────────────────────────
  function refresh(container) {
    if (document.hidden || container.getAttribute("data-refreshing") === "1") return;
    container.setAttribute("data-refreshing", "1");
    fetch(container.getAttribute("data-queue-refresh"), { credentials: "same-origin", headers: { Accept: "text/html" } })
      .then(function (r) {
        if (!r.ok) throw new Error(r.status);
        var stamp = r.headers.get("X-Queue-Stamp");
        if (stamp && stamp === container.getAttribute("data-stamp")) return null;
        container.setAttribute("data-stamp", stamp || "");
        return r.text();
      })
      .then(function (html) { if (html !== null) swap(container, html); })
      .catch(function () { /* keep what's on screen; try again next round */ })
      .then(function () { container.removeAttribute("data-refreshing"); });
  }

  function swap(container, html) {
    var holder = document.createElement("div");
    holder.innerHTML = html;
    var old = {};
    container.querySelectorAll("[data-queue-item]").forEach(function (row) { old[row.getAttribute("data-queue-item")] = row; });
    holder.querySelectorAll("[data-queue-item]").forEach(function (fresh) {
      var previous = old[fresh.getAttribute("data-queue-item")];
      if (!previous) return;
      if (busy(previous)) {
        if (previous.getAttribute("data-message-id") !== fresh.getAttribute("data-message-id")) flagNewer(previous, fresh);
        fresh.parentNode.replaceChild(previous, fresh); // keep his edits on screen
      } else if (previous.open) {
        fresh.open = true;
      }
    });
    // Busy rows that dropped out of the list (answered elsewhere) stay until he's done with them.
    Object.keys(old).forEach(function (id) {
      if (busy(old[id]) && !holder.querySelector('[data-queue-item="' + id + '"]')) holder.appendChild(old[id]);
    });
    while (container.firstChild) container.removeChild(container.firstChild);
    while (holder.firstChild) container.appendChild(holder.firstChild);
    setupMics(container);
  }

  function flagNewer(row, fresh) {
    var note = row.querySelector("[data-newer-note]");
    if (!note) {
      note = document.createElement("p");
      note.setAttribute("data-newer-note", "");
      note.setAttribute("role", "status");
      note.className = "pill pill-warn";
      note.style.cssText = "display:block; margin:0 16px 8px;";
      var summary = row.querySelector("summary");
      if (summary && summary.nextSibling) row.insertBefore(note, summary.nextSibling); else row.appendChild(note);
    }
    note.textContent = "The customer texted again. Your edit is kept, but Send will ask you to look at the new text first. ";
    var load = document.createElement("button");
    load.type = "button";
    load.className = "qchip";
    load.textContent = "Load the new draft";
    load.addEventListener("click", function () { row.parentNode.replaceChild(fresh, row); setupMics(fresh); });
    note.appendChild(load);
  }

  setupMics(document);
  var containers = document.querySelectorAll("[data-queue-refresh]");
  if (containers.length && window.fetch) {
    setInterval(function () { containers.forEach(refresh); }, REFRESH_MS);
    document.addEventListener("visibilitychange", function () { if (!document.hidden) containers.forEach(refresh); });
  }
})();
