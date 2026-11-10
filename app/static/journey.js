/* Imperial first-party journey analytics. No text input, URL, cookies or signature capture. */
(function () {
  "use strict";
  if (navigator.globalPrivacyControl || navigator.doNotTrack === "1") return;
  if (!window.crypto || !window.crypto.getRandomValues) return;

  var path = location.pathname;
  function pageForPath(p) {
    if (/^\/sign\/ro\/[^/]+$/.test(p)) return "approval";
    if (/^\/status\/[^/]+$/.test(p)) return "status";
    if (p === "/site" || p === "/site/") return "home";
    if (p === "/site/services") return "services";
    if (p.indexOf("/site/services/") === 0) return "service";
    var last = p.replace(/^\/site\/?/, "").split("/")[0];
    var allowed = ["book", "estimate", "roadside", "contact", "pricing", "faq", "rentals"];
    if (allowed.indexOf(last) >= 0) return last;
    if (last === "status") return "status_lookup";
    return "";
  }

  var page = pageForPath(path);
  if (!page) return;
  var context = document.querySelector('meta[name="imperial-journey-context"]');
  var signed = context ? context.getAttribute("content") : "";
  if ((page === "status" || page === "approval") && !signed) return;

  var sid;
  try {
    sid = sessionStorage.getItem("imperial-journey-tab-v1");
    if (!/^[a-f0-9]{32}$/.test(sid || "")) {
      var bytes = new Uint8Array(16);
      crypto.getRandomValues(bytes);
      sid = Array.from(bytes, function (b) { return b.toString(16).padStart(2, "0"); }).join("");
      sessionStorage.setItem("imperial-journey-tab-v1", sid);
    }
  } catch (_) { return; }

  var seen = Object.create(null);
  function emit(event, step, overridePage, once) {
    var p = overridePage || page;
    var key = p + "/" + event + "/" + step;
    if (once && seen[key]) return;
    if (once) seen[key] = true;
    var body = JSON.stringify({ sid: sid, page: p, event: event, step: step, context: signed || undefined });
    try {
      if (navigator.sendBeacon) {
        navigator.sendBeacon("/journey/event", new Blob([body], { type: "application/json" }));
      } else {
        fetch("/journey/event", { method: "POST", headers: { "Content-Type": "application/json" },
          body: body, credentials: "same-origin", keepalive: true }).catch(function () {});
      }
    } catch (_) { /* Never interfere with the customer's task. */ }
  }

  function stepFor(el) {
    var name = (el && el.name || "").toLowerCase();
    if (name.indexOf("line_") === 0) return "decision";
    if (["service", "service_line", "vehicle_class", "axles"].indexOf(name) >= 0) return "service";
    if (["slot_id"].indexOf(name) >= 0) return "time";
    if (["where", "zip", "street", "location"].indexOf(name) >= 0) return "location";
    if (["first_name", "last_name", "phone", "email", "contact_method", "reference", "last_name"].indexOf(name) >= 0) return "contact";
    if (["vin", "make", "model", "year", "mileage"].indexOf(name) >= 0) return "vehicle";
    if (["notes", "description", "situation"].indexOf(name) >= 0) return "details";
    if (["approved_by", "signature_data"].indexOf(name) >= 0) return "signature";
    return "other";
  }

  emit("view", "open", null, true);
  var completed = document.querySelector("[data-journey-complete]");
  if (completed) {
    var completedPage = completed.getAttribute("data-journey-complete");
    if (["book", "estimate", "roadside"].indexOf(completedPage) >= 0) {
      emit("complete", "success", completedPage, true);
    }
  }
  if (document.querySelector('[role="alert"], .error-msg')) {
    emit("error", "server_error", null, true);
  }

  document.addEventListener("focusin", function (e) {
    if (e.target && e.target.form && e.target.type !== "hidden" && e.target.type !== "submit") {
      emit("focus", stepFor(e.target), null, true);
    }
  }, true);
  document.addEventListener("invalid", function (e) {
    if (e.target && e.target.form) emit("invalid", stepFor(e.target), null, true);
  }, true);
  document.addEventListener("change", function (e) {
    if (page === "approval" && e.target && e.target.name && e.target.name.indexOf("line_") === 0) {
      emit("choice", "decision", null, true);
    }
  }, true);
  document.addEventListener("submit", function (e) {
    if (!e.target || e.target.tagName !== "FORM") return;
    emit("submit", "send", null, true);
    if (page === "approval") {
      var el = document.createElement("input");
      el.type = "hidden";
      el.name = "journey_sid";
      el.value = sid;
      e.target.appendChild(el);
    }
  }, true);
  document.addEventListener("click", function (e) {
    var link = e.target && e.target.closest ? e.target.closest("a[href]") : null;
    if (!link) return;
    var href = link.getAttribute("href") || "";
    if (href.indexOf("tel:") === 0) return emit("next", "call", null, true);
    try {
      var target = new URL(href, location.origin);
      if (target.origin !== location.origin) return;
      if (target.pathname.indexOf("/sign/ro/") === 0) {
        emit("next", "review", null, true);
      } else if (/^\/site\/(book|estimate|roadside)/.test(target.pathname)) {
        emit("next", "service", null, true);
      }
    } catch (_) {}
  }, true);
})();
