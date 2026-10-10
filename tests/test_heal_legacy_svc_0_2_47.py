"""heal_main_reply + empty-FSM legacy service actions reopen services."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import app.bot.keyboards  # noqa: F401


class HealMainReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_sends_real_text_with_main_keyboard(self):
        from app.bot.nav_chrome import heal_main_reply

        message = AsyncMock()
        message.answer = AsyncMock()
        session = AsyncMock()
        db_user = MagicMock()
        main_kb = MagicMock(name="MAIN")

        with patch(
            "app.bot.menu_nav.build_main_reply_keyboard",
            new=AsyncMock(return_value=(main_kb, {"x": 1}, "user")),
        ) as build:
            ui = await heal_main_reply(
                message,
                session,
                db_user,
                text="از لیست بالا یک سرویس را انتخاب کنید.",
                is_reseller_bot=True,
                reseller_owner_id=9,
            )

        build.assert_awaited()
        self.assertTrue(build.await_args.kwargs.get("is_reseller_bot"))
        self.assertEqual(build.await_args.kwargs.get("reseller_owner_id"), 9)
        message.answer.assert_awaited_once()
        self.assertEqual(
            message.answer.await_args.args[0],
            "از لیست بالا یک سرویس را انتخاب کنید.",
        )
        self.assertIs(message.answer.await_args.kwargs.get("reply_markup"), main_kb)
        self.assertEqual(ui, {"x": 1})

    async def test_filler_text_replaced(self):
        from app.bot.nav_chrome import heal_main_reply

        message = AsyncMock()
        message.answer = AsyncMock()
        session = AsyncMock()
        db_user = MagicMock()

        with patch(
            "app.bot.menu_nav.build_main_reply_keyboard",
            new=AsyncMock(return_value=(MagicMock(), {}, "user")),
        ):
            await heal_main_reply(
                message,
                session,
                db_user,
                text="⌨️",
                is_reseller_bot=False,
                reseller_owner_id=None,
            )

        sent = message.answer.await_args.args[0]
        self.assertNotEqual(sent, "⌨️")
        self.assertTrue(len(sent) > 2)


class EmptyFsmServiceActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_service_id_opens_services_with_heal(self):
        from app.bot import keyboards as kb
        from app.bot.handlers.reply_nav import reply_main_nav

        message = AsyncMock()
        message.text = "🔗 لینک و QR"
        message.answer = AsyncMock()
        session = AsyncMock()
        db_user = MagicMock()
        db_user.id = 1
        state = AsyncMock()
        state.get_data = AsyncMock(return_value={})  # empty FSM — no SERVICE_ID
        state.get_state = AsyncMock(return_value=None)
        state.set_state = AsyncMock()
        state.set_data = AsyncMock()
        state.clear = AsyncMock()

        opened = AsyncMock()

        with (
            patch(
                "app.bot.handlers.reply_nav.open_services_list",
                new=opened,
            ),
            patch(
                "app.bot.handlers.start.render_home",
                new=AsyncMock(),
            ),
        ):
            await reply_main_nav(
                message,
                session,
                db_user,
                state,
                reply_action=kb.REPLY_ACTION_SVC_LINK,
                reply_ui={},
                reply_role="user",
                is_reseller_bot=False,
                reseller_owner_id=None,
            )

        opened.assert_awaited_once()
        kwargs = opened.await_args.kwargs
        self.assertTrue(kwargs.get("heal_reply"))
        message.answer.assert_not_awaited()  # no dead-end text


if __name__ == "__main__":
    unittest.main()
