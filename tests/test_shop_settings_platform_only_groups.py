"""A shop's web settings never offer, save or import fields that only the platform Owner can use.

The panel link watchdog and its alerts run once for the one PasarGuard panel, and only the main
bot's Owner configures connection monitoring, so a shop value for these settings would change
nothing while looking like it does.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import ResellerSetting
from app.services.users import clear_settings_cache, get_all_settings
from app.services.ux20 import shop_bundle_allowed_setting_keys
from tests.test_update_owner_only import OWNER_SESSION, SHOP_SESSION, RoutesHarness

SHOP_ID = 5
PG_LINK_KEYS = ("pg_link_watchdog_enabled", "pg_link_alerts_enabled")
OPERATIONS_KEYS = ("action_center_expire_days", "one_tap_renew_enabled", "capacity_warn_pct", "funnel_tracking_enabled")
MONITOR_ADMIN_KEYS = (
    "connection_status_admin_title",
    "connection_status_account_prompt",
    "connection_status_name_prompt",
    "connection_status_order_prompt",
)


class PlatformOnlyGroupsHarness(RoutesHarness):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        clear_settings_cache()
        self.addCleanup(clear_settings_cache)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def form(self, payload: dict, path: str, data: dict[str, str]):
        cookie = f"session={self.signer.dumps(payload)}; csrf=token"
        headers = {"cookie": cookie, "x-csrf-token": "token", "origin": "http://panel.test"}
        return await self.client.post(path, headers=headers, data=data)

    async def stored(self, shop_id: int | None) -> dict[str, str]:
        clear_settings_cache()
        async with self.sessions() as session:
            return await get_all_settings(session, reseller_id=shop_id)

    async def shop_rows(self, keys: tuple[str, ...]) -> dict[str, str]:
        """The shop's own stored rows (defaults and platform values are not counted)."""
        async with self.sessions() as session:
            rows = await session.execute(
                select(ResellerSetting.key, ResellerSetting.value).where(
                    ResellerSetting.reseller_user_id == SHOP_ID, ResellerSetting.key.in_(keys)
                )
            )
            return dict(rows.all())


class ShopNotificationsTabTests(PlatformOnlyGroupsHarness):
    async def test_shop_page_does_not_offer_panel_link_switches(self) -> None:
        response = await self.call(SHOP_SESSION, "GET", "/shop-settings?tab=notifications")
        self.assertEqual(response.status_code, 200)
        for key in PG_LINK_KEYS:
            self.assertNotIn(f'name="s_{key}"', response.text)
        # The shop's own customer alert settings stay.
        self.assertIn('name="s_user_alert_low_enabled"', response.text)

    async def test_forged_shop_save_does_not_store_panel_link_switches(self) -> None:
        data = {f"s_{key}": "1" for key in PG_LINK_KEYS}
        response = await self.form(SHOP_SESSION, "/shop-settings?tab=notifications", data)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(await self.shop_rows(PG_LINK_KEYS), {})

    async def test_saving_shop_alerts_does_not_switch_off_hidden_operations_toggles(self) -> None:
        # The page shows only the shop's alerts; the save used to store "0" for the unseen toggles.
        response = await self.form(
            SHOP_SESSION, "/shop-settings?tab=notifications", {"s_user_alert_low_enabled": "1"}
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(await self.shop_rows(OPERATIONS_KEYS), {})
        self.assertEqual((await self.shop_rows(("user_alert_low_enabled",))), {"user_alert_low_enabled": "1"})
        shop = await self.stored(SHOP_ID)
        self.assertEqual(shop["one_tap_renew_enabled"], "1")
        self.assertEqual(shop["funnel_tracking_enabled"], "1")

    async def test_shop_page_does_not_offer_operations_settings(self) -> None:
        response = await self.call(SHOP_SESSION, "GET", "/shop-settings?tab=notifications")
        for key in OPERATIONS_KEYS:
            self.assertNotIn(f'name="s_{key}"', response.text)

    async def test_owner_still_edits_panel_link_switches(self) -> None:
        response = await self.call(OWNER_SESSION, "GET", "/settings?tab=notifications")
        self.assertEqual(response.status_code, 200)
        for key in PG_LINK_KEYS:
            self.assertIn(f'name="s_{key}"', response.text)


class ShopMessagesTabTests(PlatformOnlyGroupsHarness):
    async def test_shop_page_shows_customer_status_texts_but_not_monitor_admin_prompts(self) -> None:
        response = await self.call(SHOP_SESSION, "GET", "/shop-settings?tab=messages")
        self.assertEqual(response.status_code, 200)
        self.assertIn('name="s_connection_status_title"', response.text)
        for key in MONITOR_ADMIN_KEYS:
            self.assertNotIn(f'name="s_{key}"', response.text)

    async def test_forged_shop_save_does_not_store_monitor_admin_prompts(self) -> None:
        data = {f"s_{key}": "<b>forged</b>" for key in MONITOR_ADMIN_KEYS}
        response = await self.form(SHOP_SESSION, "/shop-settings?tab=messages", data)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(await self.shop_rows(MONITOR_ADMIN_KEYS), {})

    async def test_owner_still_edits_monitor_admin_prompts(self) -> None:
        response = await self.call(OWNER_SESSION, "GET", "/settings?tab=messages")
        self.assertEqual(response.status_code, 200)
        for key in MONITOR_ADMIN_KEYS:
            self.assertIn(f'name="s_{key}"', response.text)

    async def test_owner_save_of_monitor_admin_prompt_is_stored(self) -> None:
        response = await self.form(
            OWNER_SESSION, "/settings?tab=messages", {"s_connection_status_name_prompt": "نام هاست؟"}
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual((await self.stored(None))["connection_status_name_prompt"], "نام هاست؟")


class ShopImportTests(PlatformOnlyGroupsHarness):
    async def test_shop_settings_import_may_not_write_platform_only_fields(self) -> None:
        allowed = shop_bundle_allowed_setting_keys()
        for key in (*PG_LINK_KEYS, *MONITOR_ADMIN_KEYS, *OPERATIONS_KEYS):
            self.assertNotIn(key, allowed)
