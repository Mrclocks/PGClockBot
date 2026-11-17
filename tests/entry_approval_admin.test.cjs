"use strict";
const assert = require("node:assert/strict");
const { rowModel, statusInfo } = require("../app/web/static/entry-approval-admin.js");

const pending = rowModel({ id: 1, status: "pending", customer: 102, customer_name: "علی", username: "ali", requested_at: "r", decided_at: "" });
assert.equal(pending.name, "علی");
assert.equal(pending.username, "@ali");
assert.equal(pending.when, "r");
assert.deepEqual(pending.actions, ["approve", "reject", "block"]);

const blocked = rowModel({ id: 2, status: "blocked", customer: 103, customer_name: "", username: "", requested_at: "r", decided_at: "d" });
assert.equal(blocked.name, "103");
assert.equal(blocked.when, "d");
assert.deepEqual(blocked.actions, ["approve"]);

assert.equal(statusInfo({ status: "rejected", expired: true }).label, "منقضی");
assert.equal(statusInfo({ status: "rejected", expired: false }).label, "رد شده");
assert.deepEqual(rowModel({ status: "approved", customer: 1 }).actions, []);
const handleOnly = rowModel({ status: "pending", customer: 7, customer_name: "", username: "ali_k" });
assert.equal(handleOnly.name, "@ali_k");
assert.equal(handleOnly.username, "");
assert.equal(statusInfo({ status: "<b>" }).label, "<b>");
console.log("entry_approval_admin tests passed");
