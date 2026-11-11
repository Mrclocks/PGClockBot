"""Plans screens use one live panel: the list opens in place instead of a second message."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.bot.nav_inline import admin_plans_audience_keyboard


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


def _session_with_plans(plans: list) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = plans
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    return session


class HubKeyboardTests(unittest.TestCase):
    def test_each_audience_screen_holds_its_actions_and_backs_up_to_the_plans_hub(self) -> None:
        for audience in ("users", "resellers"):
            cbs = _callbacks(admin_plans_audience_keyboard(audience, {}))
            self.assertIn("nv:adm:ra:adm_plans_add", cbs)
            self.assertEqual(cbs[-1], "nv:adm:plans")


class PlansListInPlaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_users_list_edits_panel_and_sends_nothing_else(self) -> None:
        from app.bot.handlers import admin_plans

        message = MagicMock(answer=AsyncMock())
        present = AsyncMock()
        with (
            patch("app.bot.nav_inline.present_nav_panel", new=present),
            patch.object(admin_plans, "get_all_settings", new=AsyncMock(return_value={})),
        ):
            await admin_plans.send_users_plans_overview(
                message, _session_with_plans([]), None
            )
        present.assert_awaited_once()
        message.answer.assert_not_awaited()
        cbs = _callbacks(present.await_args.kwargs["inline"])
        self.assertEqual(cbs[-1], "nv:adm:plans")

    async def test_resellers_list_back_returns_to_hub(self) -> None:
        from app.bot.handlers import admin_plans

        message = MagicMock(answer=AsyncMock())
        present = AsyncMock()
        with (
            patch("app.bot.nav_inline.present_nav_panel", new=present),
            patch.object(admin_plans, "get_all_settings", new=AsyncMock(return_value={})),
            patch.object(admin_plans, "list_reseller_plans", new=AsyncMock(return_value=[])),
        ):
            await admin_plans.send_resellers_plans_overview(message, MagicMock(), None)
        present.assert_awaited_once()
        message.answer.assert_not_awaited()
        cbs = _callbacks(present.await_args.kwargs["inline"])
        self.assertEqual(cbs[-1], "nv:adm:plans")


class AudienceHubTests(unittest.IsolatedAsyncioTestCase):
    async def test_audience_hub_is_a_single_message(self) -> None:
        from app.bot.handlers import reply_nav

        user = SimpleNamespace(id=1)
        present = AsyncMock()
        users_list = AsyncMock()
        with (
            patch.object(reply_nav, "_deny_unless_owner", new=AsyncMock(return_value=True)),
            patch("app.bot.handlers.admin_plans.get_all_settings", new=AsyncMock(return_value={})),
            patch("app.bot.nav_inline.present_nav_panel", new=present),
            patch("app.bot.handlers.admin_plans.send_users_plans_overview", new=users_list),
        ):
            await reply_nav.present_admin_plans_audience(
                MagicMock(), _session_with_plans([]), user, None, audience="users"
            )
        present.assert_awaited_once()
        users_list.assert_not_awaited()

    async def test_non_owner_cannot_open_list_callback(self) -> None:
        from app.bot.handlers import nav_hubs, reply_nav

        callback = MagicMock(
            data="nv:adm:plans:list:users",
            answer=AsyncMock(),
            message=MagicMock(),
        )
        users_list = AsyncMock()
        with (
            patch.object(reply_nav, "_deny_unless_owner", new=AsyncMock(return_value=False)),
            patch("app.bot.handlers.admin_plans.send_users_plans_overview", new=users_list),
        ):
            await nav_hubs.nv_adm_plans_list(
                callback, MagicMock(), SimpleNamespace(id=2), MagicMock(), False
            )
        users_list.assert_not_awaited()

    async def test_invalid_audience_is_rejected(self) -> None:
        from app.bot.handlers import nav_hubs

        callback = MagicMock(
            data="nv:adm:plans:list:bogus", answer=AsyncMock(), message=MagicMock()
        )
        await nav_hubs.nv_adm_plans_list(
            callback, MagicMock(), SimpleNamespace(id=2), MagicMock(), False
        )
        callback.answer.assert_awaited_once()
        self.assertTrue(callback.answer.await_args.kwargs.get("show_alert"))


class ResellerAccessCardTests(unittest.IsolatedAsyncioTestCase):
    async def test_access_card_and_links_are_one_message(self) -> None:
        from app.bot.handlers import reply_nav

        user = SimpleNamespace(id=5, role="reseller")
        profile = SimpleNamespace(
            is_active=True, bot_username="shop_bot", plan=None
        )
        message = MagicMock(answer=AsyncMock())
        present = AsyncMock()
        with (
            patch("app.bot.menu_nav.build_main_reply_keyboard",
                  new=AsyncMock(return_value=(MagicMock(), {}, "user"))),
            patch.object(reply_nav, "get_reseller_profile", new=AsyncMock(return_value=profile)),
            patch.object(
                reply_nav, "format_reseller_access_prompt", new=AsyncMock(return_value="prompt"),
            ),
            patch.object(reply_nav, "get_all_settings", new=AsyncMock(return_value={})),
            patch.object(reply_nav, "present_nav_panel", new=present),
        ):
            await reply_nav.open_reseller_creds(message, MagicMock(), user)
        present.assert_awaited_once()
        message.answer.assert_not_awaited()
        markup = present.await_args.kwargs["inline"]
        # Confirm step first — bot URL appears only after reveal on the card keyboard.
        cbs = [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]
        self.assertEqual(cbs, ["res:creds:reveal", "nv:res:home"])
        self.assertEqual(present.await_args.kwargs["text"], "prompt")

    async def test_non_reseller_gets_refusal_only(self) -> None:
        from app.bot.handlers import reply_nav

        user = SimpleNamespace(id=9, role="user")
        message = MagicMock(answer=AsyncMock())
        with patch("app.bot.menu_nav.build_main_reply_keyboard",
                   new=AsyncMock(return_value=(MagicMock(), {}, "user"))):
            await reply_nav.open_reseller_creds(message, MagicMock(), user)
        message.answer.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
