"""Legacy soft-cancel retirement: disable without enabling purge, and the cleanup tab UI."""

from __future__ import annotations

import unittest

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, BotUser
from app.services.stale_order_cleanup import (
    KEY_RETIRED_NOTICE,
    disable_all_legacy_soft_cancel,
    disable_legacy_soft_cancel,
    soft_cancel_retired_notice_on,
)
from app.services.users import _SETTINGS_CACHE, get_all_settings, set_settings_bulk


class SoftCancelRetirementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add(BotUser(id=2, telegram_id=102, referral_code="shop", role="reseller"))
        await self.session.commit()
        _SETTINGS_CACHE.clear()

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.engine.dispose()

    async def test_disable_soft_cancel_sets_notice_without_enabling_purge(self) -> None:
        from app.services.order_purge import KEY_AUTO, KEY_TAGS

        await set_settings_bulk(
            self.session,
            {"pending_order_cleanup_enabled": "1", "pending_order_ttl_hours": "48"},
            reseller_id=2,
        )
        self.assertTrue(await disable_legacy_soft_cancel(self.session, 2))
        values = await get_all_settings(self.session, reseller_id=2)
        self.assertEqual(values["pending_order_cleanup_enabled"], "0")
        self.assertTrue(soft_cancel_retired_notice_on(values))
        self.assertEqual(values[KEY_RETIRED_NOTICE], "1")
        self.assertNotEqual(values.get(KEY_AUTO), "1")
        self.assertFalse(bool(str(values.get(KEY_TAGS) or "").strip()))
        self.assertFalse(await disable_legacy_soft_cancel(self.session, 2))

    async def test_disable_all_pages_past_the_first_batch(self) -> None:
        from app.services import stale_order_cleanup as soc

        original = soc.SHOP_SCAN_PAGE
        soc.SHOP_SCAN_PAGE = 2
        try:
            for shop_id in (10, 11, 12):
                self.session.add(
                    BotUser(id=shop_id, telegram_id=shop_id * 10, referral_code=f"s{shop_id}", role="reseller")
                )
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
                self.assertTrue(soft_cancel_retired_notice_on(values), shop_id)
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
            "soft_cancel_retired_notice": False,
            "soft_cancel_retired_ack_action": "",
            "purge": purge,
            "app_version": "t",
        }
        ctx.update(overrides)
        return templates.env.get_template("_modal_finance_settings.html").render(**ctx)

    def test_cleanup_tab_is_tags_age_auto_and_run_now(self) -> None:
        html = self._render()
        self.assertIn('data-modal-tab="cleanup"', html)
        self.assertIn("تگ‌های حذف", html)
        self.assertIn("حذف از پایگاه الان", html)
        self.assertIn("/finance/cleanup/purge-run", html)
        self.assertNotIn("cancel-pending-orders", html)
        self.assertNotIn("s_pending_order_ttl_hours", html)

    def test_retired_soft_cancel_banner_is_shown_until_acked(self) -> None:
        html = self._render(
            soft_cancel_retired_notice=True,
            soft_cancel_retired_ack_action="/finance/cleanup/ack-retired",
        )
        self.assertIn("لغو خودکار سفارش‌های معلق خاموش شد", html)
        self.assertIn("/finance/cleanup/ack-retired", html)
        self.assertIn("متوجه شدم", html)

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


if __name__ == "__main__":
    unittest.main()
