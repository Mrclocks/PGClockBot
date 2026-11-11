/* Panel design language behaviors. Every control drives a real form field, so the
   server sees exactly the fields it saw before; this file only changes how they are picked. */
(function (root) {
  "use strict";

  function qs(scope, sel) {
    return sel ? scope.querySelector(sel) || document.querySelector(sel) : null;
  }

  /* ---------- Choice groups: [data-ds-choice] buttons with data-value -> target field ---------- */
  function choiceTarget(group) {
    return qs(group, group.getAttribute("data-ds-target"));
  }

  function syncChoice(group) {
    const target = choiceTarget(group);
    if (!target) return;
    const value = String(target.value == null ? "" : target.value);
    const disabled = new Set();
    if (target.tagName === "SELECT") {
      Array.from(target.options).forEach((o) => { if (o.disabled) disabled.add(o.value); });
    }
    let matched = false;
    group.querySelectorAll("[data-value]").forEach((btn) => {
      const also = (btn.getAttribute("data-ds-also") || "").split(",").filter(Boolean);
      const on = btn.getAttribute("data-value") === value || also.indexOf(value) >= 0;
      if (on) matched = true;
      btn.classList.toggle("on", on);
      btn.setAttribute("aria-checked", on ? "true" : "false");
      btn.disabled = disabled.has(btn.getAttribute("data-value"));
    });
    const other = group.querySelector("[data-ds-other]");
    if (other) {
      const on = !matched;
      other.classList.toggle("on", on);
      other.setAttribute("aria-checked", on ? "true" : "false");
    }
    const otherField = qs(group, group.getAttribute("data-ds-other-field"));
    if (otherField) otherField.hidden = matched;
  }

  function pickChoice(group, btn) {
    const target = choiceTarget(group);
    if (!target || btn.disabled) return;
    if (btn.hasAttribute("data-ds-other")) {
      const otherField = qs(group, group.getAttribute("data-ds-other-field"));
      group.querySelectorAll("[data-value]").forEach((b) => { b.classList.remove("on"); b.setAttribute("aria-checked", "false"); });
      btn.classList.add("on");
      btn.setAttribute("aria-checked", "true");
      if (otherField) {
        otherField.hidden = false;
        const input = otherField.querySelector("input") || target;
        if (input && input.focus) input.focus();
      }
      return;
    }
    target.value = btn.getAttribute("data-value");
    target.dispatchEvent(new Event("input", { bubbles: true }));
    target.dispatchEvent(new Event("change", { bubbles: true }));
    syncChoice(group);
  }

  /* ---------- «نامحدود» toggles: [data-ds-unlimited] for a number field where 0 means no limit ---------- */
  function unlimitedField(btn) {
    return qs(btn.closest(".form-field") || document, btn.getAttribute("data-ds-unlimited"));
  }

  function syncUnlimited(btn) {
    const input = unlimitedField(btn);
    if (!input) return;
    const raw = String(input.value).trim();
    const on = btn.hasAttribute("data-ds-empty-is-unlimited") ? raw === "" : raw === "0";
    btn.setAttribute("aria-pressed", on ? "true" : "false");
    input.readOnly = on;
    const wrap = input.closest(".ds-unit");
    if (wrap) wrap.classList.toggle("is-unlimited", on);
  }

  function toggleUnlimited(btn) {
    const input = unlimitedField(btn);
    if (!input) return;
    const on = btn.getAttribute("aria-pressed") !== "true";
    const emptyIsUnlimited = btn.hasAttribute("data-ds-empty-is-unlimited");
    if (on) {
      if (String(input.value).trim() !== "" && String(input.value).trim() !== "0") input.dataset.dsLast = input.value;
      input.value = emptyIsUnlimited ? "" : "0";
    } else {
      input.value = input.dataset.dsLast || "";
    }
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
    syncUnlimited(btn);
    if (!on) input.focus();
  }

  /* ---------- Switch groups with counters: [data-ds-perms] ---------- */
  function faDigits(n) {
    return String(n).replace(/\d/g, (d) => "۰۱۲۳۴۵۶۷۸۹"[Number(d)]);
  }

  function syncPerms(box) {
    let on = 0;
    let all = 0;
    box.querySelectorAll("[data-ds-group]").forEach((group) => {
      const inputs = group.querySelectorAll('input[type="checkbox"]');
      const checked = Array.from(inputs).filter((i) => i.checked).length;
      on += checked;
      all += inputs.length;
      const meta = group.querySelector("[data-ds-group-count]");
      if (meta) meta.textContent = `${faDigits(checked)} از ${faDigits(inputs.length)}`;
    });
    const total = box.querySelector("[data-ds-total]");
    if (total) total.textContent = faDigits(on);
    const of = box.querySelector("[data-ds-of]");
    if (of) of.textContent = faDigits(all);
  }

  function resetPerms(box) {
    box.querySelectorAll('input[type="checkbox"][data-default]').forEach((input) => {
      input.checked = input.getAttribute("data-default") === "1";
    });
    syncPerms(box);
  }

  /* ---------- Show when: [data-ds-show-when="#field:value,value"] follows another field ----------
     Hidden, not disabled: the hidden field still posts, so its value survives switching back.
     A checkbox reads as its value when checked and "0" when not. */
  function showWhenMatches(rule, value) {
    const cut = String(rule || "").lastIndexOf(":");
    if (cut <= 0) return true;
    return String(rule).slice(cut + 1).split(",").indexOf(String(value)) >= 0;
  }

  function fieldValue(field) {
    if (field.type === "checkbox") return field.checked ? field.value : "0";
    return field.value;
  }

  function syncShowWhen(el) {
    const rule = el.getAttribute("data-ds-show-when") || "";
    const field = qs(el.closest("form") || document, rule.slice(0, rule.lastIndexOf(":")));
    if (field) el.hidden = !showWhenMatches(rule, fieldValue(field));
  }

  /* ---------- Two-column field grids: a field without a partner takes the whole row ----------
     Fields pair up in order; a row that already spans both columns (a switch, a long field) starts a new run.
     The last field of a run with an odd count would sit beside an empty half, so it spans the row instead. */
  const PAIR_GRIDS = ".ds-grid-2, .settings-grid, .ui-modal .form-row";
  const ALONE = "ds-row-alone";

  /** cells: [{shown, fullRow}] in DOM order -> indexes of the cells that should span the row. */
  function unpairedCells(cells) {
    const alone = [];
    let run = [];
    const close = () => {
      if (run.length % 2 === 1) alone.push(run[run.length - 1]);
      run = [];
    };
    cells.forEach((cell, index) => {
      if (!cell.shown) return;
      if (cell.fullRow) close();
      else run.push(index);
    });
    close();
    return alone;
  }

  function syncPairs(grid) {
    const kids = Array.from(grid.children);
    kids.forEach((el) => el.classList.remove(ALONE));
    const cells = kids.map((el) => {
      const style = getComputedStyle(el);
      const shown = style.display !== "none" && style.position !== "absolute" && style.position !== "fixed";
      return { shown, fullRow: shown && style.gridColumnEnd === "-1" };
    });
    unpairedCells(cells).forEach((index) => kids[index].classList.add(ALONE));
  }

  function syncAllPairs(scope) {
    scope.querySelectorAll(PAIR_GRIDS).forEach(syncPairs);
  }

  /* ---------- Reveal: [data-ds-reveal="#id"] shows an optional field and hides itself ---------- */
  function reveal(btn) {
    const target = qs(document, btn.getAttribute("data-ds-reveal"));
    if (!target) return;
    target.hidden = false;
    btn.hidden = true;
    const field = target.querySelector("input, textarea, select");
    if (field) field.focus();
  }

  /* ---------- Colour dot: mirror the chosen style on the host for the dot colour ---------- */
  function syncDot(select) {
    const host = select.closest("[data-ds-dot]");
    if (!host) return;
    const opt = select.selectedOptions && select.selectedOptions[0];
    host.setAttribute("data-value", select.value);
    host.setAttribute("data-tone", (opt && opt.getAttribute("data-tone")) || "default");
  }

  /* ---------- Missing required fields: footer warning + section bar state ---------- */
  function isShown(el, form) {
    for (let node = el; node && node !== form.parentNode; node = node.parentElement) {
      if (node.hidden) return false;
    }
    return true;
  }

  function fieldLabel(el) {
    if (el.getAttribute("data-ds-label")) return el.getAttribute("data-ds-label");
    const field = el.closest(".form-field");
    const text = field && field.querySelector(".ds-label-text");
    return text ? text.textContent.trim() : "";
  }

  function missingFields(form) {
    const missing = [];
    form.querySelectorAll("[required]").forEach((el) => {
      if (el.disabled || !isShown(el, form)) return;
      if (String(el.value || "").trim() === "") missing.push(el);
    });
    form.querySelectorAll("[data-ds-require-one]").forEach((box) => {
      if (!isShown(box, form)) return;
      if (!box.querySelector('input[type="checkbox"]:checked')) missing.push(box);
    });
    return missing;
  }

  function syncForm(form) {
    const missing = missingFields(form);
    const panel = form.closest(".ui-modal-panel") || document;
    const foot = panel.querySelector(`.ds-foot[data-ds-form="${form.id}"]`);
    if (foot) {
      const warn = foot.querySelector(".ds-foot-warn");
      const summary = foot.querySelector(".ds-summary");
      if (warn) {
        warn.hidden = missing.length === 0;
        const label = warn.querySelector("[data-ds-missing]");
        if (label && missing.length) label.textContent = fieldLabel(missing[0]);
        const count = warn.querySelector("[data-ds-missing-count]");
        if (count) count.textContent = faDigits(missing.length);
        warn._dsFirst = missing[0] || null;
      }
      if (summary) summary.hidden = missing.length > 0;
    }
    panel.querySelectorAll(`.ds-steps[data-ds-form="${form.id}"] a[href^="#"]`).forEach((link) => {
      const section = document.getElementById(link.getAttribute("href").slice(1));
      const visible = section && isShown(section, form);
      link.hidden = !visible;
      const need = visible && missing.some((el) => section.contains(el));
      link.classList.toggle("need", need);
      link.classList.toggle("done", Boolean(visible && !need));
    });
    const steps = panel.querySelector(`.ds-steps[data-ds-form="${form.id}"]`);
    if (steps) steps.style.setProperty("--ds-steps", String(steps.querySelectorAll("a:not([hidden])").length || 1));
  }

  function goToMissing(warn) {
    const el = warn._dsFirst;
    if (!el) return;
    const section = el.closest(".ds-sec") || el;
    section.scrollIntoView({ behavior: "smooth", block: "start" });
    const focusable = el.matches("input, select, textarea") ? el : el.querySelector("input, button");
    if (focusable && focusable.focus) setTimeout(() => focusable.focus({ preventScroll: true }), 250);
  }

  /* Long tab rows scroll sideways on phones: bring the active tab into view. */
  function revealActiveTabs(scope) {
    scope.querySelectorAll(".ds .section-tabs").forEach((row) => {
      const active = row.querySelector("a.active, .tab-btn.active");
      if (!active || row.scrollWidth <= row.clientWidth + 1) return;
      const rowBox = row.getBoundingClientRect();
      const box = active.getBoundingClientRect();
      row.scrollLeft += box.left + box.width / 2 - (rowBox.left + rowBox.width / 2);
    });
  }

  function syncAll(scope) {
    const base = scope || document;
    base.querySelectorAll("[data-ds-choice]").forEach(syncChoice);
    base.querySelectorAll("[data-ds-unlimited]").forEach(syncUnlimited);
    base.querySelectorAll("[data-ds-perms]").forEach(syncPerms);
    base.querySelectorAll("[data-ds-dot] select").forEach(syncDot);
    base.querySelectorAll("form[data-ds-form]").forEach(syncForm);
    base.querySelectorAll("[data-ds-show-when]").forEach(syncShowWhen);
    syncAllPairs(base);
  }

  function bind() {
    document.addEventListener("click", (event) => {
      const t = event.target;
      if (!t || !t.closest) return;
      const choiceBtn = t.closest("[data-ds-choice] [data-value], [data-ds-choice] [data-ds-other]");
      if (choiceBtn) { pickChoice(choiceBtn.closest("[data-ds-choice]"), choiceBtn); return; }
      const unlimited = t.closest("[data-ds-unlimited]");
      if (unlimited) { event.preventDefault(); toggleUnlimited(unlimited); return; }
      const reset = t.closest("[data-ds-perms-reset]");
      if (reset) { const box = qs(document, reset.getAttribute("data-ds-perms-reset")); if (box) { resetPerms(box); refreshForm(box); } return; }
      const rev = t.closest("[data-ds-reveal]");
      if (rev) { reveal(rev); return; }
      const warn = t.closest(".ds-foot-warn");
      if (warn) { event.preventDefault(); goToMissing(warn); }
    });
    const onEdit = (event) => {
      const t = event.target;
      if (!t || !t.closest) return;
      if (t.matches && t.matches("[data-ds-dot] select")) syncDot(t);
      const box = t.closest("[data-ds-perms]");
      if (box) syncPerms(box);
      document.querySelectorAll("[data-ds-choice]").forEach((g) => { if (choiceTarget(g) === t) syncChoice(g); });
      document.querySelectorAll("[data-ds-show-when]").forEach(syncShowWhen);
      refreshForm(t);
      // Show-when rules and page scripts hide fields on change, which can leave a field without a partner.
      if (event.type === "change") syncAllPairs(document);
      if (event.type === "change" && t.matches && t.matches("[data-ds-submit-on-change]") && t.form) {
        t.form.requestSubmit ? t.form.requestSubmit() : t.form.submit();
      }
    };
    document.addEventListener("change", onEdit);
    document.addEventListener("input", onEdit);
    syncAll(document);
    revealActiveTabs(document);
  }

  function refreshForm(el) {
    const form = el.closest ? el.closest("form[data-ds-form]") : null;
    if (form) syncForm(form);
  }

  const api = { syncAll, syncChoice, syncForm, missingFields, faDigits, resetPerms, syncPerms, showWhenMatches, unpairedCells };
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.PanelDs = api;
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", bind);
    else bind();
  }
})(typeof window !== "undefined" ? window : globalThis);
