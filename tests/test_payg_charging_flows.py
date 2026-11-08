"""PAYG charging at creation: shop deliveries, staff edits, resets and the usage tick."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.payg_plan_form import payg_rate_and_settings
from app.db.models import (
    Base,
    BotUser,
    Order,
    OrderStatus,
    Plan,
    ResellerCharge,
    ResellerPlan,
    ResellerProfile,
)
from app.services.billing import shop_bills_usage, tick_reseller_usage
from app.services.orders import deliver_order
from app.services.pasarguard import PasarGuardError
from app.services.payg_gate import (
    SHORTFALL_FOR_CUSTOMER,
    added_days,
    reset_with_charge,
    service_edit_changes,
    template_volume,
)
from app.services.payg_pricing import GB, ChargeEvent
from app.services.provision_gate import ProvisionError

SHOP = 5
DAY = 86400


class FakeShopPanel:
    """The shop's own PasarGuard client: a 10 GB / 30 day template."""

    _login_username = "shop_a"

    def __init__(self, *, create_error: Exception | None = None, template: dict | None = None) -> None:
        self.create_error = create_error
        self.template = template if template is not None else {"data_limit": 10 * GB, "expire_duration": 30 * DAY}
        self.create_user_from_template = AsyncMock(side_effect=self._create)
        self.get_user_template = AsyncMock(side_effect=self._template)
        self.delete_user_by_id = AsyncMock()

    async def _template(self, template_id: int) -> dict:
        return self.template

    async def _create(self, payload: dict) -> dict:
        if self.create_error is not None:
            raise self.create_error
        return {"id": 901, "username": payload["username"], "subscription_url": "https://e.example/sub/x"}


class DeliveryCase(unittest.IsolatedAsyncioTestCase):
    upfront = True
    wallet = 50_000

    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=SHOP, telegram_id=5, referral_code="shop", role="reseller", wallet_balance=self.wallet),
                BotUser(id=9, telegram_id=9, referral_code="buyer"),
                ResellerPlan(id=1, name="payg", billing_mode="payg", price_per_gb=1000,
                             payg_basis="traffic", payg_charge_upfront=self.upfront),
                Plan(id=3, name="Monthly", price=100, pg_template_id=1),
            ])
            session.add(ResellerProfile(
                id=1, user_id=SHOP, plan_id=1, billing_mode="payg", payg_wallet_linked=True,
                pg_admin_username="shop_a",
            ))
            session.add(Order(id=12, user_id=9, plan_id=3, amount=100, status=OrderStatus.PAID.value,
                              reseller_id=SHOP))
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def deliver(self, panel: FakeShopPanel) -> Order:
        async with self.Session() as session:
            order = await session.get(Order, 12)
            with (
                patch("app.services.pasarguard.get_pg_for_reseller", AsyncMock(return_value=panel)),
                patch("app.services.orders.assert_provision_create", AsyncMock()),
                patch("app.services.users.get_all_settings", AsyncMock(return_value={})),
                patch("app.services.loyalty.on_order_delivered", AsyncMock()),
                patch("app.services.billing_alerts._send", AsyncMock()),
            ):
                return await deliver_order(session, order)

    async def wallet_now(self) -> int:
        async with self.Session() as session:
            return int((await session.get(BotUser, SHOP)).wallet_balance)

    async def charges(self) -> list[ResellerCharge]:
        async with self.Session() as session:
            return list((await session.scalars(select(ResellerCharge).order_by(ResellerCharge.id))).all())

    async def order_status(self) -> str:
        async with self.Session() as session:
            return (await session.get(Order, 12)).status


class UpfrontDeliveryTests(DeliveryCase):
    async def test_delivery_charges_the_template_volume_once(self) -> None:
        order = await self.deliver(FakeShopPanel())
        self.assertEqual(order.status, OrderStatus.DELIVERED.value)
        self.assertEqual(await self.wallet_now(), 40_000)
        [charge] = await self.charges()
        self.assertEqual((charge.status, charge.amount, charge.bytes_charged), ("settled", 10_000, 10 * GB))
        await self.deliver(FakeShopPanel())
        self.assertEqual(await self.wallet_now(), 40_000)
        self.assertEqual(len(await self.charges()), 1)

    async def test_panel_refusal_refunds_and_a_retry_charges_again(self) -> None:
        with self.assertRaises(Exception):
            await self.deliver(FakeShopPanel(create_error=PasarGuardError("bad", 409)))
        self.assertEqual(await self.order_status(), OrderStatus.PAID.value)
        self.assertEqual(await self.wallet_now(), 50_000)
        self.assertEqual([c.status for c in await self.charges()], ["refunded"])
        await self.deliver(FakeShopPanel())
        self.assertEqual(await self.wallet_now(), 40_000)
        self.assertEqual([(c.status, c.attempt) for c in await self.charges()], [("refunded", 1), ("settled", 2)])

    async def test_unknown_panel_outcome_keeps_the_money_for_review(self) -> None:
        with self.assertRaises(Exception):
            await self.deliver(FakeShopPanel(create_error=httpx.ReadTimeout("slow")))
        self.assertEqual(await self.wallet_now(), 40_000)
        self.assertEqual([c.status for c in await self.charges()], ["review"])
        # The retry reuses the held charge instead of charging twice.
        await self.deliver(FakeShopPanel())
        self.assertEqual(await self.wallet_now(), 40_000)
        self.assertEqual([c.status for c in await self.charges()], ["settled"])

    async def test_unreadable_template_sells_nothing(self) -> None:
        panel = FakeShopPanel(template={"data_limit": "lots"})
        with self.assertRaises(ValueError) as ctx:
            await self.deliver(panel)
        self.assertEqual(str(ctx.exception), SHORTFALL_FOR_CUSTOMER)
        panel.create_user_from_template.assert_not_awaited()
        self.assertEqual(await self.wallet_now(), 50_000)
        self.assertEqual(await self.order_status(), OrderStatus.PAID.value)


class ShortfallDeliveryTests(DeliveryCase):
    wallet = 5_000

    async def test_shortfall_refuses_before_the_panel_with_a_safe_message(self) -> None:
        panel = FakeShopPanel()
        with self.assertRaises(ValueError) as ctx:
            await self.deliver(panel)
        self.assertEqual(str(ctx.exception), SHORTFALL_FOR_CUSTOMER)
        self.assertNotIn("کیف پول", str(ctx.exception))
        panel.create_user_from_template.assert_not_awaited()
        self.assertEqual(await self.wallet_now(), 5_000)
        self.assertEqual(await self.charges(), [])


class UsageBilledDeliveryTests(DeliveryCase):
    upfront = False

    async def test_usage_billed_shop_is_not_charged_at_creation(self) -> None:
        panel = FakeShopPanel()
        await self.deliver(panel)
        panel.get_user_template.assert_not_awaited()
        self.assertEqual(await self.wallet_now(), 50_000)
        self.assertEqual(await self.charges(), [])


class ResetTests(DeliveryCase):
    async def test_reset_charges_the_whole_volume_and_refunds_a_refused_reset(self) -> None:
        staff = {"role": "reseller", "bot_user_id": SHOP}
        user = {"id": 901, "username": "c1", "data_limit": 20 * GB}
        panel = SimpleNamespace(
            get_user_by_id=AsyncMock(return_value=user), reset_user_by_id=AsyncMock(return_value=user)
        )
        async with self.Session() as session:
            await reset_with_charge(session, staff, pg=panel, pg_user_id=901, now_ts=1_000_000)
        self.assertEqual(await self.wallet_now(), 30_000)
        refused = SimpleNamespace(
            get_user_by_id=AsyncMock(return_value=user),
            reset_user_by_id=AsyncMock(side_effect=PasarGuardError("no", 403)),
        )
        async with self.Session() as session:
            with self.assertRaises(PasarGuardError):
                await reset_with_charge(session, staff, pg=refused, pg_user_id=901, now_ts=1_000_000)
        self.assertEqual(await self.wallet_now(), 30_000)
        self.assertEqual([c.status for c in await self.charges()], ["settled", "refunded"])

    async def test_platform_staff_reset_is_never_charged(self) -> None:
        panel = SimpleNamespace(get_user_by_id=AsyncMock(), reset_user_by_id=AsyncMock(return_value={}))
        async with self.Session() as session:
            await reset_with_charge(session, {"role": "admin"}, pg=panel, pg_user_id=901, now_ts=1)
        panel.get_user_by_id.assert_not_awaited()
        self.assertEqual(await self.wallet_now(), 50_000)


class UsageTickTests(DeliveryCase):
    async def test_upfront_shop_tick_advances_the_counter_without_charging(self) -> None:
        async with self.Session() as session:
            profile = await session.get(ResellerProfile, 1)
            self.assertFalse(await shop_bills_usage(session, profile))
            await tick_reseller_usage(session, profile, {"lifetime_used_traffic": 7 * GB})
            self.assertEqual(profile.billing_watermark_bytes, 7 * GB)
        self.assertEqual(await self.wallet_now(), 50_000)


class EditChangeTests(unittest.TestCase):
    def test_renew_is_one_new_period(self) -> None:
        [change] = service_edit_changes(renew=True, current_limit=5 * GB, new_limit=20 * GB, current_expire=None,
                                        new_expire=1_000 + 30 * DAY, reset=True, now_ts=1_000)
        self.assertEqual((change.event, change.data_limit_bytes, change.days), (ChargeEvent.RENEW, 20 * GB, 30))

    def test_reset_with_a_bigger_limit_is_not_counted_twice(self) -> None:
        changes = service_edit_changes(renew=False, current_limit=5 * GB, new_limit=20 * GB, current_expire=None,
                                       new_expire=None, reset=True, now_ts=0)
        self.assertEqual([(c.event, c.data_limit_bytes) for c in changes], [(ChargeEvent.RESET, 20 * GB)])

    def test_volume_and_days_are_separate_parts(self) -> None:
        changes = service_edit_changes(renew=False, current_limit=5 * GB, new_limit=8 * GB,
                                       current_expire=10 * DAY, new_expire=40 * DAY, reset=False, now_ts=DAY)
        self.assertEqual([c.event for c in changes], [ChargeEvent.ADD_VOLUME, ChargeEvent.ADD_DAYS])
        self.assertEqual(changes[1].days, 30)

    def test_added_days(self) -> None:
        self.assertEqual(added_days(10 * DAY, 40 * DAY, now_ts=DAY), 30)
        self.assertEqual(added_days(DAY, 31 * DAY, now_ts=11 * DAY), 20)  # expired: counted from now
        self.assertIsNone(added_days(10 * DAY, 0, now_ts=DAY))  # becomes unlimited
        self.assertEqual(added_days(None, 30 * DAY, now_ts=DAY), 0)  # unlimited user gets a limit

    def test_template_volume_fails_closed(self) -> None:
        self.assertEqual(template_volume({"data_limit": 10 * GB, "expire_duration": 30 * DAY + 1}), (10 * GB, 31))
        self.assertEqual(template_volume({}), (0, 0))
        for bad in (None, {"data_limit": -1}, {"data_limit": True}, {"expire_duration": "30"}):
            with self.subTest(bad=bad), self.assertRaises(ProvisionError):
                template_volume(bad)


class PlanFormTests(unittest.TestCase):
    def base(self, **extra) -> dict:
        form = {"payg_fields_shown": "1", "payg_basis": "traffic", "price_per_gb": "1000", "payg_charge_upfront": "1"}
        form.update(extra)
        return form

    def test_user_basis_needs_a_user_price_not_a_rate(self) -> None:
        rate, settings = payg_rate_and_settings(
            self.base(payg_basis="user", price_per_gb="0", payg_user_price="20000"), billing_mode="payg"
        )
        self.assertEqual((rate, settings.basis, settings.user_price), (0, "user", 20000))
        with self.assertRaises(ValueError):
            payg_rate_and_settings(self.base(payg_basis="user", payg_user_price="0"), billing_mode="payg")

    def test_empty_unlimited_price_forbids_unlimited_users(self) -> None:
        _, settings = payg_rate_and_settings(self.base(payg_unlimited_price=""), billing_mode="payg")
        self.assertIsNone(settings.unlimited_price)
        _, settings = payg_rate_and_settings(self.base(payg_unlimited_price="300000"), billing_mode="payg")
        self.assertEqual(settings.unlimited_price, 300000)

    def test_hostile_values_are_refused(self) -> None:
        for extra in ({"payg_basis": "free"}, {"payg_user_price": "-5"}, {"payg_user_price": "abc"},
                      {"payg_unlimited_price": "99999999999"}, {"price_per_gb": "0"}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                payg_rate_and_settings(self.base(**extra), billing_mode="payg")

    def test_fixed_packages_ignore_the_payg_fields(self) -> None:
        self.assertEqual(payg_rate_and_settings(self.base(payg_basis="free"), billing_mode="fixed"), (0, None))


if __name__ == "__main__":
    unittest.main()


class ReviewDecisionTests(DeliveryCase):
    async def test_owner_decision_moves_the_money_once(self) -> None:
        from app.services.payg_charges import resolve_review_charge

        with self.assertRaises(Exception):
            await self.deliver(FakeShopPanel(create_error=httpx.ReadTimeout("slow")))
        [charge] = await self.charges()
        async with self.Session() as session:
            self.assertTrue(await resolve_review_charge(session, charge.id, applied=False, actor="owner"))
            self.assertFalse(await resolve_review_charge(session, charge.id, applied=False, actor="owner"))
            self.assertFalse(await resolve_review_charge(session, charge.id, applied=True, actor="owner"))
        self.assertEqual(await self.wallet_now(), 50_000)
        self.assertEqual([c.status for c in await self.charges()], ["refunded"])


class TemplateTests(unittest.TestCase):
    def render(self, name: str, **ctx: object) -> str:
        from jinja2 import ChainableUndefined

        from app.api.app import templates

        env = templates.env.overlay(undefined=ChainableUndefined)
        request = SimpleNamespace(query_params={}, url=SimpleNamespace(path="/x"), cookies={})
        base = {"request": request, "staff": {"role": "admin"}, "path": "/x"}
        base.update(ctx)
        return env.get_template(name).render(**base)

    def test_review_page_escapes_panel_names(self) -> None:
        charge = SimpleNamespace(id=4, shop_id=5, created_at=None, event="reset", quantity=1,
                                 pg_username="<script>x</script>", amount=10_000)
        html = self.render("payg_charges_review.html", charges=[charge], shop_names={5: "<b>shop</b>"})
        self.assertNotIn("<script>x</script>", html)
        self.assertNotIn("<b>shop</b>", html)
        self.assertIn('action="/payg/charges/4/refund"', html)
        self.assertIn("data-confirm", html)

    def test_payg_fields_post_with_the_saved_values(self) -> None:
        from app.api.app import templates

        macro = templates.env.get_template("_payg_pricing_fields.html").module.payg_pricing_fields
        plan = SimpleNamespace(payg_basis="user", payg_charge_upfront=False, payg_user_price=20000,
                               payg_user_price_per_30d=True, payg_unlimited_price=None)
        html = str(macro("edit", plan))
        for field in ("payg_fields_shown", "payg_basis", "payg_charge_upfront", "payg_user_price",
                      "payg_user_price_per_30d", "payg_unlimited_price"):
            self.assertIn(f'name="{field}"', html)
        self.assertIn('id="edit-payg-basis" value="user"', html)
        self.assertIn('value="20000"', html)
        new_plan_html = str(macro("rp"))
        self.assertIn('id="rp-payg-basis" value="traffic"', new_plan_html)
        # Charging at creation is offered switched on for a new package.
        self.assertIn('id="rp-payg-upfront" checked', new_plan_html)
        self.assertNotIn('id="edit-payg-upfront" checked', html)


class CheckoutTests(DeliveryCase):
    wallet = 5_000

    async def plan(self, session, *, gb: float | None = 10, template: bool = False) -> Plan:
        plan = Plan(name="p", price=100, data_limit_gb=gb, duration_days=30, pg_template_id=1 if template else None)
        session.add(plan)
        await session.flush()
        return plan

    async def test_buyer_is_stopped_before_paying_and_the_shop_is_told_once(self) -> None:
        from app.services.orders import _assert_shop_can_afford_plan

        sends = AsyncMock()
        async with self.Session() as session:
            plan = await self.plan(session)
            with patch("app.services.billing_alerts._send", sends):
                for _ in range(3):
                    with self.assertRaises(ValueError) as ctx:
                        await _assert_shop_can_afford_plan(session, SHOP, plan)
                    self.assertEqual(str(ctx.exception), SHORTFALL_FOR_CUSTOMER)
        # Reseller and owner each once, not once per refused buyer.
        self.assertEqual(sends.await_count, 2)
        self.assertEqual(await self.wallet_now(), 5_000)

    async def test_affordable_order_and_template_plans_pass(self) -> None:
        from app.services.orders import _assert_shop_can_afford_plan

        async with self.Session() as session:
            await _assert_shop_can_afford_plan(session, SHOP, await self.plan(session, gb=4))
            await _assert_shop_can_afford_plan(session, SHOP, await self.plan(session, gb=500, template=True))
            await _assert_shop_can_afford_plan(session, None, await self.plan(session, gb=500))

    async def test_a_successful_charge_ends_the_episode(self) -> None:
        from app.db.models import ResellerProfile as Profile
        from app.services.payg_gate import charge_shop_change
        from app.services.payg_pricing import ServiceChange

        async with self.Session() as session:
            profile = await session.get(Profile, 1)
            profile.billing_tick_issue = "wallet_short"
            await session.commit()
            await charge_shop_change(
                session, shop_id=SHOP, change=ServiceChange(ChargeEvent.CREATE, data_limit_bytes=2 * GB, days=30),
                request_key="order:900", pg_username="c",
            )
            await session.refresh(profile)
            self.assertIsNone(profile.billing_tick_issue)


class RateSummaryTests(DeliveryCase):
    async def test_staff_see_the_rates_and_the_charged_amount(self) -> None:
        from app.services.payg_gate import charged_note, shop_rate_summary

        async with self.Session() as session:
            summary = await shop_rate_summary(session, SHOP)
            self.assertIsNone(await shop_rate_summary(session, None))
        self.assertTrue(summary.charges_reset)
        self.assertIn("کل حجم", summary.reset_note)
        self.assertTrue(any("هر گیگ" in line for line in summary.lines))
        self.assertTrue(any("نامحدود: مجاز نیست" in line for line in summary.lines))
        self.assertEqual(charged_note(None), "")
        self.assertIn("از کیف پول کسر شد", charged_note([SimpleNamespace(amount=10_000)]))  # type: ignore[list-item]
