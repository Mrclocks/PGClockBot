"use strict";
const assert = require("node:assert/strict");
const { addPlanDefaults, activeTab } = require("../app/web/static/plans-tabs.js");

// The open tab decides the audience of a new plan.
assert.deepEqual(addPlanDefaults(true, "users", ""), { audience: "users", kind: "fixed" });
assert.deepEqual(addPlanDefaults(true, "resellers", ""), { audience: "resellers", kind: "fixed" });

// A redirect anchor inside the reseller panel keeps its kind.
assert.deepEqual(addPlanDefaults(true, "users", "#reseller-plans-payg"), { audience: "resellers", kind: "payg" });
assert.deepEqual(addPlanDefaults(true, "resellers", "#reseller-plans-addons"), { audience: "resellers", kind: "addon_volume" });
assert.deepEqual(addPlanDefaults(true, "resellers", "#user-service-addons"), { audience: "resellers", kind: "fixed" });

// Only a platform admin can add reseller plans.
assert.deepEqual(addPlanDefaults(false, "resellers", "#reseller-plans"), { audience: "users", kind: "fixed" });

// The visible panel is the active tab; no visible panel means users.
const fakeDoc = (panel) => ({ querySelector: () => (panel ? { getAttribute: () => panel } : null) });
assert.equal(activeTab(fakeDoc("resellers")), "resellers");
assert.equal(activeTab(fakeDoc(null)), "users");
console.log("plans_tabs tests passed");
