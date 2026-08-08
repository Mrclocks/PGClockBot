"""3.6.8 — ticket notify actually delivers; no miniapp on shop bots."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class Version368Tests(unittest.TestCase):
    def test_version(self):
        from app.version import __version__

        self.assertEqual(__version__, "4.9.0")
        self.assertEqual((ROOT / "VERSION").read_text(encoding="utf-8").strip(), "4.9.0")
        notes = (ROOT / "app/services/release_notes.py").read_text(encoding="utf-8")
        self.assertIn('"3.6.8"', notes)


class TicketNotifyDeliveryTests(unittest.TestCase):
    def test_ticket_notify_bypasses_acl(self):
        src = (ROOT / "app/services/notifications.py").read_text(encoding="utf-8")
        chunk = src.split("async def _shop_recipient_chat_ids")[1].split("async def ")[0]
        self.assertIn('notify_key != "notify_new_ticket"', chunk)
        self.assertIn("deliverable_telegram_id", chunk)
        self.assertIn("ids.append(tid)", chunk)

    def test_dispatch_prefers_live_shop_bot(self):
        src = (ROOT / "app/services/notifications.py").read_text(encoding="utf-8")
        chunk = src.split("async def _dispatch_dual_notify")[1].split("async def ")[0]
        self.assertIn("live_token == shop_token", chunk)
        self.assertIn('key != "notify_new_ticket"', chunk)
        self.assertIn("return 0", chunk)

    def test_send_returns_count(self):
        src = (ROOT / "app/services/notifications.py").read_text(encoding="utf-8")
        chunk = src.split("async def _send_to_chats")[1].split("\nasync def ")[0]
        self.assertIn("-> int", chunk)
        self.assertIn("ok += 1", chunk)

    def test_notify_new_ticket_returns_int(self):
        src = (ROOT / "app/services/notifications.py").read_text(encoding="utf-8")
        chunk = src.split("async def notify_new_ticket")[1].split("async def ")[0]
        self.assertIn("-> int", chunk)
        self.assertIn("html_mod.escape", chunk)

    def test_support_notifies_before_success_ack(self):
        src = (ROOT / "app/bot/handlers/support.py").read_text(encoding="utf-8")
        chunk = src.split("async def support_body")[1].split("@router.")[0]
        # notify runs before the success restore_main_reply call
        self.assertIn("await notify_new_ticket(", chunk)
        self.assertIn("await restore_main_reply(", chunk)
        self.assertLess(
            chunk.find("await notify_new_ticket("),
            chunk.rfind("await restore_main_reply("),
        )

class MiniappShopSuppressionTests(unittest.TestCase):
    def test_start_skips_miniapp_on_shop(self):
        src = (ROOT / "app/bot/handlers/start.py").read_text(encoding="utf-8")
        chunk = src.split("async def render_home")[1].split("async def ")[0]
        self.assertIn("if not is_reseller_bot:", chunk)
        self.assertIn("miniapp_inline_keyboard", chunk)

    def test_keyboard_refuses_shop_context(self):
        src = (ROOT / "app/bot/keyboards.py").read_text(encoding="utf-8")
        chunk = src.split("def miniapp_inline_keyboard")[1].split("\ndef ")[0]
        self.assertIn("current_shop_reseller_id", chunk)

    def test_shop_settings_strip_miniapp(self):
        src = (ROOT / "app/services/users.py").read_text(encoding="utf-8")
        chunk = src.split("async def get_all_settings")[1].split("\ndef on")[0]
        self.assertIn('"miniapp"', chunk)
        self.assertIn('data["show_miniapp"] = "0"', chunk)

    def test_seed_shop_safe_menu_order(self):
        src = (ROOT / "app/services/resellers.py").read_text(encoding="utf-8")
        chunk = src.split("async def seed_reseller_shop_settings")[1].split("\nasync def ")[0]
        self.assertIn("shop,services,wallet,support,referral", chunk)
        self.assertNotIn("miniapp", chunk.split("value =")[1].split("\n")[0])


if __name__ == "__main__":
    unittest.main()
