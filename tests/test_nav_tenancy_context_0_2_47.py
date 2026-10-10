"""Required bot-context kwargs so shop bots never heal to the wrong menu."""

from __future__ import annotations

import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import app.bot.keyboards  # noqa: F401 — resolve reply_keyboards circular init


def _labels(markup) -> list[str]:
    return [btn.text for row in markup.keyboard for btn in row]


class RequiredContextSignatureTests(unittest.TestCase):
    def test_core_keyboard_helpers_require_bot_context(self):
        from app.bot import menu_nav, nav_chrome

        for fn in (
            menu_nav.build_main_reply_keyboard,
            menu_nav.restore_main_reply,
            menu_nav.show_nav_keyboard,
            menu_nav.present_order_pay,
            menu_nav.buyer_main_reply_keyboard,
            nav_chrome.lasting_staff_reply,
            nav_chrome.answer_staff_nav,
        ):
            params = inspect.signature(fn).parameters
            for name in ("is_reseller_bot", "reseller_owner_id"):
                self.assertIn(name, params, msg=f"{fn.__name__} missing {name}")
                self.assertIs(
                    params[name].default,
                    inspect.Parameter.empty,
                    msg=f"{fn.__name__}.{name} must not default",
                )

    def test_present_topup_methods_requires_bot_context(self):
        from app.bot.handlers.wallet import present_topup_methods

        params = inspect.signature(present_topup_methods).parameters
        for name in ("is_reseller_bot", "reseller_owner_id"):
            self.assertIs(params[name].default, inspect.Parameter.empty)


class BuildMainReplyTenancyTests(unittest.IsolatedAsyncioTestCase):
    async def test_shop_bot_owner_gets_reseller_hub_not_admin(self):
        from app.bot.menu_nav import build_main_reply_keyboard
        from app.services.users import DEFAULT_SETTINGS

        session = AsyncMock()
        db_user = SimpleNamespace(id=5, role="reseller", telegram_id=100, full_name="R")
        profile = SimpleNamespace(
            id=1,
            user_id=5,
            is_active=True,
            bot_username="shopbot",
            bot_admin_ids="",
        )
        ui = dict(DEFAULT_SETTINGS)
        ui["menu_order"] = "shop,wallet,support,services"
        ui["menu_layout"] = "compact"

        with (
            patch(
                "app.services.reseller_access.effective_menu_role",
                new=AsyncMock(return_value="reseller"),
            ),
            patch(
                "app.services.reseller_access.load_reseller_actor",
                new=AsyncMock(return_value=(5, profile)),
            ),
            patch(
                "app.services.representative_unification.shop_bot_can_manage_representatives",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "app.bot.menu_nav.get_all_settings",
                new=AsyncMock(return_value=ui),
            ),
        ):
            markup, _, role = await build_main_reply_keyboard(
                session,
                db_user,
                is_reseller_bot=True,
                reseller_owner_id=5,
                ui=ui,
            )

        self.assertEqual(role, "reseller")
        labels = _labels(markup)
        self.assertTrue(
            any("نمایند" in (x or "") or "پیش‌نمایش" in (x or "") for x in labels),
            labels,
        )
        forbidden = ("پنل ادمین", "عملیات روزانه", "پاسارگارد", "بکاپ", "برودکست")
        for bad in forbidden:
            self.assertFalse(
                any(bad in (x or "") for x in labels),
                f"shop-bot owner leaked {bad!r} in {labels}",
            )

    async def test_shop_bot_customer_never_sees_admin_or_reseller_hub(self):
        from app.bot.menu_nav import build_main_reply_keyboard
        from app.services.users import DEFAULT_SETTINGS

        session = AsyncMock()
        db_user = SimpleNamespace(id=9, role="user", telegram_id=777, full_name="C")
        ui = dict(DEFAULT_SETTINGS)
        ui["menu_order"] = "shop,wallet,support,services,miniapp"
        ui["menu_layout"] = "compact"

        with (
            patch(
                "app.services.reseller_access.effective_menu_role",
                new=AsyncMock(return_value="user"),
            ),
            patch(
                "app.services.reseller_access.is_shop_owner_on_main_bot",
                return_value=False,
            ),
            patch(
                "app.bot.menu_nav.user_has_services",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "app.bot.menu_nav.get_all_settings",
                new=AsyncMock(return_value=ui),
            ),
        ):
            markup, _, role = await build_main_reply_keyboard(
                session,
                db_user,
                is_reseller_bot=True,
                reseller_owner_id=5,
                ui=ui,
            )

        self.assertEqual(role, "user")
        labels = _labels(markup)
        for bad in ("پنل ادمین", "پنل نماینده", "پیش‌نمایش منوی کاربر", "پاسارگارد"):
            self.assertFalse(
                any(bad in (x or "") for x in labels),
                f"shop customer leaked {bad!r} in {labels}",
            )

    async def test_platform_admin_keeps_admin_entry_on_main_bot(self):
        from app.bot.menu_nav import build_main_reply_keyboard
        from app.services.users import DEFAULT_SETTINGS

        session = AsyncMock()
        db_user = SimpleNamespace(id=1, role="admin", telegram_id=1, full_name="A")
        ui = dict(DEFAULT_SETTINGS)
        ui["menu_order"] = "shop,wallet,support,services"
        ui["menu_layout"] = "compact"
        ui["btn_admin"] = "🛠 پنل ادمین"

        with (
            patch(
                "app.services.reseller_access.effective_menu_role",
                new=AsyncMock(return_value="admin"),
            ),
            patch(
                "app.bot.menu_nav._platform_admin_menu_flags",
                new=AsyncMock(return_value=(frozenset(), True)),
            ),
            patch(
                "app.bot.menu_nav.user_has_services",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "app.services.reseller_access.is_shop_owner_on_main_bot",
                return_value=False,
            ),
            patch(
                "app.bot.menu_nav.get_all_settings",
                new=AsyncMock(return_value=ui),
            ),
        ):
            markup, _, role = await build_main_reply_keyboard(
                session,
                db_user,
                is_reseller_bot=False,
                reseller_owner_id=None,
                ui=ui,
            )

        self.assertEqual(role, "admin")
        labels = _labels(markup)
        self.assertTrue(any("ادمین" in (x or "") for x in labels), labels)


class PresentTopupMethodsContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_heal_main_forwards_shop_bot_context(self):
        from app.bot.handlers.wallet import present_topup_methods

        message = AsyncMock()
        session = AsyncMock()
        db_user = MagicMock()
        state = AsyncMock()
        state.set_state = AsyncMock()
        state.update_data = AsyncMock()
        captured: dict = {}

        async def _capture_build(*_a, **kw):
            captured.update(kw)
            return MagicMock(), {}, "reseller"

        with (
            patch(
                "app.bot.handlers.wallet.get_all_settings",
                new=AsyncMock(return_value={}),
            ),
            patch(
                "app.bot.nav_inline.present_inline_only",
                new=AsyncMock(),
            ),
            patch(
                "app.bot.menu_nav.set_nav_level",
                new=AsyncMock(),
            ),
            patch(
                "app.bot.menu_nav.build_main_reply_keyboard",
                new=_capture_build,
            ),
            patch(
                "app.bot.tg_utils.attach_reply_keyboard",
                new=AsyncMock(),
            ),
        ):
            await present_topup_methods(
                message,
                session,
                db_user,
                state,
                5000,
                is_reseller_bot=True,
                reseller_owner_id=42,
                heal_main=True,
            )

        self.assertTrue(captured.get("is_reseller_bot"))
        self.assertEqual(captured.get("reseller_owner_id"), 42)


if __name__ == "__main__":
    unittest.main()
