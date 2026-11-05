"""Legacy soft-cancel policy, disable migration, and cleanup-tab template shape.

Soft-cancel no longer has a scheduler job or panel routes; ``disable_all_legacy_soft_cancel``
turns the enabled flag off without enabling permanent tag purge.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, BotUser, Order, OrderStatus
from app.services.orders import cancel_stale_pending_orders
from app.services.stale_order_cleanup import (
    StaleOrderPolicy,
    cleanup_status,
    count_stale_orders,
    disable_all_legacy_soft_cancel,
    disable_legacy_soft_cancel,
    policy_from_settings,
    record_cleanup_run,
)
from app.services.users import _SETTINGS_CACHE, get_all_settings, set_settings_bulk


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
        unpaid = await self._order(OrderStatus.PENDING.value, 30)
        young_review = await self._order(OrderStatus.AWAITING_APPROVAL.value, 30)
        old_review = await self._order(OrderStatus.AWAITING_APPROVAL.value, 80)
        paid = await self._order(OrderStatus.PAID.value, 500)
        cancelled = await cancel_stale_pending_orders(
            self.session,
            older_than_hours=24,
            include_awaiting_approval=True,
            approval_older_than_hours=72,
        )
        self.assertEqual(cancelled, 2)
        self.assertEqual(await self._status(unpaid), "cancelled")
        self.assertEqual(await self._status(old_review), "cancelled")
        self.assertEqual(await self._status(young_review), "awaiting_approval")
        self.assertEqual(await self._status(paid), "paid")

    async def test_awaiting_approval_is_kept_when_not_included(self) -> None:
        review = await self._order(OrderStatus.AWAITING_APPROVAL.value, 500)
        self.assertEqual(await cancel_stale_pending_orders(self.session, older_than_hours=24), 0)
        self.assertEqual(await self._status(review), "awaiting_approval")

    async def test_preview_count_matches_what_a_run_cancels_and_stays_in_its_shop(self) -> None:
        await self._order(OrderStatus.PENDING.value, 100)
        await self._order(OrderStatus.PENDING.value, 100, shop=2)
        await self._order(OrderStatus.PENDING.value, 1)
        policy = StaleOrderPolicy(48)
        self.assertEqual(await count_stale_orders(self.session, policy, reseller_id=None), 1)
        self.assertEqual(await count_stale_orders(self.session, policy, reseller_id=2), 1)
        self.assertEqual(
            await cancel_stale_pending_orders(self.session, older_than_hours=48, reseller_id=2), 1
        )
        platform = await cleanup_status(self.session, {}, reseller_id=None)
        self.assertEqual(platform["ready_count"], 1)
        self.assertEqual(platform["last_run"], "")

    async def test_run_is_recorded_for_the_shop_that_ran(self) -> None:
        await self._order(OrderStatus.PENDING.value, 100, shop=2)
        n = await cancel_stale_pending_orders(self.session, older_than_hours=48, reseller_id=2)
        await record_cleanup_run(self.session, cancelled=n, reseller_id=2)
        shop = await cleanup_status(self.session, await get_all_settings(self.session, reseller_id=2), reseller_id=2)
        self.assertEqual(shop["last_count"], 1)
        self.assertRegex(str(shop["last_run"]), r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
        platform = await cleanup_status(self.session, await get_all_settings(self.session), reseller_id=None)
        self.assertEqual(platform["last_run"], "")

    async def test_disable_soft_cancel_does_not_enable_purge(self) -> None:
        from app.services.order_purge import KEY_AUTO, KEY_TAGS

        await set_settings_bulk(
            self.session,
            {"pending_order_cleanup_enabled": "1", "pending_order_ttl_hours": "48"},
            reseller_id=2,
        )
        self.assertTrue(await disable_legacy_soft_cancel(self.session, 2))
        values = await get_all_settings(self.session, reseller_id=2)
        self.assertEqual(values["pending_order_cleanup_enabled"], "0")
        self.assertNotEqual(values.get(KEY_AUTO), "1")
        self.assertFalse(bool(str(values.get(KEY_TAGS) or "").strip()))
        self.assertFalse(await disable_legacy_soft_cancel(self.session, 2))

    async def test_disable_all_pages_past_the_first_batch(self) -> None:
        from app.services import stale_order_cleanup as soc

        original = soc.SHOP_SCAN_PAGE
        soc.SHOP_SCAN_PAGE = 2
        try:
            for shop_id in (10, 11, 12):
                self.session.add(BotUser(id=shop_id, telegram_id=shop_id * 10, referral_code=f"s{shop_id}", role="reseller"))
            await self.session.commit()
            for shop_id in (10, 11, 12):
                await set_settings_bulk(
                    self.session, {"pending_order_cleanup_enabled": "1"}, reseller_id=shop_id
                )
            await set_settings_bulk(self.session, {"pending_order_cleanup_enabled": "1"}, reseller_id=None)
            self.assertEqual(await disable_all_legacy_soft_cancel(self.session), 4)
            for shop_id in (None, 10, 11, 12):
                values = await get_all_settings(self.session, reseller_id=shop_id)
                self.assertEqual(values["pending_order_cleanup_enabled"], "0", shop_id)
        finally:
            soc.SHOP_SCAN_PAGE = original


class CleanupTabRenderTests(unittest.TestCase):
    def _render(self, **overrides: object) -> str:
        from app.api.app import templates
        from app.services.users import DEFAULT_SETTINGS, SETTING_GROUPS, TAB_SETTING_GROUPS

        def groups(tab: str) -> dict:
            return {name: SETTING_GROUPS[name] for name in TAB_SETTING_GROUPS[tab]}

        purge = {
            "sections": (),
            "days": 30,
            "min_days": 1,
            "max_days": 3650,
            "ready": type("R", (), {"total": 4, "orders": 3, "payments": 1})(),
            "auto_on": True,
            "last_run": {"at": "2026-10-28 12:00", "kind": "دستی", "orders": 2, "payments": 1},
            "rules_action": "/finance/cleanup/save",
            "run_action": "/finance/cleanup/purge-run",
        }
        ctx = {
            "open_finance_settings": "cleanup",
            "can_pending_orders": True,
            "can_billing_settings": False,
            "values": dict(DEFAULT_SETTINGS),
            "payment_tab_groups": TAB_SETTING_GROUPS["payment"],
            "payment_groups": groups("payment"),
            "payment_settings_action": "/settings?tab=payment",
            "cleanup_settings_action": "/finance/cleanup/save",
            "purge": purge,
            "app_version": "t",
        }
        ctx.update(overrides)
        return templates.env.get_template("_modal_finance_settings.html").render(**ctx)

    def test_cleanup_tab_is_tags_age_auto_and_run_now(self) -> None:
        html = self._render()
        self.assertIn('data-modal-tab="cleanup"', html)
        self.assertIn("تگ‌های حذف", html)
        self.assertIn("زمان‌بندی", html)
        self.assertIn("حذف از پایگاه الان", html)
        self.assertIn("برای همیشه از پایگاه داده حذف", html)
        self.assertIn("/finance/cleanup/purge-run", html)
        self.assertIn("/finance/cleanup/save", html)
        self.assertNotIn("cancel-pending-orders", html)
        self.assertNotIn("s_pending_order_ttl_hours", html)
        self.assertEqual(html.count("وضعیت پاکسازی"), 1)

    def test_payment_tab_no_longer_carries_cleanup_fields(self) -> None:
        html = self._render(open_finance_settings="payment", can_pending_orders=False, purge=None)
        self.assertNotIn("s_pending_order_ttl_hours", html)
        self.assertNotIn("cancel-pending-orders", html)
        self.assertNotIn('data-modal-tab="cleanup"', html)
        self.assertNotIn("تگ‌های حذف", html)

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


if __name__ == "__main__":
    unittest.main()
