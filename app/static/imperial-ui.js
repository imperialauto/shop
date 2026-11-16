/* Shared page behavior for signed-in pages:
   - quick-reply chips fill the reply box next to them (nothing is sent)
   - "Report a problem" dialog
   - year → make → model dropdown picker that collapses once filled */
(function () {
  "use strict";

  // ── Quick replies ─────────────────────────────────────────────────────────
  document.addEventListener("click", function (event) {
    var chip = event.target.closest("[data-quick-reply]");
    if (!chip) return;
    var form = chip.closest("form");
    var box = form && form.querySelector("textarea[name='reply_text']");
    if (!box) return;
    box.value = chip.getAttribute("data-quick-reply");
    box.focus();
    box.setSelectionRange(box.value.length, box.value.length);
    form.querySelectorAll("[data-quick-reply]").forEach(function (c) { c.classList.toggle("active", c === chip); });
  });

  // ── Report a problem ──────────────────────────────────────────────────────
  var fab = document.getElementById("problem-fab");
  var dialog = document.getElementById("problem-dialog");
  if (fab && dialog && typeof dialog.showModal === "function") {
    fab.addEventListener("click", function () {
      dialog.querySelector("[name='page']").value = location.pathname + location.search;
      dialog.showModal();
      dialog.querySelector("textarea").focus();
    });
    dialog.querySelector("[data-close]").addEventListener("click", function () { dialog.close(); });
    // Save without leaving the page: whatever is typed in the page's own form stays put (Fix list #130).
    var reportForm = dialog.querySelector("form");
    reportForm.addEventListener("submit", function (event) {
      if (!window.fetch || !window.FormData) return; // plain post still works
      event.preventDefault();
      var send = reportForm.querySelector("button:not([type='button'])");
      var note = reportForm.querySelector("[data-report-status]");
      if (!note) {
        note = document.createElement("p");
        note.setAttribute("data-report-status", "");
        note.setAttribute("role", "status");
        note.style.cssText = "margin:0; font-size:13px;";
        reportForm.insertBefore(note, send.parentNode);
      }
      send.disabled = true;
      note.textContent = "Saving…";
      fetch("/problems", { method: "POST", body: new FormData(reportForm), headers: { Accept: "application/json" }, credentials: "same-origin" })
        .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
        .then(function (saved) {
          note.textContent = "Saved to the Fix list as #" + saved.id + ". Nothing on this page was changed.";
          reportForm.querySelector("textarea").value = "";
          setTimeout(function () { dialog.close(); note.textContent = ""; }, 1800);
        })
        .catch(function () { note.textContent = "Couldn't save it. Your text is still here; try again."; })
        .then(function () { send.disabled = false; });
    });
  } else if (fab) {
    // No dialog support: open the report page in a new tab so this page's form isn't lost.
    fab.addEventListener("click", function () { window.open("/problems?page=" + encodeURIComponent(location.pathname), "_blank", "noopener"); });
  }

  // ── Year / make / model picker ───────────────────────────────────────────
  var MAKES = ["Acura", "Audi", "BMW", "Buick", "Cadillac", "Chevrolet", "Chrysler", "Dodge", "Fiat", "Ford",
    "Freightliner", "Genesis", "GMC", "Honda", "Hino", "Hyundai", "Infiniti", "International", "Isuzu", "Jaguar",
    "Jeep", "Kenworth", "Kia", "Land Rover", "Lexus", "Lincoln", "Mack", "Mazda", "Mercedes-Benz", "Mercury",
    "Mini", "Mitsubishi", "Nissan", "Peterbilt", "Pontiac", "Porsche", "Ram", "Saturn", "Scion", "Subaru",
    "Tesla", "Toyota", "Volkswagen", "Volvo"];
  var OTHER = "__other__";

  function option(value, label, selected) {
    var o = document.createElement("option");
    o.value = value; o.textContent = label; if (selected) o.selected = true;
    return o;
  }

  function cached(key, url) {
    try { var hit = sessionStorage.getItem(key); if (hit) return Promise.resolve(JSON.parse(hit)); } catch (e) {}
    return fetch(url).then(function (r) { return r.json(); }).then(function (data) {
      try { sessionStorage.setItem(key, JSON.stringify(data)); } catch (e) {}
      return data;
    });
  }

  function setupPicker(root) {
    var year = root.querySelector("[name='year']");
    var make = root.querySelector("[name='make']");
    var model = root.querySelector("[name='model']");
    var vin = root.querySelector("[name='vin']");
    if (!year || !make || !model) return;

    // Year: a select from next year back to 1981.
    var yearSelect = year;
    if (year.tagName !== "SELECT") {
      yearSelect = document.createElement("select");
      yearSelect.className = year.className;
      yearSelect.appendChild(option("", "Year"));
      var next = new Date().getFullYear() + 1;
      for (var y = next; y >= 1981; y--) yearSelect.appendChild(option(String(y), String(y), String(y) === year.value));
      year.type = "hidden";
      year.parentNode.insertBefore(yearSelect, year);
      yearSelect.addEventListener("change", function () { year.value = yearSelect.value; onYearOrMake(); });
    } else {
      yearSelect.addEventListener("change", onYearOrMake);
    }

    function dropdownFor(input, placeholder) {
      var select = document.createElement("select");
      select.className = input.className;
      select.setAttribute("aria-label", placeholder);
      input.parentNode.insertBefore(select, input);
      input.style.display = "none";
      select.addEventListener("change", function () {
        if (select.value === OTHER) { input.style.display = ""; input.value = ""; input.focus(); }
        else { input.style.display = "none"; input.value = select.value; }
        if (input === make) onYearOrMake(); else maybeCollapse();
      });
      // Typed values never fold the section: folding on blur would move the
      // Save button out from under the user's tap.
      return select;
    }
    var makeSelect = dropdownFor(make, "Make");
    var modelSelect = dropdownFor(model, "Model");

    function fill(select, values, current, placeholder) {
      select.innerHTML = "";
      select.appendChild(option("", placeholder));
      var found = false;
      values.forEach(function (v) {
        var match = current && v.toLowerCase() === current.toLowerCase();
        if (match) found = true;
        select.appendChild(option(v, v, match));
      });
      select.appendChild(option(OTHER, "Other (type it)", Boolean(current) && !found));
      var input = select === makeSelect ? make : model;
      input.style.display = current && !found ? "" : "none";
    }

    fill(makeSelect, MAKES, make.value, "Make");
    fill(modelSelect, [], model.value, year.value && make.value ? "Loading models…" : "Pick year and make");

    function yearValue() { return yearSelect.value || year.value; }

    function onYearOrMake() {
      var y = yearValue(), m = make.value;
      if (!y || !m) { fill(modelSelect, [], model.value, "Pick year and make"); return; }
      fill(modelSelect, [], model.value, "Loading models…");
      var url = "https://vpic.nhtsa.dot.gov/api/vehicles/GetModelsForMakeYear/make/" +
        encodeURIComponent(m) + "/modelyear/" + encodeURIComponent(y) + "?format=json";
      cached("vp:" + m + ":" + y, url).then(function (data) {
        var names = (data.Results || []).map(function (r) { return r.Model_Name; })
          .filter(function (v, i, all) { return v && all.indexOf(v) === i; }).sort();
        fill(modelSelect, names, model.value, names.length ? "Model" : "No list found, type it");
      }).catch(function () { fill(modelSelect, [], model.value, "Type the model"); });
    }

    // Once year, make and model are set, fold the three boxes into one line.
    var summary = document.createElement("div");
    summary.className = "vp-summary";
    summary.hidden = true;
    var label = document.createElement("strong");
    var change = document.createElement("button");
    change.type = "button"; change.className = "qchip"; change.textContent = "Change";
    summary.appendChild(label); summary.appendChild(change);
    var fields = yearSelect.closest(".grid, .vp-grid") || yearSelect.parentNode.parentNode;
    fields.parentNode.insertBefore(summary, fields);
    change.addEventListener("click", function () { summary.hidden = true; fields.hidden = false; });

    function maybeCollapse() {
      if (yearValue() && make.value && model.value) {
        label.textContent = yearValue() + " " + make.value + " " + model.value;
        summary.hidden = false; fields.hidden = true;
      }
    }

    if (vin) {
      vin.addEventListener("input", function () {
        var raw = vin.value.trim().toUpperCase();
        if (raw.length !== 17) return;
        cached("vin:" + raw, "https://vpic.nhtsa.dot.gov/api/vehicles/decodevinvalues/" + raw + "?format=json")
          .then(function (d) {
            var v = (d.Results || [])[0] || {};
            if (v.ModelYear) { year.value = v.ModelYear; yearSelect.value = v.ModelYear; }
            if (v.Make) { make.value = v.Make.charAt(0) + v.Make.slice(1).toLowerCase(); fill(makeSelect, MAKES, make.value, "Make"); }
            if (v.Model) { model.value = v.Model; fill(modelSelect, [v.Model], v.Model, "Model"); }
            maybeCollapse();
          }).catch(function () {});
      });
    }

    if (yearValue() && make.value) onYearOrMake();
    maybeCollapse();
  }

  document.querySelectorAll("[data-vehicle-picker]").forEach(setupPicker);
})();
