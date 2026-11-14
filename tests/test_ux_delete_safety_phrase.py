"""VPN deletes stay simple confirm (pre-v10); account deletes keep typed reason."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class VpnDeleteSimpleConfirmTests(unittest.TestCase):
    def test_pg_users_delete_is_simple_confirm(self):
        html = (ROOT / "app/web/templates/pg_users.html").read_text(encoding="utf-8")
        self.assertNotIn("data-confirm-phrase", html)
        self.assertNotIn("'phrase': 'حذف'", html)
        self.assertIn("/pg/users/{{ uid }}/delete", html)
        self.assertIn("data-confirm=", html)
        self.assertIn("data-confirm-reason", html)

    def test_service_delete_is_simple_confirm(self):
        html = (ROOT / "app/web/templates/_user_edit_body.html").read_text(encoding="utf-8")
        self.assertIn("/services/{{ s.service.id }}/delete", html)
        self.assertNotIn("data-confirm-phrase", html)

    def test_server_does_not_enforce_vpn_phrase(self):
        pg = (ROOT / "app/api/pg_pages.py").read_text(encoding="utf-8")
        users = (ROOT / "app/api/user_pages.py").read_text(encoding="utf-8")
        bulk = (ROOT / "app/api/bulk_pages.py").read_text(encoding="utf-8")
        self.assertNotIn("confirm_phrase", pg)
        self.assertNotIn("confirm_phrase", users)
        self.assertNotIn('phrase != "حذف"', bulk)

    def test_bot_delete_ask_is_simple_yes_cancel(self):
        bot = (ROOT / "app/bot/handlers/admin_pg_users.py").read_text(encoding="utf-8")
        start = bot.find("async def pg_user_del_ask")
        end = bot.find("async def pg_user_del(", start)
        block = bot[start:end]
        self.assertIn("بله، حذف شود", block)
        self.assertIn("انصراف", block)
        self.assertNotIn("فقط غیرفعال", block)


if __name__ == "__main__":
    unittest.main()
