"""Approving and rejecting service cancellations from the bot: scope, permissions, amount and back steps."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.handlers import cancellation_review as review
from app.db.models import Base, BotUser, ServiceCancellation, ShopWallet, UserService
from app.services import bot_feedback_texts as feedback
from app.services.service_cancellations import request_cancellation
from app.services.users import _SETTINGS_CACHE, reset_shop_reseller_id, set_shop_reseller_id

CTX = {"is_reseller_bot": False, "reseller_owner_id": None}


class FakePanel:
    def __init__(self):
        self.status = "active"
        self.writes = 0
        self.lose_response = False

    async def get_user_by_id(self, user_id):
        return {"id": user_id, "status": self.status}

    async def set_disabled_by_id(self, user_id, disabled):
        self.writes += 1
        self.status = "disabled"
        if self.lose_response:
            raise TimeoutError("lost response")
        return {"id": user_id, "status": self.status}


def buttons(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


class ReviewCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.tmp.name) / 'review.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.token = set_shop_reseller_id(None)
        _SETTINGS_CACHE.clear()
        now = datetime.now(timezone.utc)
        self.session.add_all([
            BotUser(id=2, telegram_id=1002, referral_code="shop2", role="reseller"),
            BotUser(id=3, telegram_id=1003, referral_code="shop3", role="reseller"),
        ])
        await self.session.flush()
        self.platform_customer = BotUser(id=1, telegram_id=1001, referral_code="c1", wallet_balance=0, created_at=now - timedelta(days=90))
        self.shop_customer = BotUser(id=4, telegram_id=1004, referral_code="c4", reseller_id=2, created_at=now - timedelta(days=90))
        self.session.add_all([self.platform_customer, self.shop_customer])
        await self.session.flush()
        self.session.add_all([
            UserService(id=11, bot_user_id=1, pg_user_id=101, pg_username="platform-svc", remark="order:1"),
            UserService(id=12, bot_user_id=4, pg_user_id=102, pg_username="shop-svc", remark="order:2", shop_id=2),
        ])
        await self.session.commit()
        self.panel = FakePanel()
        for patcher in (
            patch("app.services.service_cancellations.get_pg", return_value=self.panel),
            patch("app.services.service_cancellations.get_pg_for_reseller", AsyncMock(return_value=self.panel)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.platform_request = (await request_cancellation(self.session, self.platform_customer, 11, shop_id=None, reason="دیگر نیاز ندارم"))["id"]
        self.shop_request = (await request_cancellation(self.session, self.shop_customer, 12, shop_id=2, reason="<b>x</b>"))["id"]

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()
        reset_shop_reseller_id(self.token)
        _SETTINGS_CACHE.clear()
        self.tmp.cleanup()

    def callback(self, data):
        callback = AsyncMock()
        callback.data = data
        callback.message = AsyncMock()
        return callback

    async def press(self, data, *, perms=("orders", "payments"), shop_id=2, owner=False, state=None):
        """Run the handler for ``data`` as a shop operator holding ``perms`` (or the platform Owner)."""
        handler = {
            "a": review.approve_start, "r": review.reject_ask, "ok": review.approve_run,
            "rn": review.reject_run, "v": review.view_card,
        }[data.split(":")[1]]
        callback = self.callback(data)
        user = SimpleNamespace(id=9, telegram_id=777)
        actor = (None, None) if owner else (shop_id, object())
        kwargs = {"state": state or AsyncMock()} if handler in (review.approve_start, review.view_card) else {}
        with patch.object(review, "load_reseller_actor", AsyncMock(return_value=actor)), \
                patch.object(review, "has_bot_perm", MagicMock(side_effect=lambda profile, key, **kw: key in perms)), \
                patch.object(review, "require_bot_owner", AsyncMock(return_value=owner)), \
                patch.object(review, "safe_edit_text", AsyncMock()) as edit, \
                patch.object(review, "ask_text", AsyncMock()) as ask:
            await handler(callback, session=self.session, db_user=user, **kwargs, **CTX)
        return callback, edit, ask

    def shown(self, edit):
        return edit.await_args.args[1], buttons(edit.await_args.kwargs["reply_markup"])

    async def status(self, request_id):
        row = await self.session.get(ServiceCancellation, request_id)
        await self.session.refresh(row)
        return row.status


class CancellationReviewBotTests(ReviewCase):
    async def test_card_shows_decisions_only_for_the_permissions_held(self):
        _, edit, _ = await self.press(f"cnrv:v:{self.shop_request}")
        text, found = self.shown(edit)
        self.assertEqual(found, [f"cnrv:a:{self.shop_request}", f"cnrv:r:{self.shop_request}", review.SHOP_LIST])
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", text)
        self.assertNotIn("<b>x</b>", text)
        _, edit, _ = await self.press(f"cnrv:v:{self.shop_request}", perms=("orders",))
        self.assertEqual(self.shown(edit)[1], [f"cnrv:r:{self.shop_request}", review.SHOP_LIST])

    async def test_without_orders_permission_nothing_is_shown_or_changed(self):
        for data in (f"cnrv:v:{self.shop_request}", f"cnrv:a:{self.shop_request}", f"cnrv:r:{self.shop_request}",
                     f"cnrv:ok:{self.shop_request}:100", f"cnrv:rn:{self.shop_request}:1"):
            callback, edit, _ = await self.press(data, perms=())
            callback.answer.assert_awaited_once_with(feedback.NO_ACCESS, show_alert=True)
            edit.assert_not_awaited()
        self.assertEqual(await self.status(self.shop_request), "pending")

    async def test_approving_needs_the_payments_permission_but_rejecting_does_not(self):
        callback, _, _ = await self.press(f"cnrv:ok:{self.shop_request}:100", perms=("orders",))
        callback.answer.assert_awaited_once_with(feedback.NO_ACCESS, show_alert=True)
        self.assertEqual(self.panel.writes, 0)
        await self.press(f"cnrv:rn:{self.shop_request}:1", perms=("orders",))
        self.assertEqual(await self.status(self.shop_request), "rejected")

    async def test_other_shop_and_platform_cannot_see_or_decide_the_request(self):
        for shop_id, owner in ((3, False), (None, True)):
            for data in (f"cnrv:v:{self.shop_request}", f"cnrv:a:{self.shop_request}", f"cnrv:r:{self.shop_request}",
                         f"cnrv:ok:{self.shop_request}:100", f"cnrv:rn:{self.shop_request}:1"):
                callback, edit, _ = await self.press(data, shop_id=shop_id, owner=owner)
                callback.answer.assert_any_await(feedback.NOT_FOUND, show_alert=True) if data.split(":")[1] in {"v", "a", "r"} else None
        self.assertEqual(await self.status(self.shop_request), "pending")
        self.assertEqual(self.panel.writes, 0)
        callback, _, _ = await self.press(f"cnrv:ok:{self.platform_request}:100", shop_id=2)
        self.assertEqual(await self.status(self.platform_request), "pending")

    async def test_pending_approval_asks_for_the_amount_then_confirms_and_credits_the_shop_wallet(self):
        state = AsyncMock()
        _, _, ask = await self.press(f"cnrv:a:{self.shop_request}", state=state)
        self.assertEqual(ask.await_args.kwargs["return_to"], f"cnrv:v:{self.shop_request}")
        self.assertEqual(ask.await_args.kwargs["cancel_code"], review.CANCEL_CODE)
        callback, edit, _ = await self.press(f"cnrv:ok:{self.shop_request}:250")
        self.assertEqual(await self.status(self.shop_request), "approved")
        self.assertEqual(self.panel.writes, 1)
        balance = await self.session.scalar(select(ShopWallet.balance).where(ShopWallet.user_id == 4, ShopWallet.reseller_id == 2))
        self.assertEqual(balance, 250)
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(ShopWallet).where(ShopWallet.reseller_id.is_(None))), 0)
        callback.answer.assert_awaited_with("لغو تأیید شد", show_alert=True)

    async def test_repeating_the_confirmation_does_not_credit_twice(self):
        await self.press(f"cnrv:ok:{self.shop_request}:250")
        await self.press(f"cnrv:ok:{self.shop_request}:250")
        balance = await self.session.scalar(select(ShopWallet.balance).where(ShopWallet.user_id == 4, ShopWallet.reseller_id == 2))
        self.assertEqual(balance, 250)
        self.assertEqual(self.panel.writes, 1)

    async def test_review_state_reuses_the_saved_amount_and_a_different_amount_is_refused(self):
        self.panel.lose_response = True
        callback, edit, _ = await self.press(f"cnrv:ok:{self.shop_request}:250")
        self.assertTrue(callback.answer.await_args_list[0].kwargs["show_alert"])
        self.assertEqual(await self.status(self.shop_request), "review")
        self.assertIn(f"cnrv:a:{self.shop_request}", self.shown(edit)[1])
        self.panel.lose_response = False
        _, edit, ask = await self.press(f"cnrv:a:{self.shop_request}")
        ask.assert_not_awaited()
        self.assertIn(f"cnrv:ok:{self.shop_request}:250", self.shown(edit)[1])
        callback, _, _ = await self.press(f"cnrv:ok:{self.shop_request}:999")
        self.assertEqual(await self.status(self.shop_request), "review")
        await self.press(f"cnrv:ok:{self.shop_request}:250")
        self.assertEqual(await self.status(self.shop_request), "approved")

    async def test_reject_offers_fixed_reasons_with_back_and_saves_the_chosen_one(self):
        _, edit, _ = await self.press(f"cnrv:r:{self.shop_request}")
        self.assertEqual(self.shown(edit)[1], [f"cnrv:rn:{self.shop_request}:{key}" for key in review.REJECT_NOTES] + [f"cnrv:v:{self.shop_request}"])
        await self.press(f"cnrv:rn:{self.shop_request}:2")
        row = await self.session.get(ServiceCancellation, self.shop_request)
        await self.session.refresh(row)
        self.assertEqual((row.status, row.operator_note), ("rejected", review.REJECT_NOTES["2"]))
        self.assertEqual(self.panel.writes, 0)

    async def test_platform_owner_decides_platform_requests_only(self):
        _, edit, _ = await self.press(f"cnrv:v:{self.platform_request}", owner=True)
        self.assertEqual(self.shown(edit)[1][-1], review.PLATFORM_LIST)
        await self.press(f"cnrv:ok:{self.platform_request}:50", owner=True)
        self.assertEqual(await self.status(self.platform_request), "approved")
        await self.session.refresh(self.platform_customer)
        self.assertEqual(self.platform_customer.wallet_balance, 50)

    async def test_unknown_reject_reason_and_oversized_amount_are_refused(self):
        callback, _, _ = await self.press(f"cnrv:rn:{self.shop_request}:9")
        callback.answer.assert_awaited_with(feedback.INVALID, show_alert=True)
        callback, _, _ = await self.press(f"cnrv:ok:{self.shop_request}:99999999999"[:-1])
        self.assertEqual(self.panel.writes, 0)


class AmountStepTests(ReviewCase):
    async def send_amount(self, text, *, allowed=True, shop_id=2, state_shop=2):
        message = AsyncMock()
        message.text = text
        message.chat.id = 5
        state = AsyncMock()
        state.get_data = AsyncMock(return_value={"cnrv_id": self.shop_request, "cnrv_shop": state_shop})
        user = SimpleNamespace(id=9, telegram_id=777)
        with patch.object(review, "review_scope", AsyncMock(return_value=(allowed, shop_id))), \
                patch.object(review, "finish_text_step", AsyncMock()) as finish:
            await review.approve_amount(message, state, self.session, user, **CTX)
        return message, state, finish

    async def test_valid_amount_moves_to_the_confirm_screen_with_back(self):
        _, state, finish = await self.send_amount("۲۵۰")
        markup = finish.await_args.kwargs["inline"]
        self.assertEqual(buttons(markup), [f"cnrv:ok:{self.shop_request}:250", f"cnrv:v:{self.shop_request}"])
        state.set_state.assert_awaited_with(None)

    async def test_invalid_amounts_are_refused_and_the_step_stays_open(self):
        for text in ("-5", "12.5", "abc", "", "99999999999"):
            message, state, finish = await self.send_amount(text)
            finish.assert_not_awaited()
            message.answer.assert_awaited()
            state.set_state.assert_not_awaited()

    async def test_lost_permission_or_changed_shop_ends_the_step(self):
        for kwargs in ({"allowed": False}, {"state_shop": 3}):
            message, state, finish = await self.send_amount("100", **kwargs)
            finish.assert_not_awaited()
            state.clear.assert_awaited()
            message.answer.assert_awaited_with(feedback.NO_ACCESS_REPLY)


class ReviewScopeTests(unittest.IsolatedAsyncioTestCase):
    async def scope(self, *, actor, perms=(), owner=False, shop_bot=False, need=("orders", "payments")):
        with patch.object(review, "load_reseller_actor", AsyncMock(return_value=actor)), \
                patch.object(review, "has_bot_perm", MagicMock(side_effect=lambda profile, key, **kw: key in perms)), \
                patch.object(review, "require_bot_owner", AsyncMock(return_value=owner)):
            return await review.review_scope(MagicMock(), MagicMock(), is_reseller_bot=shop_bot, reseller_owner_id=None, permissions=need)

    async def test_shop_operator_needs_every_permission(self):
        self.assertEqual(await self.scope(actor=(2, object()), perms=("orders", "payments")), (True, 2))
        self.assertEqual(await self.scope(actor=(2, object()), perms=("orders",)), (False, 2))

    async def test_platform_needs_the_owner_and_shop_bots_never_get_platform_scope(self):
        self.assertEqual(await self.scope(actor=(None, None), owner=True), (True, None))
        self.assertEqual(await self.scope(actor=(None, None), owner=False), (False, None))
        self.assertEqual(await self.scope(actor=(None, None), owner=True, shop_bot=True), (False, None))


if __name__ == "__main__":
    unittest.main()
