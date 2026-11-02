"""Bot flow that voids an approved payment: scope, permission, navigation and the full path."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import func, select

from app.bot.handlers import payment_voids as bot
from app.db.models import BotUser, Order, Payment, PaymentVoid, UserService
from app.services import bot_feedback_texts as feedback
from tests.test_payment_voids import VoidCase

CTX = {"is_reseller_bot": False, "reseller_owner_id": None}


def callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


class BotVoidCase(VoidCase):
    def callback(self, data: str):
        callback = AsyncMock()
        callback.data = data
        callback.message = AsyncMock()
        return callback

    async def press(self, data: str, scope=(True, 2)):
        """Run the handler for ``data`` as the given (allowed, shop_id); returns (callback, edit mock)."""
        handler = {
            "list": bot.void_list, "v": bot.void_card, "ask": bot.void_ask_reason,
            "r": bot.void_confirm, "go": bot.void_go, "retry": bot.void_retry,
        }[data.split(":")[1]]
        callback = self.callback(data)
        user = SimpleNamespace(id=9, telegram_id=777)
        with patch.object(bot, "void_scope", AsyncMock(return_value=scope)), \
                patch.object(bot, "safe_edit_text", AsyncMock()) as edit, \
                patch("app.services.payment_voids.notify_payer", AsyncMock(return_value=True)) as notify:
            await handler(callback, self.session, user, **CTX)
        self.notify = notify
        return callback, edit

    def shown(self, edit) -> tuple[str, list[str]]:
        text = edit.await_args.args[1]
        return text, callbacks(edit.await_args.kwargs["reply_markup"])


class VoidScopeTests(unittest.IsolatedAsyncioTestCase):
    async def scope(self, *, actor, perm=False, owner=False, shop_bot=False):
        with patch.object(bot, "load_reseller_actor", AsyncMock(return_value=actor)), \
                patch.object(bot, "has_bot_perm", MagicMock(return_value=perm)), \
                patch.object(bot, "require_bot_owner", AsyncMock(return_value=owner)):
            return await bot.void_scope(MagicMock(), MagicMock(), is_reseller_bot=shop_bot, reseller_owner_id=None)

    async def test_shop_operator_needs_the_permission(self):
        self.assertEqual(await self.scope(actor=(2, object()), perm=True), (True, 2))
        self.assertEqual(await self.scope(actor=(2, object()), perm=False), (False, 2))

    async def test_platform_needs_the_explicit_owner_and_a_shop_bot_never_gets_platform_scope(self):
        self.assertEqual(await self.scope(actor=(None, None), owner=True), (True, None))
        self.assertEqual(await self.scope(actor=(None, None), owner=False), (False, None))
        self.assertEqual(await self.scope(actor=(None, None), owner=True, shop_bot=True), (False, None))


class PaymentVoidBotTests(BotVoidCase):
    async def test_denied_actor_sees_nothing_and_changes_nothing(self):
        for data in ("payvoid:list", "payvoid:v:101", "payvoid:ask:101", "payvoid:r:101:1", "payvoid:go:101:1:1"):
            callback, edit = await self.press(data, scope=(False, 2))
            callback.answer.assert_awaited_once_with(feedback.NO_ACCESS, show_alert=True)
            edit.assert_not_awaited()
        self.assertEqual((await self.session.get(Payment, 101)).status, "approved")
        self.assertEqual(self.panel.writes, 0)

    async def test_other_shop_cannot_open_or_void_the_payment(self):
        for data in ("payvoid:v:101", "payvoid:ask:101", "payvoid:r:101:1", "payvoid:go:101:1:1"):
            callback, edit = await self.press(data, scope=(True, 3))
            callback.answer.assert_awaited_with(feedback.NOT_FOUND, show_alert=True)
        _, edit = await self.press("payvoid:list", scope=(True, 3))
        self.assertNotIn("payvoid:v:101", self.shown(edit)[1])
        self.assertEqual((await self.session.get(Payment, 101)).status, "approved")
        self.assertEqual(self.panel.writes, 0)

    async def test_platform_scope_cannot_touch_a_shop_payment(self):
        callback, _ = await self.press("payvoid:go:101:1:1", scope=(True, None))
        callback.answer.assert_awaited_with(feedback.NOT_FOUND, show_alert=True)
        self.assertEqual((await self.session.get(Payment, 101)).status, "approved")
        self.assertEqual(self.panel.writes, 0)

    async def test_list_offers_only_voidable_payments_of_the_shop_with_back_to_orders(self):
        _, edit = await self.press("payvoid:list")
        _, buttons = self.shown(edit)
        self.assertIn("payvoid:v:101", buttons)
        self.assertIn("payvoid:v:102", buttons)
        self.assertNotIn("payvoid:v:100", buttons)  # platform payment
        self.assertNotIn("payvoid:v:103", buttons)  # renewal
        self.assertNotIn("payvoid:v:104", buttons)  # gateway
        self.assertNotIn("payvoid:v:105", buttons)  # pending
        self.assertEqual(buttons[-1], bot.SHOP_BACK)
        _, platform = await self.press("payvoid:list", scope=(True, None))
        self.assertEqual(self.shown(platform)[1][-1], bot.PLATFORM_BACK)

    async def test_navigation_walks_forward_and_each_screen_goes_back_one_step(self):
        _, edit = await self.press("payvoid:v:101")
        self.assertEqual(self.shown(edit)[1], ["payvoid:ask:101", "payvoid:list"])
        _, edit = await self.press("payvoid:ask:101")
        buttons = self.shown(edit)[1]
        self.assertEqual(buttons[:-1], [f"payvoid:r:101:{key}" for key in bot.BOT_REASONS])
        self.assertEqual(buttons[-1], "payvoid:v:101")
        _, edit = await self.press("payvoid:r:101:2")
        self.assertEqual(self.shown(edit)[1], ["payvoid:go:101:2:0", "payvoid:go:101:2:1", "payvoid:ask:101"])

    async def test_confirm_runs_the_void_notifies_once_and_a_repeat_changes_nothing(self):
        callback, edit = await self.press("payvoid:go:101:1:1")
        callback.answer.assert_awaited_with("پرداخت ابطال شد", show_alert=True)
        self.assertEqual(self.shown(edit)[1], ["payvoid:list"])
        self.assertEqual((await self.session.get(Payment, 101)).status, "voided")
        self.assertEqual(await self.shop_balance(), 200)
        self.notify.assert_awaited_once()
        self.assertEqual(self.notify.await_args.kwargs["shop_id"], 2)
        reason = await self.session.scalar(select(PaymentVoid.reason))
        self.assertEqual(reason, bot.BOT_REASONS["1"])
        await self.press("payvoid:go:101:1:1")
        self.notify.assert_not_awaited()
        self.assertEqual(await self.shop_balance(), 200)
        self.assertEqual(self.panel.writes, 1)

    async def test_unknown_panel_outcome_shows_the_card_with_a_retry_that_finishes_it(self):
        self.panel.lose_response = True
        callback, edit = await self.press("payvoid:go:101:3:1")
        self.assertTrue(callback.answer.await_args.kwargs["show_alert"])
        self.assertEqual(self.shown(edit)[1], ["payvoid:retry:101", "payvoid:list"])
        self.assertEqual(await self.shop_balance(), 0)
        _, edit = await self.press("payvoid:list")
        self.assertIn("payvoid:v:101", self.shown(edit)[1])
        self.panel.lose_response = False
        callback, _ = await self.press("payvoid:retry:101")
        callback.answer.assert_awaited_with("پرداخت ابطال شد", show_alert=True)
        self.assertEqual(await self.shop_balance(), 200)
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(PaymentVoid)), 1)

    async def test_unknown_reason_key_is_refused(self):
        callback, _ = await self.press("payvoid:go:101:9:0")
        callback.answer.assert_awaited_with(feedback.INVALID, show_alert=True)
        self.assertEqual((await self.session.get(Payment, 101)).status, "approved")

    async def test_customer_name_is_escaped_on_the_card(self):
        user = await self.session.get(BotUser, 4)
        user.full_name = "<b>x</b>"
        await self.session.commit()
        _, edit = await self.press("payvoid:v:101")
        text = self.shown(edit)[0]
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", text)
        self.assertNotIn("<b>x</b>", text)

    async def test_ask_is_refused_for_a_payment_that_is_already_voided(self):
        await self.press("payvoid:go:101:1:0")
        callback, _ = await self.press("payvoid:ask:101")
        callback.answer.assert_awaited_with(feedback.INVALID, show_alert=True)


class ServiceUntouchedTests(BotVoidCase):
    async def test_denied_go_leaves_service_active(self):
        await self.press("payvoid:go:101:1:1", scope=(False, 2))
        self.assertFalse((await self.session.get(UserService, 21)).is_cancelled)
        self.assertEqual((await self.session.get(Order, 11)).status, "delivered")


if __name__ == "__main__":
    unittest.main()
