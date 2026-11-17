"""3.5.4 — Hard shop isolation: notifies, main-bot panel, PG shop credentials."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]


class ShopNotifyIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_shop_event_never_hits_platform_admins(self):
        from app.services.notifications import _dispatch_dual_notify

        bot = MagicMock()
        session = AsyncMock()
        with patch(
            "app.services.notifications.notify_enabled", new=AsyncMock(return_value=True)
        ), patch(
            "app.services.notifications._send_to_chats", new=AsyncMock()
        ) as send, patch(
            "app.services.notifications._resolve_shop_reseller_id",
            new=AsyncMock(return_value=42),
        ), patch(
            "app.services.notifications._shop_recipient_chat_ids",
            new=AsyncMock(return_value=[9001]),
        ), patch(
            "app.services.resellers.get_reseller_profile",
            new=AsyncMock(return_value=MagicMock(bot_token="shop:token")),
        ), patch(
            "app.services.reseller_bots.open_notify_bot_for_reseller",
            new=AsyncMock(
                return_value=(MagicMock(session=MagicMock(close=AsyncMock())), True)
            ),
        ), patch(
            "app.services.notifications.get_settings"
        ) as gs:
            gs.return_value.admin_ids = [1, 2, 3]
            await _dispatch_dual_notify(
                bot, session, "notify_new_ticket", "t", ticket_user_id=7
            )
        self.assertEqual(send.await_count, 1)
        # Must be shop targets only — not ADMIN_IDS
        self.assertEqual(send.await_args.args[1], [9001])


class MainBotResellerPanelTests(unittest.TestCase):
    def test_keyboards_have_creds_callback(self):
        src = (ROOT / "app/bot/keyboards.py").read_text(encoding="utf-8")
        self.assertIn("res:creds", src)
        self.assertIn("show_reseller_creds", src)

    def test_reseller_handler_has_creds(self):
        src = (ROOT / "app/bot/handlers/reseller.py").read_text(encoding="utf-8")
        self.assertIn('F.data == "res:creds"', src)
        self.assertIn("format_reseller_access_card", src)
        self.assertIn("if not is_reseller_bot:", src)

    def test_resolve_main_bot_no_actor(self):
        from app.services.reseller_access import resolve_reseller_owner_id
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        user = SimpleNamespace(id=5, role="reseller", telegram_id=1)

        async def _run():
            return await resolve_reseller_owner_id(
                MagicMock(), user, is_reseller_bot=False
            )

        self.assertIsNone(asyncio.run(_run()))


class PlatformCannotTouchShopTests(unittest.TestCase):
    def test_ordrev_guards_shop_orders(self):
        src = (ROOT / "app/bot/handlers/admin.py").read_text(encoding="utf-8")
        self.assertIn("order.reseller_id", src)
        self.assertIn("مربوط به نماینده است", src)

    def test_ticket_actor_skips_shop_tickets(self):
        from types import SimpleNamespace

        from app.services.ticket_scope import staff_ticket_in_scope
        from tests.test_update_owner_only import OWNER_SESSION

        shop_ticket = SimpleNamespace(reseller_id=5)
        platform_ticket = SimpleNamespace(reseller_id=None)
        self.assertFalse(staff_ticket_in_scope(OWNER_SESSION, shop_ticket))
        self.assertTrue(staff_ticket_in_scope(OWNER_SESSION, platform_ticket))

    def test_list_open_tickets_platform_only(self):
        src = (ROOT / "app/services/tickets.py").read_text(encoding="utf-8")
        self.assertIn("platform_only", src)
        admin = (ROOT / "app/bot/handlers/admin.py").read_text(encoding="utf-8")
        self.assertIn("platform_only=True", admin)


class PgShopCredentialsTests(unittest.TestCase):
    def test_secret_box_roundtrip(self):
        from app.services.secret_box import decrypt_secret, encrypt_secret

        enc = encrypt_secret("SecretPass!23")
        self.assertTrue(enc)
        self.assertEqual(decrypt_secret(enc), "SecretPass!23")

    def test_get_pg_for_reseller_exists(self):
        from app.services import pasarguard

        self.assertTrue(hasattr(pasarguard, "get_pg_for_reseller"))
        self.assertIn("pg_admin_password_enc", (ROOT / "app/db/models.py").read_text())

    def test_deliver_order_uses_reseller_client(self):
        src = (ROOT / "app/services/orders.py").read_text(encoding="utf-8")
        self.assertIn("get_pg_for_reseller", src)


if __name__ == "__main__":
    unittest.main()
