"""Wave E: admin/reseller leaf hubs as inline (no submenu chrome)."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch


class LeafHubKeyboardTests(unittest.TestCase):
    def test_users_and_settings_callbacks(self):
        from app.bot.nav_inline import (
            admin_settings_hub_keyboard,
            admin_users_hub_keyboard,
        )

        users = [
            b.callback_data
            for row in admin_users_hub_keyboard({}).inline_keyboard
            for b in row
        ]
        self.assertIn("adm:users:list:0", users)
        self.assertIn("nv:adm:people", users)

        settings = [
            b.callback_data
            for row in admin_settings_hub_keyboard({}).inline_keyboard
            for b in row
        ]
        self.assertIn("adm:st:sec:shop", settings)
        self.assertIn("nv:adm:ra:adm_st_panel", settings)
        self.assertIn("nv:adm:system", settings)

    def test_backup_broadcast_plans(self):
        from app.bot.nav_inline import (
            admin_backup_hub_keyboard,
            admin_broadcast_hub_keyboard,
            admin_plans_audience_hub_keyboard,
            admin_plans_add_type_hub_keyboard,
        )

        bak = [
            b.callback_data
            for row in admin_backup_hub_keyboard({}).inline_keyboard
            for b in row
        ]
        self.assertIn("adm:backup:create:noenv", bak)
        self.assertIn("nv:adm:backup", bak)

        bc = [
            b.callback_data
            for row in admin_broadcast_hub_keyboard({}).inline_keyboard
            for b in row
        ]
        self.assertIn("adm:broadcast:aud:all", bc)

        pa = [
            b.callback_data
            for row in admin_plans_audience_hub_keyboard({}).inline_keyboard
            for b in row
        ]
        self.assertIn("nv:adm:plans:aud:users", pa)
        self.assertIn("nv:adm:ra:adm_plans_categories", pa)

        pt = [
            b.callback_data
            for row in admin_plans_add_type_hub_keyboard(
                "users", {}
            ).inline_keyboard
            for b in row
        ]
        self.assertTrue(any(d.startswith("nv:adm:ra:adm_plans_kind_") for d in pt))

    def test_reseller_leaf_hubs(self):
        from app.bot.nav_inline import (
            reseller_plans_hub_keyboard,
            reseller_settings_hub_keyboard,
        )

        rs = [
            b.callback_data
            for row in reseller_settings_hub_keyboard({}).inline_keyboard
            for b in row
        ]
        self.assertIn("nv:res:ra:res_st_shop", rs)
        self.assertIn("nv:res:home", rs)
        self.assertNotIn("nv:res:close", rs)

        rp = [
            b.callback_data
            for row in reseller_plans_hub_keyboard({}).inline_keyboard
            for b in row
        ]
        self.assertIn("nv:res:ra:res_plan_add", rp)


class OpenAdminUsersInlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_inline_users_hub_one_message(self):
        from app.bot.handlers.reply_nav import open_admin_users_hub

        message = AsyncMock()
        message.answer = AsyncMock(return_value=MagicMock())
        session = AsyncMock()
        session.scalar = AsyncMock(side_effect=[10, 1, 3])
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
                "app.bot.handlers.reply_nav.admin_customer_counts",
                AsyncMock(
                    return_value={"users": 10, "blocked": 1, "orders": 3}
                ),
            ),
            patch(
                "app.bot.handlers.reply_nav.nav.show_nav_keyboard",
                new_callable=AsyncMock,
            ) as show_nav,
        ):
            await open_admin_users_hub(message, session, db_user, state)

        self.assertEqual(message.answer.await_count, 1)
        markup = message.answer.await_args.kwargs.get("reply_markup")
        data = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("adm:users:list:0", data)
        show_nav.assert_not_awaited()


class OpenAdminBackupInlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_inline_backup_no_chrome_attach(self):
        from app.bot.handlers.reply_nav import open_admin_backup_hub

        message = AsyncMock()
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
                "asyncio.to_thread",
                AsyncMock(return_value=[]),
            ),
            patch(
                "app.bot.handlers.admin_backup._hub_text",
                return_value="FILES",
            ),
            patch(
                "app.bot.handlers.reply_nav.kb.backup_files_keyboard",
                return_value=MagicMock(inline_keyboard=[]),
            ),
            patch(
                "app.bot.tg_utils.attach_reply_keyboard",
                new_callable=AsyncMock,
            ) as attach,
            patch(
                "app.bot.handlers.reply_nav.nav.show_nav_keyboard",
                new_callable=AsyncMock,
            ) as show_nav,
        ):
            await open_admin_backup_hub(message, session, db_user, state)

        # one combined panel (actions + files) — no chrome attach
        self.assertEqual(message.answer.await_count, 1)
        attach.assert_not_awaited()
        show_nav.assert_not_awaited()


class OpenAdminPlansInlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_plans_hub_inline(self):
        from app.bot.handlers.reply_nav import open_admin_plans_hub

        message = AsyncMock()
        message.answer = AsyncMock(return_value=MagicMock())
        session = AsyncMock()
        db_user = MagicMock()
        state = AsyncMock()
        state.set_state = AsyncMock()
        state.update_data = AsyncMock()
        state.get_data = AsyncMock(return_value={})

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
                "app.bot.handlers.reply_nav.nav.show_nav_keyboard",
                new_callable=AsyncMock,
            ) as show_nav,
        ):
            await open_admin_plans_hub(message, session, db_user, state)

        markup = message.answer.await_args.kwargs.get("reply_markup")
        data = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("nv:adm:plans:aud:users", data)
        show_nav.assert_not_awaited()


class PresentPlansAudienceInlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_audience_presents_kind_hub(self):
        from app.bot.handlers.reply_nav import present_admin_plans_audience

        message = AsyncMock()
        message.answer = AsyncMock(return_value=MagicMock())
        session = AsyncMock()
        db_user = MagicMock()
        state = AsyncMock()
        state.update_data = AsyncMock()
        state.get_data = AsyncMock(return_value={})

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
                "app.bot.handlers.admin_plans.send_users_plans_overview",
                new_callable=AsyncMock,
            ) as overview,
            patch(
                "app.bot.handlers.reply_nav.nav.show_nav_keyboard",
                new_callable=AsyncMock,
            ) as show_nav,
        ):
            await present_admin_plans_audience(
                message, session, db_user, state, audience="users"
            )

        markup = message.answer.await_args.kwargs.get("reply_markup")
        data = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("nv:adm:ra:adm_plans_add", data)
        overview.assert_awaited()
        show_nav.assert_not_awaited()


class ShowNavLeafInlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_users_level_keeps_main_kb(self):
        from app.bot import menu_nav as nav

        message = AsyncMock()
        message.answer = AsyncMock(return_value=MagicMock())
        session = AsyncMock()
        db_user = MagicMock()
        state = AsyncMock()
        state.get_data = AsyncMock(return_value={})
        state.update_data = AsyncMock()
        main_kb = MagicMock(name="MAIN")

        with (
            patch(
                "app.bot.menu_nav.get_all_settings",
                AsyncMock(return_value={"nav_mode": "inline"}),
            ),
            patch(
                "app.bot.menu_nav.build_main_reply_keyboard",
                AsyncMock(return_value=(main_kb, {}, "admin")),
            ),
            patch(
                "app.bot.menu_nav.kb.admin_users_reply_keyboard",
                return_value="USERS_CHROME",
            ) as chrome,
        ):
            await nav.show_nav_keyboard(
                message,
                session,
                db_user,
                nav.NAV_ADMIN_USERS,
                text="users",
                state=state,
                is_reseller_bot=False,
                reseller_owner_id=None,
            )

        chrome.assert_not_called()
        self.assertIs(
            message.answer.await_args.kwargs.get("reply_markup"),
            main_kb,
        )


if __name__ == "__main__":
    unittest.main()
