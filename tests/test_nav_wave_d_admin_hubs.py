"""Wave D: admin / PG / reseller manage hubs as inline (no submenu chrome)."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch


class AdminHubKeyboardTests(unittest.TestCase):
    def test_groups_and_ops_callbacks(self):
        from app.bot.nav_inline import (
            admin_groups_hub_keyboard,
            admin_ops_hub_keyboard,
        )

        groups = [
            b.callback_data
            for row in admin_groups_hub_keyboard({}).inline_keyboard
            for b in row
        ]
        self.assertEqual(
            groups,
            [
                "nv:adm:ops",
                "nv:adm:people",
                "nv:adm:product",
                "nv:adm:system",
                "menu:home",
            ],
        )
        ops = [
            b.callback_data
            for row in admin_ops_hub_keyboard({}).inline_keyboard
            for b in row
        ]
        self.assertIn("adm:dash", ops)
        self.assertIn("adm:orders", ops)
        self.assertIn("nv:adm:home", ops)

    def test_pg_reuses_adm_pg_prefix(self):
        from app.bot.nav_inline import pg_hub_keyboard

        feats = frozenset(
            {
                "pg_overview",
                "pg_users",
                "pg_nodes",
                "pg_groups",
                "pg_templates",
            }
        )
        data = [
            b.callback_data
            for row in pg_hub_keyboard(
                {}, features=feats, can_create_user=True
            ).inline_keyboard
            for b in row
        ]
        self.assertTrue(all(d.startswith(("adm:pg:", "nv:adm:")) for d in data))
        self.assertIn("adm:pg:stats", data)
        self.assertIn("nv:adm:product", data)

    def test_people_hides_resellers_when_disabled(self):
        from app.bot.nav_inline import admin_people_hub_keyboard

        data = [
            b.callback_data
            for row in admin_people_hub_keyboard(
                {}, can_manage_representatives=False
            ).inline_keyboard
            for b in row
        ]
        self.assertNotIn("nv:adm:resellers", data)
        self.assertIn("nv:adm:users", data)
        self.assertIn("nv:adm:loy", data)

    def test_loyalty_manage_callbacks(self):
        from app.bot.nav_inline import admin_loyalty_manage_hub_keyboard

        data = [
            b.callback_data
            for row in admin_loyalty_manage_hub_keyboard({}).inline_keyboard
            for b in row
        ]
        self.assertIn("nv:adm:loy:overview", data)
        self.assertIn("nv:adm:loy:tiers", data)
        self.assertIn("nv:adm:people", data)

    def test_reseller_manage_uses_ra_prefix(self):
        from app.bot.nav_inline import reseller_manage_hub_keyboard

        profile = MagicMock()
        with patch(
            "app.bot.reply_keyboards._reseller_submenu_entries",
            return_value=[("res_dash", "خانه"), ("res_plans", "پلن")],
        ):
            data = [
                b.callback_data
                for row in reseller_manage_hub_keyboard(
                    profile, {}
                ).inline_keyboard
                for b in row
            ]
        self.assertIn("nv:res:ra:res_dash", data)
        self.assertIn("nv:res:ra:res_plans", data)
        self.assertIn("menu:home", data)


class OpenAdminOpsInlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_inline_one_message_ops_hub(self):
        from app.bot.handlers.reply_nav import open_admin_ops_hub

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
                "app.bot.handlers.reply_nav.nav.show_nav_keyboard",
                new_callable=AsyncMock,
            ) as show_nav,
        ):
            await open_admin_ops_hub(message, session, db_user, state)

        message.answer.assert_awaited()
        self.assertEqual(message.answer.await_count, 1)
        markup = message.answer.await_args.kwargs.get("reply_markup")
        self.assertTrue(hasattr(markup, "inline_keyboard"))
        data = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("adm:dash", data)
        show_nav.assert_not_awaited()

    async def test_classic_still_show_nav(self):
        from app.bot.handlers.reply_nav import open_admin_ops_hub

        message = AsyncMock()
        session = AsyncMock()
        db_user = MagicMock()
        state = AsyncMock()

        with (
            patch(
                "app.bot.handlers.reply_nav._deny_unless_owner",
                AsyncMock(return_value=True),
            ),
            patch(
                "app.bot.handlers.reply_nav.get_all_settings",
                AsyncMock(return_value={"nav_mode": "classic"}),
            ),
            patch(
                "app.bot.handlers.reply_nav.nav.show_nav_keyboard",
                new_callable=AsyncMock,
            ) as show_nav,
        ):
            await open_admin_ops_hub(message, session, db_user, state)

        show_nav.assert_awaited()


class OpenPgHomeInlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_inline_pg_hub(self):
        from app.bot.handlers.reply_nav import open_pg_home

        message = AsyncMock()
        message.answer = AsyncMock(return_value=MagicMock())
        session = AsyncMock()
        db_user = MagicMock()
        state = AsyncMock()
        state.get_data = AsyncMock(return_value={})
        state.update_data = AsyncMock()
        feats = frozenset({"pg_overview", "pg_users"})

        with (
            patch(
                "app.bot.auth.bot_may_open_pg_hub",
                AsyncMock(return_value=True),
            ),
            patch(
                "app.bot.auth.bot_migrated_pg_features",
                AsyncMock(return_value=feats),
            ),
            patch(
                "app.bot.auth.bot_pg_can_create_user",
                AsyncMock(return_value=False),
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
            await open_pg_home(
                message, session, db_user, state, is_reseller_bot=False
            )

        message.answer.assert_awaited()
        markup = message.answer.await_args.kwargs.get("reply_markup")
        data = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("adm:pg:stats", data)
        show_nav.assert_not_awaited()


class ShowNavKeyboardAdminInlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_ops_keeps_main_kb(self):
        from app.bot import menu_nav as nav

        message = AsyncMock()
        message.answer = AsyncMock(return_value=MagicMock())
        session = AsyncMock()
        db_user = MagicMock()
        state = AsyncMock()
        state.get_data = AsyncMock(return_value={})
        state.update_data = AsyncMock()
        main_kb = MagicMock(name="MAIN_ADMIN")

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
                "app.bot.menu_nav.kb.admin_ops_reply_keyboard",
                return_value="OPS_CHROME",
            ) as ops_chrome,
        ):
            await nav.show_nav_keyboard(
                message,
                session,
                db_user,
                nav.NAV_ADMIN_OPS,
                text="ops",
                state=state,
            )

        ops_chrome.assert_not_called()
        self.assertIs(
            message.answer.await_args.kwargs.get("reply_markup"),
            main_kb,
        )


class OpenAdminLoyaltyInlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_inline_loyalty_manage_hub(self):
        from app.bot.handlers.loyalty import open_admin_loyalty_hub

        message = AsyncMock()
        message.answer = AsyncMock(return_value=MagicMock())
        session = AsyncMock()
        db_user = MagicMock()
        state = AsyncMock()

        with (
            patch(
                "app.bot.handlers.loyalty.resolve_loyalty_manage_scope",
                AsyncMock(return_value=(None, True)),
            ),
            patch(
                "app.bot.handlers.loyalty.ensure_loyalty_defaults",
                AsyncMock(),
            ),
            patch(
                "app.bot.handlers.loyalty.loyalty_enabled",
                AsyncMock(return_value=True),
            ),
            patch(
                "app.services.users.get_all_settings",
                AsyncMock(return_value={"nav_mode": "inline"}),
            ),
            patch(
                "app.bot.menu_nav.show_nav_keyboard",
                new_callable=AsyncMock,
            ) as show_nav,
        ):
            state.get_data = AsyncMock(return_value={})
            state.update_data = AsyncMock()
            await open_admin_loyalty_hub(message, session, db_user, state)

        message.answer.assert_awaited()
        markup = message.answer.await_args.kwargs.get("reply_markup")
        data = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("nv:adm:loy:overview", data)
        show_nav.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
