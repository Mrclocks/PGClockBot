"""Bot capacity purchases: the list screens only ask for confirmation; the pay button buys once."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.bot.handlers import reseller, reseller_capacity_buy
from app.bot.handlers.reseller_capacity_buy import GB_OK, PACK_OK, USERS_OK, confirm_view


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _plan() -> SimpleNamespace:
    return SimpleNamespace(id=1, category_id=None, plan_kind="subscription", billing_mode="fixed",
                           allow_buy_extra=True, extra_gb_price=1000, extra_user_price=500)


class ConfirmScreenTests(unittest.IsolatedAsyncioTestCase):
    async def _open(self, handler, data: str):
        callback = MagicMock(data=data, answer=AsyncMock(), message=MagicMock())
        edit = AsyncMock()
        owner = SimpleNamespace(id=5, wallet_balance=12_345)
        profile = SimpleNamespace(pg_admin_username="shop5")
        buy = AsyncMock()
        with (
            patch.object(reseller, "_capacity_context", new=AsyncMock(return_value=(5, profile, _plan(), owner))),
            patch.object(reseller, "safe_edit_text", new=edit),
            patch("app.bot.handlers.reseller_capacity_buy.current_generation", new=AsyncMock(return_value=7)),
            patch("app.services.reseller_capacity.buy_extra_gb", new=buy),
            patch("app.services.reseller_capacity.buy_extra_users", new=buy),
            patch("app.services.reseller_packs.buy_addon_pack", new=buy),
            patch("app.services.reseller_capacity.plan_allows_buy_extra", return_value=True),
        ):
            await handler(callback, MagicMock(), SimpleNamespace(id=5))
        return edit, buy

    async def test_unit_volume_asks_before_charging(self) -> None:
        edit, buy = await self._open(reseller.res_buy_gb_go, "res:buy_gb:5")
        buy.assert_not_awaited()
        pay = [c for c in _callbacks(edit.await_args.kwargs["reply_markup"]) if c.startswith(GB_OK)]
        self.assertEqual(pay, [f"{GB_OK}5:7"], "the pay button carries the subscription generation")
        self.assertIn("12", edit.await_args.args[1].replace(",", "").replace("٬", ""), "the wallet balance is shown")

    async def test_unit_users_asks_before_charging(self) -> None:
        edit, buy = await self._open(reseller.res_buy_users_go, "res:buy_users:10")
        buy.assert_not_awaited()
        self.assertIn(f"{USERS_OK}10:7", _callbacks(edit.await_args.kwargs["reply_markup"]))

    async def test_forged_amounts_are_refused_without_a_screen(self) -> None:
        edit, buy = await self._open(reseller.res_buy_gb_go, "res:buy_gb:999")
        edit.assert_not_awaited()
        buy.assert_not_awaited()

    def test_callback_data_stays_within_the_telegram_limit(self) -> None:
        for prefix in (PACK_OK, GB_OK, USERS_OK):
            self.assertLessEqual(len(f"{prefix}{2**31 - 1}:{2**31 - 1}".encode()), 64)

    def test_confirm_screen_has_a_pay_and_a_back_button(self) -> None:
        _text, markup = confirm_view(title="t", lines=["x"], price=1000, wallet=5000, ok_callback="ok:1", back_callback="back")
        self.assertEqual(_callbacks(markup), ["ok:1", "back"])


class PayHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def _pay(self, handler, data: str, result: dict | Exception, target: str):
        callback = MagicMock(data=data, answer=AsyncMock(), message=MagicMock())
        owner, profile = SimpleNamespace(id=5), SimpleNamespace(pg_admin_username="shop5")
        buy = AsyncMock(side_effect=result if isinstance(result, Exception) else None,
                        return_value=None if isinstance(result, Exception) else result)
        with (
            patch.object(reseller_capacity_buy, "_actor", new=AsyncMock(return_value=(owner, profile, _plan()))),
            patch.object(reseller_capacity_buy, "safe_edit_text", new=AsyncMock()),
            patch.object(reseller_capacity_buy, target, new=buy),
        ):
            await handler(callback, MagicMock(), SimpleNamespace(id=5))
        return callback, buy

    async def test_pack_pay_passes_the_generation_from_the_button(self) -> None:
        result = {"pack": "p", "gb": 200, "users": 0, "amount": 1, "new_limit": 1}
        _callback, buy = await self._pay(reseller_capacity_buy.res_addon_pay, f"{PACK_OK}9:4", result, "buy_addon_pack")
        self.assertEqual((buy.await_args.kwargs["pack_id"], buy.await_args.kwargs["generation"]), (9, 4))

    async def test_a_stale_button_shows_the_refusal_and_nothing_else(self) -> None:
        callback, _buy = await self._pay(
            reseller_capacity_buy.res_addon_pay, f"{PACK_OK}9:4", ValueError("صفحه قدیمی"), "buy_addon_pack",
        )
        self.assertTrue(callback.answer.await_args.kwargs["show_alert"])

    async def test_malformed_and_forged_buttons_never_reach_the_service(self) -> None:
        for data in (f"{PACK_OK}x:1", f"{PACK_OK}9", f"{PACK_OK}0:1", f"{PACK_OK}9:1:2"):
            _callback, buy = await self._pay(reseller_capacity_buy.res_addon_pay, data, {}, "buy_addon_pack")
            buy.assert_not_awaited()
        _callback, buy = await self._pay(reseller_capacity_buy.res_buy_gb_pay, f"{GB_OK}7:1", {}, "buy_extra_gb")
        buy.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
