"use strict";

const assert = require("node:assert/strict");
const {test} = require("node:test");
const {parseItems, validateItem, changeItem, removeItem, button, setButtonIcon, createControls, renderItems} = require("../app/web/static/list-inputs.js");
const normalize = (raw) => String(raw).replace(/[۰-۹٠-٩]/g, (digit) => {
  const code = digit.charCodeAt(0);
  return String(code >= 0x06f0 ? code - 0x06f0 : code - 0x0660);
}).replace(/[,٬\s]/g, "");

function installDocument(t) {
  function createNode(tagName) {
    const node = {
      tagName, className: "", dataset: {}, attributes: {}, children: [],
      setAttribute(name, value) {this.attributes[name] = value;},
      replaceChildren(...children) {this.children = children;},
      appendChild(child) {this.children.push(child);},
      append(...children) {this.children.push(...children);},
    };
    node.classList = {
      contains(name) {return node.className.split(" ").includes(name);},
      toggle(name, enabled) {
        const classes = new Set(node.className.split(" ").filter(Boolean));
        if (enabled) classes.add(name);
        else classes.delete(name);
        node.className = [...classes].join(" ");
      },
    };
    return node;
  }
  const previous = globalThis.document;
  globalThis.document = {createElement: createNode, createElementNS: (_namespace, tag) => createNode(tag)};
  t.after(() => {
    if (previous === undefined) delete globalThis.document;
    else globalThis.document = previous;
  });
}

test("list actions render labeled icons with add and delete color classes", (t) => {
  installDocument(t);
  const actions = [
    ["save", "افزودن", "icon-add"], ["delete", "حذف example.com", "icon-remove"],
    ["confirm-delete", "تأیید حذف example.com", "icon-remove"],
    ["edit", "ویرایش example.com", null], ["cancel", "انصراف", null],
    ["cancel-delete", "انصراف", null],
  ];
  for (const [action, label, colorClass] of actions) {
    const control = button(label, action, 0);
    assert.equal(control.type, "button");
    assert.equal(control.dataset.listAction, action);
    assert.equal(control.dataset.index, "0");
    assert.equal(control.attributes["aria-label"], label);
    assert.equal(control.title, label);
    assert.equal(control.textContent, undefined);
    assert.equal(control.children.length, 1);
    const svg = control.children[0];
    assert.equal(svg.tagName, "svg");
    assert.equal(svg.attributes["aria-hidden"], "true");
    assert.equal(svg.attributes.focusable, "false");
    assert.equal(svg.children[0].tagName, "path");
    assert.ok(svg.children[0].attributes.d);
    assert.equal(control.classList.contains("icon-add"), colorClass === "icon-add");
    assert.equal(control.classList.contains("icon-remove"), colorClass === "icon-remove");
  }
});

test("saving an edit changes the add icon to a check and restores it for additions", (t) => {
  installDocument(t);
  const control = button("افزودن", "save");
  const addPath = control.children[0].children[0].attributes.d;
  setButtonIcon(control, "check", "ثبت ویرایش");
  assert.notEqual(control.children[0].children[0].attributes.d, addPath);
  assert.equal(control.attributes["aria-label"], "ثبت ویرایش");
  assert.equal(control.title, "ثبت ویرایش");
  assert.equal(control.classList.contains("icon-add"), true);
  assert.equal(control.children.length, 1);
  setButtonIcon(control, "add", "افزودن");
  assert.equal(control.children[0].children[0].attributes.d, addPath);
  assert.equal(control.attributes["aria-label"], "افزودن");
  assert.equal(control.title, "افزودن");
});

test("list editor shows every item with only the entry input and no search control", (t) => {
  installDocument(t);
  const state = {
    source: {}, kind: "text", label: "آدرس‌ها", editing: -1, deleting: -1,
    items: ["a.example", "b.example", "c.example", "d.example", "e.example", "f.example"],
  };
  createControls(state);
  function inputs(node) {
    return (node.tagName === "input" ? [node] : []).concat(node.children.flatMap(inputs));
  }
  assert.deepEqual(inputs(state.panel), [state.draft]);
  renderItems(state);
  assert.deepEqual(state.itemsList.children.map((row) => row.children[0].textContent), state.items);
  assert.equal(state.empty.hidden, true);
  for (const [index, row] of state.itemsList.children.entries()) {
    const [edit, remove] = row.children[1].children;
    assert.equal(edit.dataset.listAction, "edit");
    assert.equal(remove.dataset.listAction, "delete");
    assert.equal(edit.dataset.index, String(index));
    assert.equal(remove.dataset.index, String(index));
  }
  state.items = [];
  renderItems(state);
  assert.deepEqual(state.itemsList.children, []);
  assert.equal(state.empty.hidden, false);
  assert.match(state.empty.textContent, /هنوز موردی اضافه نشده/);
});

test("existing lists keep their order and accept legacy separators", () => {
  assert.deepEqual(parseItems(" one.example, two.example،three.example;four.example\nfive.example ", "text", normalize),
    ["one.example", "two.example", "three.example", "four.example", "five.example"]);
  assert.deepEqual(parseItems("", "text", normalize), []);
  assert.deepEqual(parseItems("۵۰۰۰۰,١٠٠٠٠٠,200000", "money", normalize), ["50000", "100000", "200000"]);
  assert.deepEqual(parseItems("9223372036854775807,123456789", "telegram", normalize),
    ["9223372036854775807", "123456789"]);
  assert.deepEqual(parseItems("a.example,a.example", "text", normalize), ["a.example", "a.example"]);
});

test("adding and editing change only the chosen item", () => {
  const items = ["a.example", "b.example"];
  assert.deepEqual(changeItem(items, " c.example ", {kind: "text", normalize}),
    ["a.example", "b.example", "c.example"]);
  assert.deepEqual(changeItem(items, "c.example", {kind: "text", normalize, index: 0}),
    ["c.example", "b.example"]);
  assert.deepEqual(items, ["a.example", "b.example"]);
  assert.deepEqual(changeItem(items, "b.example", {kind: "text", normalize, index: 1}), items);
  assert.throws(() => changeItem(items, "x", {kind: "text", normalize, index: 4}), RangeError);
});

test("duplicate additions and edits cannot overwrite another item", () => {
  for (const index of [-1, 1]) {
    assert.throws(() => changeItem(["123", "456"], "۰۰۱۲۳", {kind: "telegram", normalize, index}), /قبلاً/);
  }
});

test("deletion removes only its selected item and preserves the remaining order", () => {
  const items = ["a.example", "b.example", "c.example"];
  assert.deepEqual(removeItem(items, 1), ["a.example", "c.example"]);
  assert.deepEqual(items, ["a.example", "b.example", "c.example"]);
  assert.deepEqual(removeItem(["a.example"], 0), []);
  for (const index of [-1, 3, 0.5, NaN]) assert.throws(() => removeItem(items, index), RangeError);
});

test("grouped and Persian money entries submit one integer preset", () => {
  assert.equal(validateItem("۱٬۲۵۰٬۰۰۰", "money", normalize), "1250000");
  assert.equal(validateItem("50,000", "money", normalize), "50000");
  assert.equal(validateItem("1000", "money", normalize), "1000");
  assert.equal(validateItem("2147483647", "money", normalize), "2147483647");
  for (const raw of ["999", "0", "-1000", "NaN", "Infinity", "1e6", "1000.5", "true", "2147483648"]) {
    assert.throws(() => validateItem(raw, "money", normalize), /مبلغ/);
  }
});

test("Telegram IDs keep integer precision and reject invalid values", () => {
  assert.equal(validateItem("٩٢٢٣٣٧٢٠٣٦٨٥٤٧٧٥٨٠٧", "telegram", normalize), "9223372036854775807");
  assert.equal(validateItem("۱۲۳۴۵۶۷۸۹", "telegram", normalize), "123456789");
  for (const raw of ["0", "-1", "NaN", "1.5", "true", "123 456", "9223372036854775808"]) {
    assert.throws(() => validateItem(raw, "telegram", normalize), /آیدی/);
  }
  assert.throws(() => validateItem("123,456", "telegram", normalize), /یک مقدار/);
});

test("blank entries and multiple addresses are rejected without losing hostile text", () => {
  assert.throws(() => validateItem("  ", "text", normalize), /وارد کنید/);
  assert.throws(() => validateItem("a.example،b.example", "text", normalize), /یک مقدار/);
  const hostile = '<img src=x onerror="alert(1)">';
  assert.deepEqual(changeItem([], hostile, {kind: "text", normalize}), [hostile]);
  assert.deepEqual(parseItems(hostile, "text", normalize), [hostile]);
});
