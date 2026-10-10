"""Pure-inline staff nav: thin reply KB + one edited panel (v0.2.43)."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch


class AdminMainReplyInlineTests(unittest.TestCase):
    def test_inline_admin_uses_user_menu_plus_panel_entry(self):
        import app.bot.keyboards  # noqa: F401
        from app.bot.reply_keyboards import main_reply_keyboard
        from app.db.models import Role

        ui = {
            "nav_mode": "inline",
            "menu_layout": "compact",
            "menu_order": "shop,wallet,support",
            "btn_shop": "خرید",
            "btn_wallet": "کیف",
            "btn_support": "پشتیبانی",
            "btn_admin": "🛠 پنل ادمین",
            "loyalty_enabled": "0",
        }
        labels = [
            b.text
            for row in main_reply_keyboard(
                Role.ADMIN.value, has_services=False, ui=ui
            ).keyboard
            for b in row
        ]
        self.assertIn("🛠 پنل ادمین", labels)
        self.assertIn("خرید", labels)
        self.assertNotIn("🗓 عملیات روزانه", labels)
        self.assertNotIn("🛠 سیستم", labels)

    def test_classic_admin_keeps_four_groups(self):
        import app.bot.keyboards  # noqa: F401
        from app.bot.reply_keyboards import main_reply_keyboard
        from app.db.models import Role

        ui = {
            "nav_mode": "classic",
            "menu_layout": "compact",
            "btn_menu_home": "🏠 منوی اصلی",
        }
        labels = [
            b.text
            for row in main_reply_keyboard(
                Role.ADMIN.value, has_services=False, ui=ui
            ).keyboard
            for b in row
        ]
        self.assertIn("🗓 عملیات روزانه", labels)
        self.assertIn("🛠 سیستم", labels)


class ResellerThinReplyTests(unittest.TestCase):
    def test_inline_reseller_hub_is_thin(self):
        import app.bot.keyboards  # noqa: F401
        from app.bot.reply_keyboards import reseller_hub_main_keyboard

        labels = [
            b.text
            for row in reseller_hub_main_keyboard(
                None, {"nav_mode": "inline"}
            ).keyboard
            for b in row
        ]
        self.assertEqual(
            labels,
            ["🤝 پنل مدیریت", "👁 پیش‌نمایش منوی کاربر"],
        )


class GroupHubBackHomeTests(unittest.TestCase):
    def test_group_backs_go_to_admin_home(self):
        from app.bot.nav_inline import (
            admin_ops_hub_keyboard,
            admin_people_hub_keyboard,
            admin_product_hub_keyboard,
            admin_system_hub_keyboard,
        )

        for kb_fn in (
            admin_ops_hub_keyboard,
            lambda ui: admin_people_hub_keyboard(ui, can_manage_representatives=True),
            lambda ui: admin_product_hub_keyboard(
                ui, pg_features=frozenset({"pg_users"})
            ),
            admin_system_hub_keyboard,
        ):
            data = [
                b.callback_data for row in kb_fn({}).inline_keyboard for b in row
            ]
            self.assertIn("nv:adm:home", data)
            self.assertNotIn("nv:adm:close", data)


class OpenAdminHomePanelTests(unittest.IsolatedAsyncioTestCase):
    async def test_inline_home_presents_groups_panel(self):
        from app.bot.handlers.reply_nav import open_admin_home

        message = AsyncMock()
        message.from_user = MagicMock(is_bot=False)
        message.answer = AsyncMock(return_value=MagicMock())
        session = AsyncMock()
        db_user = MagicMock()
        state = AsyncMock()
        state.get_data = AsyncMock(return_value={})
        state.update_data = AsyncMock()

        with (
            patch(
                "app.bot.handlers.reply_nav._deny_unless_owner",
                AsyncMock(return_value=True),
            ),
            patch(
                "app.bot.handlers.reply_nav.get_all_settings",
                AsyncMock(return_value={"nav_mode": "inline"}),
            ),
            patch(
                "app.bot.handlers.reply_nav.nav.set_nav_level",
                new_callable=AsyncMock,
            ),
            patch(
                "app.bot.nav_inline.present_nav_panel",
                new_callable=AsyncMock,
            ) as present,
            patch(
                "app.bot.nav_inline.admin_groups_hub_keyboard",
                return_value="GROUPS",
            ),
        ):
            await open_admin_home(message, session, db_user, state)

        present.assert_awaited()
        self.assertEqual(present.await_args.kwargs.get("inline"), "GROUPS")


class LastingStaffReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_inline_returns_main_not_classic(self):
        from app.bot.nav_chrome import lasting_staff_reply

        session = AsyncMock()
        db_user = MagicMock()
        main_kb = MagicMock(name="MAIN")
        classic = MagicMock(name="CLASSIC")

        with (
            patch(
                "app.bot.nav_chrome.get_all_settings",
                AsyncMock(return_value={"nav_mode": "inline"}),
            ),
            patch(
                "app.bot.menu_nav.build_main_reply_keyboard",
                AsyncMock(return_value=(main_kb, {}, "admin")),
            ),
        ):
            out = await lasting_staff_reply(session, db_user, classic=classic)

        self.assertIs(out, main_kb)

    async def test_classic_keeps_submenu(self):
        from app.bot.nav_chrome import lasting_staff_reply

        session = AsyncMock()
        db_user = MagicMock()
        classic = MagicMock(name="CLASSIC")

        with patch(
            "app.bot.nav_chrome.get_all_settings",
            AsyncMock(return_value={"nav_mode": "classic"}),
        ):
            out = await lasting_staff_reply(session, db_user, classic=classic)

        self.assertIs(out, classic)


class AnswerStaffNavReopenTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_heals_main_and_reopens_panel(self):
        from app.bot.nav_chrome import answer_staff_nav

        message = AsyncMock()
        message.answer = AsyncMock()
        session = AsyncMock()
        db_user = MagicMock()
        state = AsyncMock()
        main_kb = MagicMock(name="MAIN")
        reopen = AsyncMock()

        with (
            patch(
                "app.bot.nav_chrome.get_all_settings",
                AsyncMock(return_value={"nav_mode": "inline"}),
            ),
            patch(
                "app.bot.menu_nav.build_main_reply_keyboard",
                AsyncMock(return_value=(main_kb, {}, "admin")),
            ),
        ):
            await answer_staff_nav(
                message,
                session,
                db_user,
                text="لغو شد.",
                classic=MagicMock(),
                state=state,
                reopen_panel=reopen,
                clear_state=True,
            )

        message.answer.assert_awaited()
        self.assertIs(message.answer.await_args.kwargs.get("reply_markup"), main_kb)
        state.clear.assert_awaited()
        reopen.assert_awaited()


if __name__ == "__main__":
    unittest.main()
