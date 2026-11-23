"""«لغو سرویس» settings: the switch, plan list, deadline, refund rule and automatic approval."""

from __future__ import annotations

import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from unittest.mock import AsyncMock, patch

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.nav_inline import service_more_keyboard
from app.db.models import Base, BotUser, Order, Plan, ServiceCancellation, ShopWallet, UserService, WalletTransaction
from app.services import service_cancellations as cancellations
from app.services.cancellation_context import cancellation_detail_lines
from app.services.cancellation_notifications import _notice
from app.services.cancellation_refund import PeriodPayment, period_payment, quote_refund, remaining_share
from app.services.service_actions_page import normalize_service_action_values
from app.services.service_cancellation_settings import (
    APPROVAL_AUTO,
    APPROVAL_MANUAL,
    AUTO_ACTOR,
    REFUND_FULL,
    REFUND_NONE,
    REFUND_REMAINING,
    cancellation_policy,
)
from app.services.settings_tab_groups import group_settings_tabs
from app.services.users import SETTINGS_TABS, _SETTINGS_CACHE, get_all_settings, reset_shop_reseller_id, set_shop_reseller_id
from tests.postgres_support import scoped_async_engine, scoped_sync_engine
from tests.test_update_owner_only import OWNER_SESSION, SHOP_SESSION, RoutesHarness

ROOT = Path(__file__).resolve().parents[1]
CUSTOMER, OTHER, SHOP, OTHER_SHOP = 1, 2, 7, 8
GB = 1024**3
AUTO = {"service_cancel_approval": APPROVAL_AUTO}


def _callbacks(markup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row]


class FakePanel:
    """A panel user with 30 days bought 10 days ago (20 left) and 10 GB with 5 GB used: volume is the smaller share."""

    def __init__(self, now: datetime) -> None:
        self.info = {"status": "active", "expire": int((now + timedelta(days=20)).timestamp()),
                     "data_limit": 10 * GB, "used_traffic": 5 * GB}
        self.down = False
        self.disables = 0

    async def get_user_by_id(self, user_id: int) -> dict:
        if self.down:
            raise TimeoutError("panel down")
        return {"id": user_id, **self.info}

    async def set_disabled_by_id(self, user_id: int, disabled: bool) -> dict:
        self.disables += 1
        self.info["status"] = "disabled"
        return {"id": user_id, "status": "disabled"}


class PolicyTests(unittest.TestCase):
    def test_defaults_keep_the_request_on_for_every_plan_with_manual_approval(self) -> None:
        policy = cancellation_policy({})
        self.assertTrue(policy.allows_plan(None))
        self.assertTrue(policy.allows_plan(42))
        self.assertEqual((policy.approval, policy.refund), (APPROVAL_MANUAL, REFUND_REMAINING))
        self.assertFalse(policy.is_auto)

    def test_unknown_choices_and_out_of_range_numbers_fall_back_to_safe_values(self) -> None:
        policy = cancellation_policy({
            "service_cancel_approval": "yes-please", "service_cancel_refund": "double",
            "service_cancel_fee_percent": "250", "service_cancel_window_days": "-3", "service_cancel_auto_max": "NaN",
        })
        self.assertEqual((policy.approval, policy.refund), (APPROVAL_MANUAL, REFUND_REMAINING))
        self.assertEqual((policy.fee_percent, policy.window_days, policy.auto_max), (100, 0, 0))

    def test_turned_off_or_unlisted_plan_is_not_allowed(self) -> None:
        self.assertFalse(cancellation_policy({"service_cancel_enabled": "0"}).allows_plan(1))
        listed = cancellation_policy({"service_cancel_plans": "3,4"})
        self.assertTrue(listed.allows_plan(3))
        self.assertFalse(listed.allows_plan(1))
        self.assertFalse(listed.allows_plan(None))

    def test_automatic_cap_zero_means_no_cap(self) -> None:
        self.assertTrue(cancellation_policy(AUTO).auto_allows(10**9))
        capped = cancellation_policy({**AUTO, "service_cancel_auto_max": "5000"})
        self.assertTrue(capped.auto_allows(5000))
        self.assertFalse(capped.auto_allows(5001))
        self.assertFalse(cancellation_policy({}).auto_allows(0))

    def test_saving_clamps_numbers_and_replaces_unoffered_choices(self) -> None:
        payload = {"service_cancel_approval": "<script>", "service_cancel_refund": "full",
                   "service_cancel_fee_percent": "500", "service_cancel_window_days": "abc"}
        normalize_service_action_values(payload)
        self.assertEqual(payload["service_cancel_approval"], APPROVAL_MANUAL)
        self.assertEqual(payload["service_cancel_refund"], REFUND_FULL)
        self.assertEqual(payload["service_cancel_fee_percent"], "100")
        self.assertNotIn("service_cancel_window_days", payload)

    def test_the_tab_is_a_section_of_customer_services(self) -> None:
        groups = group_settings_tabs(SETTINGS_TABS, "service_cancel")
        merged = [tab for group in groups for tab in group.tabs if tab.key == "customer_services"]
        self.assertEqual(len(merged), 1)
        self.assertIn("service_cancel", [section.key for section in merged[0].sections])
        self.assertTrue(merged[0].shows("service_cancel"))


class QuoteMathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 11, 1, tzinfo=timezone.utc)
        self.service = UserService(id=1, bot_user_id=1, pg_username="s")
        self.started = self.now - timedelta(days=10)

    def share(self, **info) -> Fraction:
        return remaining_share(info, self.service, self.started, self.now)

    def test_the_smaller_of_time_and_volume_left_is_used(self) -> None:
        expire = int((self.now + timedelta(days=20)).timestamp())
        self.assertEqual(self.share(expire=expire, data_limit=10 * GB, used_traffic=2 * GB), Fraction(2, 3))
        self.assertEqual(self.share(expire=expire, data_limit=10 * GB, used_traffic=9 * GB), Fraction(1, 10))

    def test_expired_or_used_up_leaves_nothing_and_unlimited_counts_as_full(self) -> None:
        past = int((self.now - timedelta(days=1)).timestamp())
        self.assertEqual(self.share(expire=past, data_limit=10 * GB, used_traffic=0), 0)
        self.assertEqual(self.share(expire=0, data_limit=10 * GB, used_traffic=10 * GB), 0)
        self.assertEqual(self.share(expire=0, data_limit=0, used_traffic=5 * GB), 1)

    def test_modes_round_down_and_take_the_fee_last(self) -> None:
        payment = PeriodPayment(100_001, self.started)
        third = Fraction(1, 3)
        remaining = quote_refund(cancellation_policy({"service_cancel_fee_percent": "10"}), payment, third)
        # 100,001 / 3 = 33,333 (rounded down), less 10% = 29,999 (rounded down).
        self.assertEqual((remaining.amount, remaining.remaining_percent, remaining.paid_amount), (29_999, 33, 100_001))
        full = quote_refund(cancellation_policy({"service_cancel_refund": REFUND_FULL}), payment, third)
        self.assertEqual(full.amount, 100_001)
        none = quote_refund(cancellation_policy({"service_cancel_refund": REFUND_NONE}), payment, third)
        self.assertEqual(none.amount, 0)
        self.assertIsInstance(remaining.amount, int)


class FlowCase(unittest.IsolatedAsyncioTestCase):
    """A platform and a shop service, each bought 10 days ago for 300,000 by the same customer."""

    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.session = self.Session()
        self.token = set_shop_reseller_id(None)
        _SETTINGS_CACHE.clear()
        self.now = datetime.now(timezone.utc)
        bought = self.now - timedelta(days=10)
        self.session.add_all([
            BotUser(id=SHOP, telegram_id=1007, referral_code="s7", role="reseller"),
            BotUser(id=OTHER_SHOP, telegram_id=1008, referral_code="s8", role="reseller"),
        ])
        await self.session.flush()
        self.session.add_all([
            BotUser(id=CUSTOMER, telegram_id=1001, referral_code="c1", wallet_balance=0),
            BotUser(id=OTHER, telegram_id=1002, referral_code="c2", wallet_balance=0),
            Plan(id=1, name="month", price=300_000, duration_days=30, data_limit_gb=10),
            Plan(id=2, name="other", price=100_000, duration_days=30, data_limit_gb=10),
        ])
        await self.session.flush()
        self.session.add_all([
            Order(id=100, user_id=CUSTOMER, plan_id=1, amount=300_000, status="delivered", delivered_at=bought),
            Order(id=200, user_id=CUSTOMER, plan_id=1, amount=300_000, status="delivered", reseller_id=SHOP, delivered_at=bought),
        ])
        await self.session.flush()
        self.session.add_all([
            UserService(id=10, bot_user_id=CUSTOMER, plan_id=1, pg_user_id=500, pg_username="mine", remark="order:100"),
            UserService(id=20, bot_user_id=CUSTOMER, plan_id=1, pg_user_id=600, pg_username="shop", remark="order:200", shop_id=SHOP),
        ])
        await self.session.commit()
        self.panel = FakePanel(self.now)
        self.patches = [
            patch("app.services.service_cancellations.get_pg", return_value=self.panel),
            patch("app.services.service_cancellations.get_pg_for_reseller", AsyncMock(return_value=self.panel)),
            patch("app.services.service_cancellations.notify_cancellation_decision", AsyncMock(return_value=True)),
            patch("app.services.service_cancellations.reverse_after_refund", AsyncMock()),
        ]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        await self.session.close()
        await self.engine.dispose()
        reset_shop_reseller_id(self.token)
        _SETTINGS_CACHE.clear()

    async def request(self, ui: dict, *, service_id: int = 10, shop_id: int | None = None, user_id: int = CUSTOMER) -> dict:
        user = await self.session.get(BotUser, user_id)
        return await cancellations.request_cancellation(
            self.session, user, service_id, shop_id=shop_id, reason="دیگر نیاز ندارم", ui=ui
        )

    async def rows(self) -> int:
        return int(await self.session.scalar(select(func.count()).select_from(ServiceCancellation)) or 0)

    async def platform_balance(self) -> int:
        self.session.expire_all()
        return int((await self.session.get(BotUser, CUSTOMER)).wallet_balance or 0)


class CancellationFlowTests(FlowCase):
    async def test_turned_off_unlisted_plan_or_passed_deadline_records_nothing(self) -> None:
        for ui in ({"service_cancel_enabled": "0"}, {"service_cancel_plans": "2"}, {"service_cancel_window_days": "5"}):
            with self.subTest(ui=ui), self.assertRaises(ValueError):
                await self.request(ui)
        self.assertEqual(await self.rows(), 0)
        self.assertEqual(self.panel.disables, 0)

    async def test_manual_request_stores_the_suggestion_and_moves_no_money(self) -> None:
        status = await self.request({})
        self.assertEqual(status["status"], "pending")
        row = await self.session.get(ServiceCancellation, status["id"])
        # 20 of 30 days and 5 of 10 GB left: the smaller share, one half, of 300,000.
        self.assertEqual((row.paid_amount, row.remaining_percent, row.suggested_refund), (300_000, 50, 150_000))
        self.assertIsNone(row.refund_amount)
        self.assertEqual(self.panel.disables, 0)
        self.assertEqual(await self.platform_balance(), 0)
        await self.session.refresh(row)
        self.assertIn("150,000", cancellation_detail_lines(row, None, None, None, currency="تومان").replace("٬", ","))

    async def test_automatic_request_cancels_and_credits_once(self) -> None:
        first = await self.request(AUTO)
        self.assertEqual((first["status"], first["refund_amount"]), ("approved", 150_000))
        again = await self.request(AUTO)
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(await self.platform_balance(), 150_000)
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(WalletTransaction)), 1)
        self.assertEqual(self.panel.disables, 1)
        row = await self.session.get(ServiceCancellation, first["id"])
        self.assertEqual(row.processed_by, AUTO_ACTOR)
        self.assertTrue((await self.session.get(UserService, 10)).is_cancelled)
        user = await self.session.get(BotUser, CUSTOMER)
        service = await self.session.get(UserService, 10)
        self.assertIn("خودکار", _notice(row, user, service, "month").text)

    async def test_full_refund_with_fee_is_paid_automatically(self) -> None:
        status = await self.request({**AUTO, "service_cancel_refund": REFUND_FULL, "service_cancel_fee_percent": "10"})
        self.assertEqual(status["refund_amount"], 270_000)
        self.assertEqual(await self.platform_balance(), 270_000)

    async def test_amount_over_the_automatic_cap_waits_for_an_operator(self) -> None:
        status = await self.request({**AUTO, "service_cancel_auto_max": "100000"})
        self.assertEqual(status["status"], "pending")
        self.assertEqual(self.panel.disables, 0)
        self.assertEqual(await self.platform_balance(), 0)

    async def test_panel_down_leaves_the_request_for_an_operator_without_a_suggestion(self) -> None:
        self.panel.down = True
        status = await self.request(AUTO)
        self.assertEqual(status["status"], "pending")
        row = await self.session.get(ServiceCancellation, status["id"])
        self.assertIsNone(row.suggested_refund)
        self.assertEqual(await self.platform_balance(), 0)

    async def test_shop_refund_goes_to_that_shops_wallet_and_other_shops_and_customers_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            await self.request(AUTO, service_id=20, shop_id=OTHER_SHOP)
        with self.assertRaises(ValueError):
            await self.request(AUTO, service_id=20, shop_id=SHOP, user_id=OTHER)
        self.assertEqual(await self.rows(), 0)
        status = await self.request(AUTO, service_id=20, shop_id=SHOP)
        self.assertEqual(status["status"], "approved")
        wallet = await self.session.scalar(
            select(ShopWallet.balance).where(ShopWallet.user_id == CUSTOMER, ShopWallet.reseller_id == SHOP)
        )
        self.assertEqual(int(wallet or 0), 150_000)
        self.assertEqual(await self.platform_balance(), 0)
        with self.assertRaises(ValueError):
            await cancellations.approve_cancellation(
                self.session, status["id"], shop_id=OTHER_SHOP, amount=1, actor="x"
            )

    async def test_only_the_owners_delivered_orders_of_the_current_period_count(self) -> None:
        later = self.now - timedelta(days=2)
        self.session.add_all([
            # An add-on bought in this period counts; a voided one and another customer's do not.
            Order(id=101, user_id=CUSTOMER, plan_id=None, amount=50_000, status="delivered", service_id=10, delivered_at=later),
            Order(id=102, user_id=CUSTOMER, plan_id=None, amount=70_000, status="cancelled", service_id=10, delivered_at=later),
            Order(id=103, user_id=OTHER, plan_id=None, amount=90_000, status="delivered", service_id=10, delivered_at=later),
        ])
        await self.session.commit()
        payment = await period_payment(self.session, await self.session.get(UserService, 10))
        self.assertEqual(payment.amount, 350_000)
        self.session.add(Order(id=104, user_id=CUSTOMER, plan_id=1, amount=280_000, status="delivered",
                               service_id=10, delivered_at=self.now - timedelta(days=1)))
        await self.session.commit()
        renewed = await period_payment(self.session, await self.session.get(UserService, 10))
        self.assertEqual(renewed.amount, 280_000, "a renewal opens a new period")

    async def test_offer_shows_quote_and_the_more_menu_follows_the_switch(self) -> None:
        user = await self.session.get(BotUser, CUSTOMER)
        offer = await cancellations.cancellation_offer(self.session, user, 10, shop_id=None, ui=AUTO)
        self.assertTrue(offer.can_request)
        self.assertEqual(offer.quote.amount, 150_000)
        off = await cancellations.cancellation_offer(self.session, user, 10, shop_id=None, ui={"service_cancel_enabled": "0"})
        self.assertFalse(off.can_request)
        self.assertIsNone(off.quote)
        service = await self.session.get(UserService, 10)
        self.assertTrue(cancellations.shows_cancel_button(service, {}))
        self.assertFalse(cancellations.shows_cancel_button(service, {"service_cancel_enabled": "0"}))
        self.assertNotIn("svc:cancel:10", _callbacks(service_more_keyboard(10, {}, cancel=False)))
        self.assertIn("svc:cancel:10", _callbacks(service_more_keyboard(10, {}, cancel=True)))
        service.cancellation_pending = True
        self.assertTrue(cancellations.shows_cancel_button(service, {"service_cancel_enabled": "0"}),
                        "an open request stays reachable after the shop turns the feature off")


class FakeState:
    def __init__(self) -> None:
        self.data: dict = {}
        self.state = None

    async def get_data(self) -> dict:
        return dict(self.data)

    async def update_data(self, **kwargs) -> None:
        self.data.update(kwargs)

    async def set_state(self, value) -> None:
        self.state = value


class CustomerBotTests(FlowCase):
    """The bot's customer steps: an automatic cancellation is confirmed before anything happens."""

    async def run_handler(self, handler, event, state: FakeState, ui: dict):
        from app.bot.handlers import services

        with patch.object(services, "get_all_settings", AsyncMock(return_value=ui)), \
                patch.object(services, "current_shop_reseller_id", return_value=None), \
                patch.object(services, "safe_edit_text", AsyncMock()) as edit, \
                patch("app.bot.nav_input.finish_text_step", AsyncMock()) as finish:
            await handler(event, session=self.session, db_user=await self.session.get(BotUser, CUSTOMER), state=state)
        return edit, finish

    def message(self, text: str):
        message = AsyncMock()
        message.text = text
        return message

    def callback(self, data: str):
        callback = AsyncMock()
        callback.data = data
        return callback

    async def test_automatic_mode_asks_for_confirmation_then_cancels_once(self) -> None:
        from app.bot.handlers import services

        state = FakeState()
        state.data["cancellation_service_id"] = 10
        _, finish = await self.run_handler(services.svc_cancel_reason, self.message("<b>گران است</b>"), state, AUTO)
        self.assertEqual(await self.rows(), 0, "typing the reason alone files nothing")
        self.assertIn("svc:cancelok:10", _callbacks(finish.await_args.kwargs["inline"]))
        edit, _ = await self.run_handler(services.svc_cancel_confirm, self.callback("svc:cancelok:10"), state, AUTO)
        self.assertIn("سرویس لغو شد", edit.await_args.args[1])
        self.assertEqual(await self.platform_balance(), 150_000)
        await self.run_handler(services.svc_cancel_confirm, self.callback("svc:cancelok:10"), state, AUTO)
        self.assertEqual(await self.rows(), 1)
        self.assertEqual(await self.platform_balance(), 150_000, "a second tap does not credit again")

    async def test_confirm_for_another_service_or_without_a_reason_is_refused(self) -> None:
        from app.bot.handlers import services

        state = FakeState()
        state.data.update(cancellation_service_id=10, cancellation_reason="x")
        callback = self.callback("svc:cancelok:20")
        await self.run_handler(services.svc_cancel_confirm, callback, state, AUTO)
        self.assertTrue(callback.answer.await_args.kwargs.get("show_alert"))
        state.data["cancellation_reason"] = None
        await self.run_handler(services.svc_cancel_confirm, self.callback("svc:cancelok:10"), state, AUTO)
        self.assertEqual(await self.rows(), 0)

    async def test_manual_mode_files_the_request_from_the_reason_step(self) -> None:
        from app.bot.handlers import services

        state = FakeState()
        state.data["cancellation_service_id"] = 10
        await self.run_handler(services.svc_cancel_reason, self.message("دیگر لازم ندارم"), state, {})
        self.assertEqual(await self.rows(), 1)
        self.assertEqual(self.panel.disables, 0)

    async def test_screen_escapes_the_refusal_and_offers_no_request_button(self) -> None:
        from app.bot import cancellation_screen

        user = await self.session.get(BotUser, CUSTOMER)
        offer = await cancellations.cancellation_offer(self.session, user, 10, shop_id=None, ui={"service_cancel_enabled": "0"})
        offer = cancellations.CancellationOffer(None, offer.policy, "<b>x</b>", None)
        text, markup = cancellation_screen.offer_screen(offer, 10, "تومان")
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", text)
        self.assertNotIn("svc:cancelnew:10", _callbacks(markup))


class SettingsPageTests(RoutesHarness):
    """The «لغو سرویس» section of the Owner's and a shop's bot settings page."""

    async def send(self, session_payload: dict, path: str, data: dict | None = None):
        cookie = f"session={self.signer.dumps(session_payload)}; csrf=token"
        headers = {"cookie": cookie, "x-csrf-token": "token", "origin": "http://panel.test"}
        if data is None:
            return await self.client.get(path, headers=headers)
        return await self.client.post(path, headers=headers, data={"csrf_token": "token", **data})

    async def settings(self, shop_id: int | None) -> dict:
        _SETTINGS_CACHE.clear()
        async with async_sessionmaker(self.engine)() as session:
            return await get_all_settings(session, reseller_id=shop_id)

    async def test_owner_and_shop_pages_show_the_section_inside_customer_services(self) -> None:
        for session_payload, path in ((OWNER_SESSION, "/settings?tab=service_cancel"), (SHOP_SESSION, "/shop-settings?tab=service_cancel")):
            with self.subTest(path=path):
                response = await self.send(session_payload, path)
                self.assertEqual(response.status_code, 200)
                for marker in ('name="s_service_cancel_enabled"', 'id="sc-approval"', 'name="s_service_cancel_auto_max"', "ذخیرهٔ لغو سرویس"):
                    self.assertIn(marker, response.text)

    async def test_saving_keeps_values_valid_and_each_shop_keeps_its_own(self) -> None:
        owner = await self.send(OWNER_SESSION, "/settings?tab=service_cancel", {
            "s_service_cancel_enabled": "1", "s_service_cancel_plans_mode": "all",
            "s_service_cancel_approval": "<script>", "s_service_cancel_refund": "full",
            "s_service_cancel_fee_percent": "900", "s_service_cancel_window_days": "7", "s_service_cancel_auto_max": "0",
        })
        self.assertEqual(owner.status_code, 303)
        platform = await self.settings(None)
        self.assertEqual(platform["service_cancel_approval"], APPROVAL_MANUAL)
        self.assertEqual(platform["service_cancel_refund"], REFUND_FULL)
        self.assertEqual(platform["service_cancel_fee_percent"], "100")
        shop = await self.send(SHOP_SESSION, "/shop-settings?tab=service_cancel", {
            "s_service_cancel_plans_mode": "all", "s_service_cancel_approval": APPROVAL_AUTO,
            "s_service_cancel_refund": "remaining", "s_service_cancel_fee_percent": "5",
            "s_service_cancel_window_days": "0", "s_service_cancel_auto_max": "50000",
        })
        self.assertEqual(shop.status_code, 303)
        shop_values = await self.settings(5)
        self.assertEqual(shop_values["service_cancel_enabled"], "0", "an unchecked switch saves off")
        self.assertEqual(shop_values["service_cancel_approval"], APPROVAL_AUTO)
        self.assertEqual((await self.settings(None))["service_cancel_approval"], APPROVAL_MANUAL,
                         "a shop's save never changes the platform's settings")


class RefundQuoteMigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade_keep_existing_requests(self) -> None:
        spec = importlib.util.spec_from_file_location("quote_migration", ROOT / "alembic/versions/0132_cancellation_refund_quote.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = scoped_sync_engine()
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE service_cancellations (id INTEGER PRIMARY KEY, reason TEXT)"))
            conn.execute(text("INSERT INTO service_cancellations VALUES (1, 'old')"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                columns = {c["name"] for c in sa.inspect(conn).get_columns("service_cancellations")}
                self.assertTrue({"paid_amount", "remaining_percent", "suggested_refund"} <= columns)
                self.assertIsNone(conn.execute(text("SELECT suggested_refund FROM service_cancellations")).scalar())
                migration.downgrade()
                migration.downgrade()
                columns = {c["name"] for c in sa.inspect(conn).get_columns("service_cancellations")}
                self.assertNotIn("suggested_refund", columns)
                self.assertEqual(conn.execute(text("SELECT reason FROM service_cancellations")).scalar(), "old")
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
