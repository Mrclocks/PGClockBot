"""Stale pending order cleanup: separate approval deadline, preview count and run record."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, BotUser, Order, OrderStatus
from app.services.orders import cancel_stale_pending_for_settings
from app.services.stale_order_cleanup import (
    StaleOrderPolicy,
    cleanup_status,
    count_stale_orders,
    policy_from_settings,
)
from app.services.users import _SETTINGS_CACHE, set_settings_bulk


class StaleOrderCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add_all([
            BotUser(id=1, telegram_id=101, referral_code="buyer"),
            BotUser(id=2, telegram_id=102, referral_code="shop", role="reseller"),
        ])
        await self.session.commit()
        _SETTINGS_CACHE.clear()

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.engine.dispose()

    async def _order(self, status: str, age_hours: int, *, shop: int | None = None) -> int:
        order = Order(user_id=1, amount=5, status=status, reseller_id=shop)
        self.session.add(order)
        await self.session.flush()
        created = datetime.now(timezone.utc) - timedelta(hours=age_hours)
        await self.session.execute(update(Order).where(Order.id == order.id).values(created_at=created))
        await self.session.commit()
        return int(order.id)

    async def _status(self, order_id: int) -> str:
        return str(await self.session.scalar(select(Order.status).where(Order.id == order_id)))

    def test_policy_clamps_hours_and_ignores_approval_unless_enabled(self) -> None:
        self.assertEqual(policy_from_settings({"pending_order_ttl_hours": "9999"}), StaleOrderPolicy(720, None))
        self.assertEqual(policy_from_settings({"pending_order_ttl_hours": "junk"}), StaleOrderPolicy(48, None))
        enabled = {"pending_order_cleanup_awaiting_approval": "1"}
        self.assertEqual(policy_from_settings(enabled), StaleOrderPolicy(48, 72))

    async def test_awaiting_approval_uses_its_own_longer_deadline(self) -> None:
        await set_settings_bulk(self.session, {
            "pending_order_cleanup_enabled": "1", "pending_order_ttl_hours": "24",
            "pending_order_cleanup_awaiting_approval": "1", "pending_order_approval_ttl_hours": "72",
        })
        unpaid = await self._order(OrderStatus.PENDING.value, 30)
        young_review = await self._order(OrderStatus.AWAITING_APPROVAL.value, 30)
        old_review = await self._order(OrderStatus.AWAITING_APPROVAL.value, 80)
        paid = await self._order(OrderStatus.PAID.value, 500)
        self.assertEqual(await cancel_stale_pending_for_settings(self.session), 2)
        self.assertEqual(await self._status(unpaid), "cancelled")
        self.assertEqual(await self._status(old_review), "cancelled")
        self.assertEqual(await self._status(young_review), "awaiting_approval")
        self.assertEqual(await self._status(paid), "paid")

    async def test_awaiting_approval_is_kept_when_not_opted_in(self) -> None:
        await set_settings_bulk(self.session, {"pending_order_cleanup_enabled": "1", "pending_order_ttl_hours": "24"})
        review = await self._order(OrderStatus.AWAITING_APPROVAL.value, 500)
        self.assertEqual(await cancel_stale_pending_for_settings(self.session), 0)
        self.assertEqual(await self._status(review), "awaiting_approval")

    async def test_disabled_cleanup_does_nothing_unless_forced(self) -> None:
        stale = await self._order(OrderStatus.PENDING.value, 100)
        self.assertEqual(await cancel_stale_pending_for_settings(self.session), 0)
        self.assertEqual(await self._status(stale), "pending")
        self.assertEqual(await cancel_stale_pending_for_settings(self.session, force=True), 1)

    async def test_preview_count_matches_what_a_run_cancels_and_stays_in_its_shop(self) -> None:
        await self._order(OrderStatus.PENDING.value, 100)
        await self._order(OrderStatus.PENDING.value, 100, shop=2)
        await self._order(OrderStatus.PENDING.value, 1)
        policy = StaleOrderPolicy(48)
        self.assertEqual(await count_stale_orders(self.session, policy, reseller_id=None), 1)
        self.assertEqual(await count_stale_orders(self.session, policy, reseller_id=2), 1)
        self.assertEqual(await cancel_stale_pending_for_settings(self.session, reseller_id=2, force=True), 1)
        platform = await cleanup_status(self.session, {}, reseller_id=None)
        self.assertEqual(platform["ready_count"], 1)
        self.assertEqual(platform["last_run"], "")

    async def test_run_is_recorded_for_the_shop_that_ran(self) -> None:
        await self._order(OrderStatus.PENDING.value, 100, shop=2)
        await cancel_stale_pending_for_settings(self.session, reseller_id=2, force=True)
        from app.services.users import get_all_settings

        shop = await cleanup_status(self.session, await get_all_settings(self.session, reseller_id=2), reseller_id=2)
        self.assertEqual(shop["last_count"], 1)
        self.assertRegex(str(shop["last_run"]), r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
        platform = await cleanup_status(self.session, await get_all_settings(self.session), reseller_id=None)
        self.assertEqual(platform["last_run"], "")


if __name__ == "__main__":
    unittest.main()


class CleanupTabRenderTests(unittest.TestCase):
    def _render(self, **overrides: object) -> str:
        from app.api.app import templates
        from app.services.users import DEFAULT_SETTINGS, SETTING_GROUPS, TAB_SETTING_GROUPS

        def groups(tab: str) -> dict:
            return {name: SETTING_GROUPS[name] for name in TAB_SETTING_GROUPS[tab]}

        ctx = {
            "open_finance_settings": "cleanup",
            "can_pending_orders": True,
            "can_billing_settings": False,
            "values": dict(DEFAULT_SETTINGS),
            "payment_tab_groups": TAB_SETTING_GROUPS["payment"], "payment_groups": groups("payment"),
            "cleanup_tab_groups": TAB_SETTING_GROUPS["cleanup"], "cleanup_groups": groups("cleanup"),
            "payment_settings_action": "/settings?tab=payment", "cleanup_settings_action": "/settings?tab=cleanup",
            "finance_pending_action": "/settings/cancel-pending-orders",
            "cleanup_status": {"ready_count": 7, "last_run": "2026-10-28 12:00", "last_count": 3},
        }
        ctx.update(overrides)
        return templates.env.get_template("_modal_finance_settings.html").render(**ctx)

    def test_cleanup_tab_shows_status_settings_and_manual_cancel(self) -> None:
        html = self._render()
        self.assertIn('data-modal-tab="cleanup"', html)
        self.assertIn("s_pending_order_approval_ttl_hours", html)
        self.assertIn("2026-10-28 12:00", html)
        self.assertEqual(html.count('action="/settings/cancel-pending-orders"'), 1)
        self.assertEqual(html.count("وضعیت پاکسازی"), 1)

    def test_payment_tab_no_longer_carries_cleanup_fields(self) -> None:
        html = self._render(open_finance_settings="payment", can_pending_orders=False)
        self.assertNotIn("s_pending_order_ttl_hours", html)
        self.assertNotIn("cancel-pending-orders", html)
        self.assertNotIn('data-modal-tab="cleanup"', html)

    def test_cleanup_is_the_last_tab_after_payg(self) -> None:
        from app.services.users import SETTING_GROUPS, TAB_SETTING_GROUPS

        html = self._render(
            can_billing_settings=True,
            billing_tab_groups=TAB_SETTING_GROUPS["billing"],
            billing_groups={n: SETTING_GROUPS[n] for n in TAB_SETTING_GROUPS["billing"]},
            billing_settings_action="/settings?tab=billing",
        )
        tabs = [html.index(f'data-modal-tab="{name}"') for name in ("payment", "billing", "cleanup")]
        self.assertEqual(tabs, sorted(tabs))
        panels = [html.index(f'data-modal-tab-panel="{name}"') for name in ("payment", "billing", "cleanup")]
        self.assertEqual(panels, sorted(panels))
