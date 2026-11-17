"""Confirm modal reason field: only for delete user/reseller/admin."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "app/web/static/panel.js").read_text(encoding="utf-8")
BASE = (ROOT / "app/web/templates/base.html").read_text(encoding="utf-8")


class ConfirmReasonSlotTests(unittest.TestCase):
    def test_base_has_slot_not_static_reason_field(self):
        self.assertIn('id="confirm-reason-slot"', BASE)
        self.assertIn("confirm-reason-slot", JS)

    def test_js_supports_require_reason(self):
        self.assertIn("requireReason", JS)
        self.assertIn("attrTruthy(src, 'data-confirm-reason')", JS)


class ConfirmReasonBackendEnforced(unittest.TestCase):

    def test_apply_reason_updates_in_place(self):
        self.assertIn("update existing hidden reason in place", JS)
        self.assertIn("form.submit()", JS)
        # Must not rely on requestSubmit after confirm (confirmSkip race)
        # for the data-confirm form path
        block = JS.split("function applyReason")[1].split("function applyPhrase")[0]
        self.assertIn("hidden.value = text", block)


if __name__ == "__main__":
    unittest.main()
