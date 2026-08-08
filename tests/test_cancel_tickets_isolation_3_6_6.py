"""3.6.6 — cancel SkipHandler fix; shop settings isolation; ticket shop scope."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class Version366Tests(unittest.TestCase):
    def test_version(self):
        from app.version import __version__

        self.assertEqual(__version__, "4.9.0")
        self.assertEqual((ROOT / "VERSION").read_text(encoding="utf-8").strip(), "4.9.0")
        notes = (ROOT / "app/services/release_notes.py").read_text(encoding="utf-8")
        self.assertIn('"3.6.8"', notes)


class CancelSkipHandlerTests(unittest.TestCase):
    def test_orphan_cancel_imports_from_bases(self):
        src = (ROOT / "app/bot/handlers/start.py").read_text(encoding="utf-8")
        self.assertIn("aiogram.dispatcher.event.bases", src)
        self.assertNotIn("from aiogram.exceptions import SkipHandler", src)

    def test_global_cancel_defers_all_fsm(self):
        src = (ROOT / "app/bot/handlers/reply_nav.py").read_text(encoding="utf-8")
        chunk = src.split("async def global_cancel_restore")[1].split("@router.")[0]
        self.assertIn("aiogram.dispatcher.event.bases", chunk)
        self.assertIn("if current is not None", chunk)
        self.assertIn("raise SkipHandler()", chunk)

    def test_error_middleware_reraises_skip(self):
        src = (ROOT / "app/bot/middlewares.py").read_text(encoding="utf-8")
        chunk = src.split("class ErrorLogMiddleware")[1].split("class ")[0]
        self.assertIn("SkipHandler", chunk)
        self.assertIn("CancelHandler", chunk)
        self.assertIn("raise", chunk)

    def test_skiphandler_importable(self):
        from aiogram.dispatcher.event.bases import SkipHandler

        self.assertTrue(issubclass(SkipHandler, Exception))


class ShopSettingsIsolationTests(unittest.TestCase):
    def test_get_all_settings_no_platform_overlay(self):
        src = (ROOT / "app/services/users.py").read_text(encoding="utf-8")
        chunk = src.split("async def get_all_settings")[1].split("\nasync def ")[0]
        self.assertIn("Never inherit live platform", chunk)
        self.assertNotIn("Start from global (fallback)", chunk)
        # When rid is set, only ResellerSetting is queried (no select(Setting) before return)
        rid_branch = chunk.split("if rid:")[1].split("result = await session.execute(select(Setting))")[0]
        self.assertIn("ResellerSetting", rid_branch)
        self.assertNotIn("select(Setting)", rid_branch)

    def test_get_setting_defaults_not_platform(self):
        src = (ROOT / "app/services/users.py").read_text(encoding="utf-8")
        chunk = src.split("async def get_setting")[1].split("async def set_setting")[0]
        self.assertIn("never fall through to live platform", chunk)
        self.assertIn("DEFAULT_SETTINGS.get(key, default)", chunk)

    def test_platform_set_clears_only_platform_cache(self):
        src = (ROOT / "app/services/users.py").read_text(encoding="utf-8")
        chunk = src.split("async def set_setting")[1].split("async def set_settings_bulk")[0]
        self.assertIn("clear_settings_cache(0)", chunk)
        # bare clear_settings_cache() would wipe shops
        self.assertNotIn("clear_settings_cache()\n", chunk)

    def test_reseller_layout_toggle_persian(self):
        src = (ROOT / "app/bot/handlers/reseller_settings.py").read_text(encoding="utf-8")
        self.assertIn('"menu_layout"', src)
        self.assertIn("فشرده (جفتی)", src)
        self.assertIn("کلاسیک (تکی)", src)
        self.assertIn("res:st:menu:layout", src)


class TicketShopScopeTests(unittest.TestCase):
    def test_ticket_model_has_reseller_id(self):
        from app.db.models import Ticket

        self.assertTrue(hasattr(Ticket, "reseller_id"))

    def test_migration_adds_column(self):
        src = (ROOT / "app/db/session.py").read_text(encoding="utf-8")
        self.assertIn('ADD COLUMN reseller_id INTEGER', src)
        self.assertIn('has_table("tickets")', src)

    def test_create_ticket_uses_shop_context(self):
        src = (ROOT / "app/services/tickets.py").read_text(encoding="utf-8")
        self.assertIn("current_shop_reseller_id", src)
        self.assertIn("reseller_id=", src)

    def test_notify_accepts_ticket_reseller_id(self):
        src = (ROOT / "app/services/notifications.py").read_text(encoding="utf-8")
        self.assertIn("ticket_reseller_id", src)
        self.assertIn("open_notify_bot_for_reseller", src)

    def test_admin_cannot_open_shop_ticket(self):
        src = (ROOT / "app/bot/handlers/admin.py").read_text(encoding="utf-8")
        chunk = src.split("async def adm_ticket_view")[1].split("async def")[0]
        self.assertIn("ticket.reseller_id", chunk)
        self.assertIn("فروشگاه نماینده", chunk)

    def test_support_passes_reseller_id(self):
        src = (ROOT / "app/bot/handlers/support.py").read_text(encoding="utf-8")
        self.assertIn("reseller_id=reseller_owner_id", src)
        self.assertIn("ticket_reseller_id=ticket.reseller_id", src)
        # user replies also pass shop scope
        chunk = src.split("async def support_reply")[1].split("@router.")[0]
        self.assertIn("ticket_reseller_id=ticket.reseller_id", chunk)


class MiddlewareFailClosedTests(unittest.TestCase):
    def test_unknown_shop_token_rejected(self):
        src = (ROOT / "app/bot/middlewares.py").read_text(encoding="utf-8")
        chunk = src.split("class UserMiddleware")[1].split("class ")[0]
        self.assertIn("not a known reseller shop", chunk)
        self.assertIn("return None", chunk)


class TicketNotifyAuthorityTests(unittest.TestCase):
    def test_explicit_none_not_sticky(self):
        src = (ROOT / "app/services/notifications.py").read_text(encoding="utf-8")
        self.assertIn("_TICKET_RID_UNSET", src)
        chunk = src.split("async def _resolve_shop_reseller_id")[1].split("async def ")[0]
        self.assertIn("is not _TICKET_RID_UNSET", chunk)

    def test_platform_notify_prefs_force_platform(self):
        src = (ROOT / "app/services/notifications.py").read_text(encoding="utf-8")
        chunk = src.split("async def get_notify_prefs")[1].split("async def ")[0]
        self.assertIn("reseller_id=0", chunk)

    def test_panel_admin_tg_filters_ticket_reseller(self):
        src = (ROOT / "app/api/panel_tickets_pages.py").read_text(encoding="utf-8")
        self.assertIn("Ticket.reseller_id.is_(None)", src)

    def test_no_main_bot_ticket_notify_fallback(self):
        src = (ROOT / "app/services/reseller_bots.py").read_text(encoding="utf-8")
        chunk = src.split("async def open_notify_bot_for_reseller")[1].split("\ndef ")[0]
        self.assertIn("return None, False", chunk)
        self.assertIn("do not fall back to main bot", chunk)
        # Must not create the main/platform bot as a silent fallback
        self.assertNotIn("return create_bot(), True", chunk)


class WalletCancelTests(unittest.TestCase):
    def test_waiting_receipt_cancel(self):
        src = (ROOT / "app/bot/handlers/wallet.py").read_text(encoding="utf-8")
        self.assertIn("WalletStates.waiting_receipt", src)
        self.assertIn("async def wallet_receipt_cancel", src)
        self.assertIn("async def wallet_choose_method_cancel", src)


class ShopSeedTests(unittest.TestCase):
    def test_seed_on_setup(self):
        src = (ROOT / "app/services/resellers.py").read_text(encoding="utf-8")
        self.assertIn("seed_reseller_shop_settings", src)
        self.assertIn('"menu_layout"', src)


if __name__ == "__main__":
    unittest.main()
