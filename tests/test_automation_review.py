"""Queue of automatic purchases parked for review: scope, safe release, web route, bot and template."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.automation_review_pages import register_automation_review_pages
from app.bot.handlers import automation_review as bot
from app.db.models import Base, BotUser, Order, Plan, ResellerProfile, ServiceAutomation, UserService, WalletTransaction
from app.services import bot_feedback_texts as feedback
from app.services.automation_review import (
    STATE_CLEARABLE,
    STATE_WAIT_ORDER,
    STATE_WILL_FINISH,
    clear_review,
    count_review_queue,
    list_review_queue,
    shops_without_bot_token,
)

OWNER = {"role": "admin", "org_principal_id": 1, "org_depth": 0, "org_status": "active"}
SHOP_STAFF = {"role": "reseller", "bot_user_id": 2, "permissions": ["orders"]}


class ReviewCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.tmp.name) / 'auto.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add_all([
            BotUser(id=2, telegram_id=1002, referral_code="shop2", role="reseller"),
            BotUser(id=3, telegram_id=1003, referral_code="shop3", role="reseller"),
        ])
        await self.session.flush()
        self.session.add_all([
            BotUser(id=1, telegram_id=1001, referral_code="c1", full_name="Platform Customer"),
            BotUser(id=4, telegram_id=1004, referral_code="c4", reseller_id=2, full_name="<b>Shop</b> Customer"),
            BotUser(id=5, telegram_id=1005, referral_code="c5", reseller_id=3, full_name="Other Shop Customer"),
            Plan(id=1, name="P", price=100, duration_days=30, data_limit_gb=10),
        ])
        await self.session.flush()
        self.session.add_all([
            UserService(id=10, bot_user_id=1, plan_id=1, pg_user_id=500, pg_username="plat-svc"),
            UserService(id=11, bot_user_id=4, plan_id=1, pg_user_id=501, pg_username="shop-wait", shop_id=2),
            UserService(id=12, bot_user_id=4, plan_id=1, pg_user_id=502, pg_username="shop-done", shop_id=2),
            UserService(id=13, bot_user_id=4, plan_id=1, pg_user_id=503, pg_username="shop-cancelled", shop_id=2),
            UserService(id=14, bot_user_id=4, plan_id=1, pg_user_id=504, pg_username="shop-missing", shop_id=2),
            UserService(id=15, bot_user_id=5, plan_id=1, pg_user_id=505, pg_username="other-shop", shop_id=3),
        ])
        await self.session.flush()
        self.session.add_all([
            Order(id=20, user_id=1, plan_id=1, amount=100, status="cancelled"),
            Order(id=21, user_id=4, plan_id=1, amount=200, status="paid", reseller_id=2, service_id=11),
            Order(id=22, user_id=4, plan_id=1, amount=200, status="delivered", reseller_id=2, service_id=12),
            Order(id=23, user_id=4, plan_id=1, amount=300, status="cancelled", reseller_id=2, service_id=13),
            Order(id=25, user_id=5, plan_id=1, amount=50, status="cancelled", reseller_id=3, service_id=15),
        ])
        await self.session.flush()
        self.session.add_all([
            ServiceAutomation(service_id=10, shop_id=None, renew_enabled=True, needs_review=True, pending_order_id=20, pending_action="renew", renew_notice="review"),
            ServiceAutomation(service_id=11, shop_id=2, renew_enabled=True, needs_review=True, pending_order_id=21, pending_action="renew"),
            ServiceAutomation(service_id=12, shop_id=2, renew_enabled=True, needs_review=True, pending_order_id=22, pending_action="renew"),
            ServiceAutomation(service_id=13, shop_id=2, renew_enabled=True, needs_review=True, pending_order_id=23, pending_action="renew", renew_notice="review"),
            ServiceAutomation(service_id=14, shop_id=2, volume_enabled=True, needs_review=True, pending_action="volume"),
            ServiceAutomation(service_id=15, shop_id=3, renew_enabled=True, needs_review=True, pending_order_id=25, pending_action="renew"),
        ])
        await self.session.commit()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()
        self.tmp.cleanup()

    async def row(self, service_id):
        row = await self.session.get(ServiceAutomation, service_id)
        await self.session.refresh(row)
        return row

    async def debit(self, order_id=23, user_id=4, shop_id=2):
        self.session.add(WalletTransaction(user_id=user_id, amount=-300, balance_after=0, reason=f"خرید سفارش #{order_id}", reseller_id=shop_id))
        await self.session.commit()


class AutomationReviewServiceTests(ReviewCase):
    async def test_queue_is_scoped_to_one_shop_and_classifies_each_item(self):
        items, _ = await list_review_queue(self.session, shop_id=2)
        self.assertEqual({item.service_id: item.state for item in items},
                         {11: STATE_WAIT_ORDER, 12: STATE_WILL_FINISH, 13: STATE_CLEARABLE, 14: STATE_CLEARABLE})
        platform, _ = await list_review_queue(self.session, shop_id=None)
        self.assertEqual([item.service_id for item in platform], [10])
        self.assertEqual(await count_review_queue(self.session, shop_id=3), 1)

    async def test_queue_pages_by_service_id_without_repeating(self):
        first, cursor = await list_review_queue(self.session, shop_id=2, limit=2)
        self.assertEqual([item.service_id for item in first], [11, 12])
        second, cursor = await list_review_queue(self.session, shop_id=2, after_service_id=cursor, limit=2)
        self.assertEqual([item.service_id for item in second], [13, 14])
        self.assertIsNone(cursor)

    async def test_debit_in_the_ledger_is_reported_from_one_query(self):
        await self.debit()
        items, _ = await list_review_queue(self.session, shop_id=2)
        self.assertEqual({item.service_id: item.wallet_debited for item in items}, {11: False, 12: False, 13: True, 14: False})

    async def test_unresolved_or_delivered_orders_cannot_be_released(self):
        for service_id in (11, 12):
            with self.assertRaises(ValueError):
                await clear_review(self.session, service_id, shop_id=2, actor="a", money_settled=True)
            self.assertTrue((await self.row(service_id)).needs_review)

    async def test_release_without_a_debit_clears_the_parked_state_and_resets_the_notice(self):
        await clear_review(self.session, 13, shop_id=2, actor="a", money_settled=False)
        row = await self.row(13)
        self.assertEqual((row.needs_review, row.pending_order_id, row.pending_action, row.renew_notice), (False, None, None, None))
        self.assertTrue(row.renew_enabled)
        self.assertIsNone(row.lock_token)

    async def test_release_with_a_debit_needs_the_money_confirmation(self):
        await self.debit()
        with self.assertRaises(ValueError):
            await clear_review(self.session, 13, shop_id=2, actor="a", money_settled=False)
        self.assertTrue((await self.row(13)).needs_review)
        await clear_review(self.session, 13, shop_id=2, actor="a", money_settled=True)
        self.assertFalse((await self.row(13)).needs_review)

    async def test_missing_order_can_be_released_and_automation_can_be_switched_off(self):
        await clear_review(self.session, 14, shop_id=2, actor="a", money_settled=False, disable_automation=True)
        row = await self.row(14)
        self.assertFalse(row.needs_review)
        self.assertFalse(row.volume_enabled)

    async def test_other_shop_and_platform_cannot_release(self):
        for shop_id in (3, None):
            with self.assertRaises(ValueError) as caught:
                await clear_review(self.session, 13, shop_id=shop_id, actor="a", money_settled=True)
            self.assertEqual(str(caught.exception), "این مورد پیدا نشد")
        self.assertTrue((await self.row(13)).needs_review)

    async def test_second_release_and_live_lease_are_refused(self):
        await clear_review(self.session, 13, shop_id=2, actor="a", money_settled=False)
        with self.assertRaises(ValueError):
            await clear_review(self.session, 13, shop_id=2, actor="a", money_settled=False)
        from datetime import datetime, timedelta, timezone

        row = await self.row(14)
        row.lock_token, row.locked_until = "busy", datetime.now(timezone.utc) + timedelta(minutes=5)
        await self.session.commit()
        with self.assertRaises(ValueError):
            await clear_review(self.session, 14, shop_id=2, actor="a", money_settled=False)
        self.assertTrue((await self.row(14)).needs_review)

    async def test_shops_without_a_bot_token_are_listed_for_the_owner(self):
        self.session.add_all([
            ResellerProfile(user_id=2, is_active=True, bot_token=None),
            ResellerProfile(user_id=3, is_active=True, bot_token="token"),
        ])
        await self.session.commit()
        self.assertEqual(await shops_without_bot_token(self.session), [2])


class AutomationReviewRouteTests(ReviewCase):
    async def post(self, staff, service_id, **form):
        app = FastAPI()

        async def db():
            yield self.session

        async def auth():
            return staff

        register_automation_review_pages(app, require_staff=auth, get_db=db)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.post(f"/service-automation/{service_id}/clear", data=form, follow_redirects=False)

    async def test_missing_permission_or_scope_is_forbidden(self):
        for staff in ({"role": "reseller", "bot_user_id": 2, "permissions": ["payments"]}, {"role": "admin"}, {"role": "pg_staff"}):
            self.assertEqual((await self.post(staff, 13)).status_code, 403)
        self.assertTrue((await self.row(13)).needs_review)

    async def test_shop_releases_only_its_own_services(self):
        denied = await self.post(SHOP_STAFF, 15)
        self.assertIn("error=", denied.headers["location"])
        self.assertTrue((await self.row(15)).needs_review)
        ok = await self.post(SHOP_STAFF, 13)
        self.assertNotIn("error=", ok.headers["location"])
        self.assertFalse((await self.row(13)).needs_review)

    async def test_owner_releases_platform_services_only(self):
        self.assertIn("error=", (await self.post(OWNER, 13)).headers["location"])
        self.assertTrue((await self.row(13)).needs_review)
        self.assertNotIn("error=", (await self.post(OWNER, 10)).headers["location"])
        self.assertFalse((await self.row(10)).needs_review)


class AutomationReviewTemplateTests(ReviewCase):
    async def render(self, shop_id=2, missing=()):
        from app.api.app import templates

        items, cursor = await list_review_queue(self.session, shop_id=shop_id)
        from app.services.automation_review import STATE_LABELS
        from app.services.service_automation import LABELS

        request = SimpleNamespace(url=SimpleNamespace(path="/finance"), query_params={}, state=SimpleNamespace(), cookies={}, headers={})
        return templates.env.get_template("finance.html").render(
            request=request, staff=OWNER, finance_tab="automation", can_orders=True, can_payments=False, csrf_token="TOK",
            automation_items=items, automation_next=cursor, automation_labels=STATE_LABELS, automation_actions=LABELS,
            automation_missing_bots=list(missing), automation_flash_err=None, flash_ok=None, flash_err=None,
            can_finance_settings=False, q="", pager=None, csp_nonce="n",
        )

    async def test_release_form_only_for_clearable_items_and_money_check_only_when_debited(self):
        html = await self.render()
        self.assertIn('action="/service-automation/13/clear"', html)
        self.assertIn('action="/service-automation/14/clear"', html)
        self.assertNotIn('action="/service-automation/11/clear"', html)
        self.assertNotIn('action="/service-automation/12/clear"', html)
        self.assertNotIn('name="money_settled"', html)
        await self.debit()
        self.assertIn('name="money_settled"', await self.render())

    async def test_customer_name_is_escaped_and_missing_bots_are_flagged(self):
        html = await self.render(missing=[2])
        self.assertIn("&lt;b&gt;Shop&lt;/b&gt; Customer", html)
        self.assertNotIn("<b>Shop</b> Customer", html)
        self.assertIn("ربات ندارند", html)


class AutomationReviewBotTests(ReviewCase):
    async def press(self, data, *, scope=(True, 2)):
        handler = {"list": bot.review_list, "v": bot.review_card, "go": bot.review_release}[data.split(":")[1]]
        callback = AsyncMock()
        callback.data = data
        callback.message = AsyncMock()
        with patch.object(bot, "review_scope", AsyncMock(return_value=scope)), \
                patch.object(bot, "safe_edit_text", AsyncMock()) as edit:
            await handler(callback, self.session, SimpleNamespace(id=9, telegram_id=777), is_reseller_bot=False, reseller_owner_id=None)
        return callback, edit

    def shown(self, edit):
        return edit.await_args.args[1], [b.callback_data for row in edit.await_args.kwargs["reply_markup"].inline_keyboard for b in row]

    async def test_denied_actor_sees_and_changes_nothing(self):
        for data in ("autorv:list", "autorv:v:13", "autorv:go:13:0"):
            callback, edit = await self.press(data, scope=(False, 2))
            callback.answer.assert_awaited_once_with(feedback.NO_ACCESS, show_alert=True)
            edit.assert_not_awaited()
        self.assertTrue((await self.row(13)).needs_review)

    async def test_list_and_card_are_scoped_and_navigate_back_one_step(self):
        _, edit = await self.press("autorv:list")
        _, buttons = self.shown(edit)
        self.assertEqual(buttons, ["autorv:v:11", "autorv:v:12", "autorv:v:13", "autorv:v:14", bot.SHOP_BACK])
        _, edit = await self.press("autorv:v:13")
        self.assertEqual(self.shown(edit)[1], ["autorv:go:13:0", "autorv:go:13:2", "autorv:list"])
        callback, _ = await self.press("autorv:v:15")
        callback.answer.assert_awaited_with(feedback.NOT_FOUND, show_alert=True)

    async def test_card_for_an_unresolved_order_has_no_release_button(self):
        _, edit = await self.press("autorv:v:11")
        self.assertEqual(self.shown(edit)[1], ["autorv:list"])

    async def test_debited_card_only_offers_the_confirmed_release(self):
        await self.debit()
        _, edit = await self.press("autorv:v:13")
        self.assertEqual(self.shown(edit)[1], ["autorv:go:13:1", "autorv:list"])
        callback, _ = await self.press("autorv:go:13:0")
        self.assertTrue(callback.answer.await_args.kwargs["show_alert"])
        self.assertTrue((await self.row(13)).needs_review)

    async def test_release_and_other_shop_attempt(self):
        callback, edit = await self.press("autorv:go:15:0")
        self.assertTrue((await self.row(15)).needs_review)
        callback, edit = await self.press("autorv:go:13:2")
        callback.answer.assert_awaited_with("آزاد شد", show_alert=True)
        row = await self.row(13)
        self.assertEqual((row.needs_review, row.renew_enabled), (False, False))
        self.assertNotIn("autorv:v:13", self.shown(edit)[1])

    async def test_scope_needs_the_orders_permission_and_a_shop_bot_never_gets_platform(self):
        async def scope(actor, perm, owner=False, shop_bot=False):
            with patch.object(bot, "load_reseller_actor", AsyncMock(return_value=actor)), \
                    patch.object(bot, "has_bot_perm", MagicMock(return_value=perm)), \
                    patch.object(bot, "require_bot_owner", AsyncMock(return_value=owner)):
                return await bot.review_scope(MagicMock(), MagicMock(), is_reseller_bot=shop_bot, reseller_owner_id=None)

        self.assertEqual(await scope((2, object()), True), (True, 2))
        self.assertEqual(await scope((2, object()), False), (False, 2))
        self.assertEqual(await scope((None, None), False, owner=True), (True, None))
        self.assertEqual(await scope((None, None), False, owner=True, shop_bot=True), (False, None))


if __name__ == "__main__":
    unittest.main()
