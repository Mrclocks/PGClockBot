"""Reseller hub taps edit the tapped panel; sub-screens go back one level."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.bot.handlers import reply_nav, reseller
from app.bot.nav_inline import reseller_access_card_keyboard


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


def _panel_message() -> MagicMock:
    return MagicMock(
        from_user=SimpleNamespace(is_bot=True),
        bot=MagicMock(),
        answer=AsyncMock(),
        edit_text=AsyncMock(),
    )


def _user_message() -> MagicMock:
    bubble = _panel_message()
    return MagicMock(
        from_user=SimpleNamespace(is_bot=False),
        bot=MagicMock(),
        answer=AsyncMock(return_value=bubble),
    )


async def _tap_capacity(message: MagicMock) -> AsyncMock:
    handler = AsyncMock()
    user = SimpleNamespace(id=5, role="reseller", telegram_id=1)
    with (
        patch("app.services.reseller_access.load_reseller_capacity_actor",
              new=AsyncMock(return_value=(5, MagicMock()))),
        patch("app.bot.handlers.reseller.res_addons", new=handler),
    ):
        await reply_nav._soft_reseller(
            message, MagicMock(), user, "res_addon_packs", None,
            is_reseller_bot=False, reseller_owner_id=None,
        )
    return handler


class HubTapTests(unittest.IsolatedAsyncioTestCase):
    async def test_inline_tap_reuses_the_hub_message(self) -> None:
        message = _panel_message()
        handler = await _tap_capacity(message)
        message.answer.assert_not_awaited()
        handler.assert_awaited_once()
        self.assertIs(handler.await_args.args[0].message, message)

    async def test_reply_keyboard_tap_still_sends_one_bubble(self) -> None:
        message = _user_message()
        handler = await _tap_capacity(message)
        message.answer.assert_awaited_once()
        self.assertIs(handler.await_args.args[0].message, message.answer.return_value)

    async def test_denial_keeps_a_back_button_on_the_panel(self) -> None:
        message = _panel_message()
        cb = reply_nav._SoftCallback(message, "res:addons", back_callback="nv:res:home")
        await cb.answer("فقط نمایندگان", show_alert=True)
        markup = message.edit_text.await_args.kwargs["reply_markup"]
        self.assertEqual(_callbacks(markup), ["nv:res:home"])


class SubScreenBackTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, handler, plan: MagicMock, **patches):
        callback = MagicMock(answer=AsyncMock(), message=MagicMock())
        edit = AsyncMock()
        if getattr(plan, "id", None) is None or not isinstance(getattr(plan, "id", None), int):
            plan.id = 1
        with (
            patch.object(reseller, "_capacity_context",
                         new=AsyncMock(return_value=(5, MagicMock(pg_admin_username="a"), plan, MagicMock()))),
            patch.object(reseller, "safe_edit_text", new=edit),
            patch("app.services.reseller_capacity.plan_allows_buy_extra", return_value=True),
            patch("app.services.reseller_capacity.plan_extra_gb_price", return_value=1000),
            patch("app.services.reseller_capacity.plan_extra_user_price", return_value=500),
            patch("app.services.reseller_capacity.plan_renew_price", return_value=1000),
            patch("app.services.pg_admin_subscription.is_subscription_plan", return_value=True),
            patch("app.services.pg_admin_subscription.get_or_create_subscription",
                  new=AsyncMock(return_value=SimpleNamespace(access_status="active"))),
            patch("app.services.pg_admin_subscription.get_subscription",
                  new=AsyncMock(return_value=None)),
            patch("app.services.pg_admin_subscription.compute_renew_invoice",
                  return_value={"total": 1000}),
            patch("app.services.pg_admin_subscription.format_renew_invoice_html",
                  return_value="invoice"),
            patch("app.services.pg_admin_subscription.list_addon_plans",
                  new=AsyncMock(return_value=[])),
            patch(
                "app.services.reseller_plan_extra_packs.buyable_packs_for_plan",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "app.services.reseller_plan_extra_packs.active_amounts_for_plan",
                new=AsyncMock(return_value=frozenset({1, 5, 10, 50, 20})),
            ),
        ):
            await handler(callback, MagicMock(), SimpleNamespace(id=5))
        return _callbacks(edit.await_args.kwargs["reply_markup"])

    async def test_renew_back_returns_to_its_group(self) -> None:
        cbs = await self._run(reseller.res_renew, MagicMock(category_id=None))
        self.assertEqual(cbs[-1], "nv:res:grp:account")
        self.assertNotIn("menu:home", cbs)

    async def test_addons_back_returns_to_its_group(self) -> None:
        cbs = await self._run(reseller.res_addons, MagicMock(category_id=None))
        self.assertEqual(cbs[-1], "nv:res:grp:account")
        self.assertNotIn("menu:home", cbs)

    async def test_unit_purchase_back_returns_to_packs(self) -> None:
        for handler in (reseller.res_buy_gb, reseller.res_buy_users):
            cbs = await self._run(handler, MagicMock(category_id=None))
            self.assertEqual(cbs[-1], "res:addons")


class AccessCardTests(unittest.IsolatedAsyncioTestCase):
    def test_card_keyboard_has_back_to_hub(self) -> None:
        profile = SimpleNamespace(bot_username="shop_bot", plan=None)
        cbs = _callbacks(reseller_access_card_keyboard(profile, {}))
        self.assertEqual(cbs, ["nv:res:home"])

    def test_card_keyboard_without_bot_still_has_back(self) -> None:
        profile = SimpleNamespace(bot_username=None, plan=None)
        self.assertEqual(
            _callbacks(reseller_access_card_keyboard(profile, {})), ["nv:res:home"]
        )

    async def test_open_creds_edits_panel_with_back(self) -> None:
        user = SimpleNamespace(id=5, role="reseller")
        profile = SimpleNamespace(is_active=True, bot_username="b", plan=None)
        present = AsyncMock()
        with (
            patch("app.bot.menu_nav.build_main_reply_keyboard",
                  new=AsyncMock(return_value=(MagicMock(), {}, "user"))),
            patch("app.services.resellers.get_reseller_profile", new=AsyncMock(return_value=profile)),
            patch("app.services.resellers.format_reseller_access_card", new=AsyncMock(return_value="card")),
            patch.object(reply_nav, "get_all_settings", new=AsyncMock(return_value={})),
            patch("app.bot.nav_inline.present_nav_panel", new=present),
        ):
            await reply_nav.open_reseller_creds(MagicMock(), MagicMock(), user)
        present.assert_awaited_once()
        self.assertIn("nv:res:home", _callbacks(present.await_args.kwargs["inline"]))


if __name__ == "__main__":
    unittest.main()
