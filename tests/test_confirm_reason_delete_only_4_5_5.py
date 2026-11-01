"""Confirm modal reason field: only for delete user/reseller/admin."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "app/web/static/panel.js").read_text(encoding="utf-8")
CSS = (ROOT / "app/web/static/panel.css").read_text(encoding="utf-8")
BASE = (ROOT / "app/web/templates/base.html").read_text(encoding="utf-8")
TEMPLATES = ROOT / "app/web/templates"


class ConfirmReasonSlotTests(unittest.TestCase):
    def test_base_has_slot_not_static_reason_field(self):
        self.assertIn('id="confirm-reason-slot"', BASE)
        self.assertIn("confirm-reason-slot", JS)

    def test_js_supports_require_reason(self):
        self.assertIn("requireReason", JS)
        self.assertIn("attrTruthy(src, 'data-confirm-reason')", JS)


class ConfirmReasonOnlyOnAccountDeletes(unittest.TestCase):
    ALLOWED = {
        "users.html",
        "resellers.html",
        "_reseller_edit_body.html",
        "_user_edit_body.html",
        "pg_admins.html",
        # Not a delete: the message to trial users reuses the prompt as a text box (own label).
        "trial_users.html",
    }

    def test_only_allowed_templates_set_data_confirm_reason(self):
        offenders = []
        for path in TEMPLATES.rglob("*.html"):
            text = path.read_text(encoding="utf-8")
            if "data-confirm-reason" not in text:
                continue
            if path.name not in self.ALLOWED:
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [], msg=f"unexpected reason attrs: {offenders}")

    def test_user_reseller_admin_deletes_require_reason(self):
        users = (TEMPLATES / "users.html").read_text(encoding="utf-8")
        delete = users.split("/users/{{ u.id }}/delete", 1)[1].split("</form>", 1)[0]
        self.assertIn('data-confirm-reason="1"', delete)
        self.assertIn('name="reason"', delete)

        resellers = (TEMPLATES / "resellers.html").read_text(encoding="utf-8")
        self.assertIn('data-confirm-reason="1"', resellers)

        admins = (TEMPLATES / "pg_admins.html").read_text(encoding="utf-8")
        self.assertIn('data-confirm-reason="1"', admins)

        user_edit = (TEMPLATES / "_user_edit_body.html").read_text(encoding="utf-8")
        self.assertIn('data-confirm-reason="1"', user_edit)
        self.assertNotIn("data-confirm-phrase", user_edit)


class ConfirmReasonBackendEnforced(unittest.TestCase):
    def test_users_delete_requires_reason(self):
        api = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        block = api.split("async def users_delete", 1)[1].split("\n    @app.", 1)[0]
        self.assertIn("extract_delete_reason", block)
        self.assertIn("delete_reason_too_short", block)
        self.assertIn("علت حذف کاربر الزامی است", block)
        self.assertIn("notify_account_edit", block)
        self.assertLess(block.find("notify_account_edit"), block.find("delete_bot_user"))

    def test_apply_reason_updates_in_place(self):
        self.assertIn("update existing hidden reason in place", JS)
        self.assertIn("form.submit()", JS)
        # Must not rely on requestSubmit after confirm (confirmSkip race)
        # for the data-confirm form path
        block = JS.split("function applyReason")[1].split("function applyPhrase")[0]
        self.assertIn("hidden.value = text", block)


if __name__ == "__main__":
    unittest.main()
