/* Permission group counters fill from checkbox state (fragment-safe syncPerms). */
const assert = require("assert");
const ds = require("../app/web/static/panel-ds.js");

function el(tag, attrs, kids) {
  const node = {
    tagName: String(tag).toUpperCase(),
    attributes: Object.assign({}, attrs || {}),
    children: kids || [],
    textContent: "",
    getAttribute(name) {
      return this.attributes[name] == null ? null : String(this.attributes[name]);
    },
    matches(sel) {
      if (sel === 'input[type="checkbox"]') {
        return this.tagName === "INPUT" && this.attributes.type === "checkbox";
      }
      return false;
    },
    querySelector(sel) {
      const all = this.querySelectorAll(sel);
      return all[0] || null;
    },
    querySelectorAll(sel) {
      const out = [];
      const walk = (n) => {
        if (!n || typeof n !== "object") return;
        if (sel === "[data-ds-group]" && n.attributes && "data-ds-group" in n.attributes) out.push(n);
        if (sel === "[data-ds-group-count]" && n.attributes && "data-ds-group-count" in n.attributes) out.push(n);
        if (sel === "[data-ds-total]" && n.attributes && "data-ds-total" in n.attributes) out.push(n);
        if (sel === "[data-ds-of]" && n.attributes && "data-ds-of" in n.attributes) out.push(n);
        if (sel === 'input[type="checkbox"]' && n.matches && n.matches(sel)) out.push(n);
        (n.children || []).forEach(walk);
      };
      walk(this);
      return out;
    },
  };
  if (tag === "input") {
    node.checked = Boolean(attrs && attrs.checked);
    node.type = (attrs && attrs.type) || "text";
  }
  return node;
}

const box = el("div", { "data-ds-perms": "" }, [
  el("b", { "data-ds-total": "" }),
  el("span", { "data-ds-of": "" }),
  el("details", { "data-ds-group": "" }, [
    el("span", { "data-ds-group-count": "" }),
    el("input", { type: "checkbox", checked: true }),
    el("input", { type: "checkbox" }),
    el("input", { type: "checkbox", checked: true }),
  ]),
  el("details", { "data-ds-group": "" }, [
    el("span", { "data-ds-group-count": "" }),
    el("input", { type: "checkbox" }),
    el("input", { type: "checkbox" }),
  ]),
]);

ds.syncPerms(box);

assert.strictEqual(box.querySelector("[data-ds-total]").textContent, "۲");
assert.strictEqual(box.querySelector("[data-ds-of]").textContent, "۵");
const metas = box.querySelectorAll("[data-ds-group-count]").map((n) => n.textContent);
assert.deepStrictEqual(metas, ["۲ از ۳", "۰ از ۲"]);
assert.ok(metas.every((t) => String(t).trim().length > 0), "group counters must not stay empty");

console.log("ds_perm_counters.test.cjs ok");
