"""«نام دلخواه سرویس» on the web: the main-bot switch is on the owner's naming tab and never on a shop's."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.services.custom_service_name import SETTING_KEY, name_step_enabled
from app.services.panel_nav import NavViewer
from app.services.panel_search import panel_search
from app.services.users import clear_settings_cache, get_all_settings
from app.services.ux20 import shop_bundle_allowed_setting_keys
from tests.test_update_owner_only import OWNER_SESSION, SHOP_SESSION, RoutesHarness

FIELD = f"s_{SETTING_KEY}"
FIELD_LABEL = "نام دلخواه سرویس هنگام خرید"
SHOP_ID = 5


class CustomServiceNameHarness(RoutesHarness):
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

    async def step_enabled(self, shop_id: int | None) -> bool:
        clear_settings_cache()
        async with self.sessions() as session:
            return await name_step_enabled(session, shop_id)


class OwnerNamingTabTests(CustomServiceNameHarness):
    async def test_the_owner_sees_the_switch_on_the_naming_tab(self) -> None:
        response = await self.call(OWNER_SESSION, "GET", "/settings?tab=naming")
        self.assertEqual(response.status_code, 200)
        self.assertIn(f'name="{FIELD}"', response.text)
        self.assertIn(FIELD_LABEL, response.text)

    async def test_turning_the_switch_on_and_off_drives_the_main_bot_name_step(self) -> None:
        self.assertFalse(await self.step_enabled(None))
        response = await self.form(OWNER_SESSION, "/settings?tab=naming", {FIELD: "1"})
        self.assertEqual(response.status_code, 303)
        self.assertEqual((await self.stored(None))[SETTING_KEY], "1")
        self.assertTrue(await self.step_enabled(None))

        response = await self.form(OWNER_SESSION, "/settings?tab=naming", {"s_pg_username_prefix": "clk"})
        self.assertEqual(response.status_code, 303)
        self.assertEqual((await self.stored(None))[SETTING_KEY], "0")
        self.assertFalse(await self.step_enabled(None))


class ShopNamingTabTests(CustomServiceNameHarness):
    async def test_a_shop_owner_never_sees_the_main_bot_switch(self) -> None:
        response = await self.call(SHOP_SESSION, "GET", "/shop-settings?tab=naming")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(FIELD, response.text)
        self.assertNotIn(FIELD_LABEL, response.text)

    async def test_a_forged_shop_save_neither_stores_the_switch_nor_opens_the_name_step(self) -> None:
        response = await self.form(SHOP_SESSION, "/shop-settings?tab=naming", {FIELD: "1"})
        self.assertEqual(response.status_code, 303)
        self.assertNotEqual((await self.stored(SHOP_ID)).get(SETTING_KEY), "1")
        self.assertNotEqual((await self.stored(None)).get(SETTING_KEY), "1")
        self.assertFalse(await self.step_enabled(SHOP_ID))

    async def test_a_shop_settings_import_may_not_write_the_switch(self) -> None:
        self.assertNotIn(SETTING_KEY, shop_bundle_allowed_setting_keys())


class PanelSearchTests(CustomServiceNameHarness):
    def _viewer(self, *, is_owner: bool) -> NavViewer:
        return NavViewer(
            role="admin" if is_owner else "reseller",
            perms=frozenset() if is_owner else frozenset({"shop_settings", "dashboard"}),
            pg_perms=frozenset(),
            is_owner=is_owner,
            can_add_representative=False,
            can_manage_representatives=is_owner,
        )

    async def test_search_finds_the_switch_for_the_owner_only(self) -> None:
        owner_hits = panel_search(self._viewer(is_owner=True), FIELD_LABEL)
        self.assertTrue(any(hit.label == FIELD_LABEL for hit in owner_hits))
        shop_hits = panel_search(self._viewer(is_owner=False), FIELD_LABEL)
        self.assertFalse(any(hit.label == FIELD_LABEL for hit in shop_hits))
