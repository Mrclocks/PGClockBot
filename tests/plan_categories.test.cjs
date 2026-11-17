"use strict";
const assert = require("node:assert/strict");
const { cleanName, readQuickResult, addCategory, arrivalId, NAME_MAX } = require("../app/web/static/plan-categories.js");

// The name sent is trimmed; nothing usable means nothing is sent.
assert.equal(cleanName("  ویژه "), "ویژه");
assert.equal(cleanName("   "), "");
assert.equal(cleanName(null), "");
assert.equal(cleanName("x".repeat(NAME_MAX)), "x".repeat(NAME_MAX));
assert.equal(cleanName("x".repeat(NAME_MAX + 1)), "");

// Only a well-formed answer counts as created; anything else shows a message, never raw server data.
assert.deepEqual(readQuickResult({ ok: true, id: 7, name: "طلایی", created: true, tone: "success" }),
  { ok: true, id: 7, name: "طلایی", tone: "success" });
// A tone the panel does not know falls back to the neutral dot.
assert.equal(readQuickResult({ ok: true, id: 7, name: "x", tone: "url(evil)" }).tone, "default");
assert.deepEqual(readQuickResult({ ok: false, error: "دسته‌ای با همین نام خاموش است" }),
  { ok: false, error: "دسته‌ای با همین نام خاموش است" });
assert.equal(readQuickResult({ ok: true, id: "7", name: "x" }).ok, false);
assert.equal(readQuickResult({ ok: true, id: 0, name: "x" }).ok, false);
assert.equal(readQuickResult(null).ok, false);
assert.ok(readQuickResult({ ok: false, error: "  " }).error.length > 0);

// A tiny DOM: enough for options, chips and text that is never parsed as markup.
class Node {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.attributes = {}; this._text = ""; this.parent = null; }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
  set textContent(v) { this._text = String(v); this.children = []; }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  getAttribute(k) { return this.attributes[k] ?? null; }
  appendChild(c) { c.parent = this; this.children.push(c); return c; }
  insertBefore(c, ref) {
    c.parent = this;
    const at = ref ? this.children.indexOf(ref) : -1;
    if (at < 0) this.children.push(c); else this.children.splice(at, 0, c);
    return c;
  }
  querySelector(sel) { return sel === '[data-value=""]' ? this.children.find((c) => c.getAttribute("data-value") === "") || null : null; }
}
const doc = { createElement: (tag) => new Node(tag) };
const select = new Node("select");
select.options = select.children;
const noneOption = doc.createElement("option");
noneOption.value = "";
select.appendChild(noneOption);
const chips = new Node("div");
const noneChip = new Node("button");
noneChip.setAttribute("data-value", "");
chips.appendChild(noneChip);

const hostile = '<img src=x onerror="alert(1)">';
assert.equal(addCategory(select, chips, doc, { id: 9, name: hostile, tone: "danger" }), "9");
const chip = chips.children[0];
assert.equal(chip.getAttribute("data-value"), "9", "the new chip goes before «بدون دسته»");
assert.equal(chip.getAttribute("data-tone"), "danger", "the chip wears the colour of its bot button");
assert.equal(chips.children[1], noneChip);
assert.equal(chip.textContent, hostile, "the name is text");
assert.ok(chip.children.every((c) => c.children.length === 0), "no markup was parsed from the name");
assert.equal(select.options.length, 2);
assert.equal(select.options[1].value, "9");

// Adding the same category again (a repeated answer) changes nothing.
addCategory(select, chips, doc, { id: 9, name: hostile });
assert.equal(select.options.length, 2);
assert.equal(chips.children.length, 2);
// Only a category anchor brings a group into view; anything else in the address is ignored.
assert.equal(arrivalId("#plan-cat-12"), "plan-cat-12");
assert.equal(arrivalId("#plan-category-board"), null);
assert.equal(arrivalId("#plan-cat-0"), null);
assert.equal(arrivalId("#plan-cat-12x"), null);
assert.equal(arrivalId('#plan-cat-1"><img'), null);
assert.equal(arrivalId(""), null);
console.log("plan_categories tests passed");
