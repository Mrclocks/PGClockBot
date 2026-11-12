"""The «افزودن سرویس قبلی» settings tab: who sees what, what a save may store, the status and link-test endpoints."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy import select, update

from app.db.models import BotUser, ResellerProfile, ServiceImportAttempt
from app.services import pg_staff_access
from app.services import service_import as si
from app.services import service_import_config as cfg
from app.services import service_import_admin
from app.services.service_import_admin import import_scope
from app.services.users import clear_settings_cache, get_all_settings
from tests.test_service_import import PLATFORM_ADMIN, SHOP_ADMIN, FakePanel, _panel_user
from tests.test_update_owner_only import OWNER_SESSION, SHOP_SESSION, RoutesHarness

LIMITED_SHOP = {"role": "reseller", "bot_user_id": 5, "permissions": ["orders"], "pv": SHOP_SESSION["pv"]}


class TabHarness(RoutesHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        clear_settings_cache()
        si.clear_identity_cache()
        service_import_admin._test_calls.clear()
        Session = self._session_factory()
        async with Session() as session:
            session.add_all([
                BotUser(id=1, telegram_id=101, referral_code="u1", role="user", full_name="مشتری یک"),
                BotUser(id=6, telegram_id=106, referral_code="u6", role="user", reseller_id=5, full_name="مشتری شاپ"),
            ])
            await session.execute(update(ResellerProfile).where(ResellerProfile.user_id == 5).values(pg_admin_username=SHOP_ADMIN))
            session.add_all([
                ServiceImportAttempt(bot_user_id=1, token_hash="h1", outcome="refused", reason="not_found"),
                ServiceImportAttempt(bot_user_id=6, token_hash="h6", outcome="refused", reason="admin_not_allowed"),
            ])
            await session.commit()
        self.users = {
            "tok-plat-0000001": _panel_user(11, "plat-one", PLATFORM_ADMIN),
            "tok-other-000001": _panel_user(12, "other-one", "other-admin"),
            "tok-shop-0000001": _panel_user(21, "shop-one", SHOP_ADMIN),
        }
        self.platform_panel = FakePanel(self.users, signed_in_as=PLATFORM_ADMIN)
        self.shop_panel = FakePanel(self.users, visible_admin=SHOP_ADMIN, signed_in_as=SHOP_ADMIN)
        self.panel_settings = SimpleNamespace(pg_username=PLATFORM_ADMIN, pg_subscription_path="/sub")

        async def for_reseller(_session, _shop_id):
            return self.shop_panel

        async def gate_ok(_username):
            return "ok", {"username": SHOP_ADMIN, "status": "active"}

        for module, target, value in (
            (pg_staff_access, "fetch_pg_admin_gate", gate_ok),
            (si, "get_pg", lambda: self.platform_panel),
            (si, "get_pg_for_reseller", for_reseller),
            (si, "get_settings", lambda: self.panel_settings),
            (cfg, "get_settings", lambda: self.panel_settings),
        ):
            patcher = patch.object(module, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _session_factory(self):
        from sqlalchemy.ext.asyncio import async_sessionmaker

        return async_sessionmaker(self.engine, expire_on_commit=False)

    async def form(self, payload: dict, path: str, data: dict[str, str]):
        cookie = f"session={self.signer.dumps(payload)}; csrf=token"
        headers = {"cookie": cookie, "x-csrf-token": "token", "origin": "http://panel.test"}
        return await self.client.post(path, headers=headers, data=data)

    async def stored(self, shop_id: int | None) -> dict[str, str]:
        clear_settings_cache()
        async with self._session_factory()() as session:
            return await get_all_settings(session, reseller_id=shop_id)


class TabPageTests(TabHarness):
    async def test_the_owner_sees_every_setting_the_status_the_test_and_the_script(self):
        response = await self.call(OWNER_SESSION, "GET", "/settings?tab=service_import")
        self.assertEqual(response.status_code, 200)
        for needle in ("s_service_import_enabled", "s_service_import_max_per_user", "s_service_import_paths",
                       "s_service_import_admins", 'id="si-panel"', "/static/service-import-admin.js", "بررسی لینک"):
            self.assertIn(needle, response.text)

    async def test_a_shop_owner_sees_the_shared_settings_but_never_the_panel_wide_ones(self):
        response = await self.call(SHOP_SESSION, "GET", "/shop-settings?tab=service_import")
        self.assertEqual(response.status_code, 200)
        for needle in ("s_service_import_enabled", "s_service_import_max_per_user", 'id="si-panel"'):
            self.assertIn(needle, response.text)
        for needle in ("s_service_import_paths", "s_service_import_admins"):
            self.assertNotIn(needle, response.text)

    async def test_the_tab_lives_in_the_service_and_sales_group_for_both(self):
        owner = await self.call(OWNER_SESSION, "GET", "/settings?tab=service_import")
        shop = await self.call(SHOP_SESSION, "GET", "/shop-settings?tab=service_import")
        for response, base in ((owner, "/settings"), (shop, "/shop-settings")):
            head = response.text.split("settings-layout", 1)[0]
            self.assertIn(f'href="{base}?tab=service_import" class="active"', head)
            self.assertIn(f'href="{base}?tab=delivery"', head)


class SavePlumbingTests(TabHarness):
    async def test_the_owner_saves_normalised_values(self):
        response = await self.form(OWNER_SESSION, "/settings?tab=service_import", {
            "s_service_import_enabled": "1", "s_service_import_max_per_user": "99",
            "s_service_import_paths": "sub, hamid/ https://evil.example/x ../x", "s_service_import_admins": "Admin1, bad name!",
        })
        self.assertEqual(response.status_code, 303)
        values = await self.stored(None)
        self.assertEqual(values["service_import_enabled"], "1")
        self.assertEqual(values["service_import_max_per_user"], "20")
        self.assertEqual(values["service_import_paths"], "/sub\n/hamid")
        self.assertEqual(values["service_import_admins"], "admin1\nbad")

    async def test_a_shop_cannot_store_the_panel_wide_lists_even_by_posting_them(self):
        response = await self.form(SHOP_SESSION, "/shop-settings?tab=service_import", {
            "s_service_import_enabled": "1", "s_service_import_max_per_user": "3",
            "s_service_import_paths": "/evil", "s_service_import_admins": "platformadmin",
        })
        self.assertEqual(response.status_code, 303)
        shop = await self.stored(5)
        self.assertEqual((shop["service_import_enabled"], shop["service_import_max_per_user"]), ("1", "3"))
        self.assertEqual((shop["service_import_paths"], shop["service_import_admins"]), ("", ""))
        platform = await self.stored(None)
        self.assertEqual(platform["service_import_enabled"], "1")
        self.assertEqual((platform["service_import_paths"], platform["service_import_admins"]), ("", ""))

    async def test_a_shops_own_values_never_change_the_platforms(self):
        await self.form(SHOP_SESSION, "/shop-settings?tab=service_import", {"s_service_import_max_per_user": "2"})
        self.assertEqual((await self.stored(None))["service_import_max_per_user"], "5")


class StatusEndpointTests(TabHarness):
    async def test_the_owner_sees_the_login_the_signed_in_admin_and_the_effective_lists(self):
        async with self._session_factory()() as session:
            from app.services.users import set_settings_bulk

            await set_settings_bulk(session, {"service_import_paths": "/hamid", "service_import_admins": "other-admin"})
        body = (await self.call(OWNER_SESSION, "GET", "/service-import/status")).json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["login"], "password")
        self.assertEqual(body["signed_in_as"], PLATFORM_ADMIN)
        self.assertEqual(body["admins"], sorted({PLATFORM_ADMIN, "other-admin"}))
        self.assertEqual(body["paths"], ["/sub", "/hamid"])

    async def test_an_api_key_login_is_reported_as_such(self):
        self.platform_panel.uses_api_key = True
        body = (await self.call(OWNER_SESSION, "GET", "/service-import/status")).json()
        self.assertEqual(body["login"], "api_key")

    async def test_a_shop_owner_sees_only_its_own_admin_never_the_platforms(self):
        body = (await self.call(SHOP_SESSION, "GET", "/service-import/status")).json()
        self.assertEqual(body["admins"], [SHOP_ADMIN])
        self.assertNotIn("signed_in_as", body)
        self.assertNotIn(PLATFORM_ADMIN, str(body))

    async def test_recent_tries_are_scoped_to_the_viewers_shop_and_carry_no_token(self):
        owner = (await self.call(OWNER_SESSION, "GET", "/service-import/status")).json()["attempts"]
        shop = (await self.call(SHOP_SESSION, "GET", "/service-import/status")).json()["attempts"]
        self.assertEqual([a["customer"] for a in owner], [101])
        self.assertEqual([a["customer"] for a in shop], [106])
        self.assertNotIn("h1", str(shop) + str(owner))
        self.assertNotIn("token", str(owner).lower())

    async def test_a_panel_that_cannot_be_reached_is_reported_without_details(self):
        self.platform_panel.fail_with = si.PasarGuardError("boom https://secret-host/x", status_code=500)
        body = (await self.call(OWNER_SESSION, "GET", "/service-import/status")).json()
        self.assertNotIn("secret-host", str(body))

    async def test_nobody_else_reaches_it(self):
        response = await self.client.get("/service-import/status")
        self.assertIn(response.status_code, {303, 401, 403})
        for payload in ({"role": "principal", "org_principal_id": 2, "org_depth": 1}, {"role": "pg_staff", "username": "x"}):
            response = await self.call(payload, "GET", "/service-import/status")
            self.assertEqual(response.status_code, 303, payload["role"])

    def test_scope_rules(self):
        self.assertIsNone(import_scope(OWNER_SESSION))
        self.assertEqual(import_scope({"role": "reseller", "permissions": ["shop_settings"], "bot_user_id": 5}), 5)
        for staff in (
            {"role": "reseller", "permissions": ["orders"], "bot_user_id": 5},
            {"role": "reseller", "permissions": ["shop_settings"], "bot_user_id": None},
            {"role": "admin", "permissions": []},
            {"role": "pg_staff", "permissions": ["shop_settings"]},
        ):
            with self.subTest(staff=staff), self.assertRaises(PermissionError):
                import_scope(staff)


class LinkTestEndpointTests(TabHarness):
    async def test_an_acceptable_link_is_reported_with_its_path_and_nothing_is_attached(self):
        body = (await self.form(OWNER_SESSION, "/service-import/test", {"link": "https://p.example/sub/tok-plat-0000001"})).json()
        self.assertEqual((body["ok"], body["path"], body["panel_username"]), (True, "/sub", "plat-one"))
        from app.db.models import UserService

        async with self._session_factory()() as session:
            self.assertEqual((await session.scalars(select(UserService))).all(), [])
            self.assertEqual(len((await session.scalars(select(ServiceImportAttempt))).all()), 2)

    async def test_a_refusal_names_its_reason_and_the_owner_of_the_service_for_the_platform_owner(self):
        body = (await self.form(OWNER_SESSION, "/service-import/test", {"link": "tok-other-000001"})).json()
        self.assertEqual((body["ok"], body["reason"], body["owner_admin"]), (False, "admin_not_allowed", "other-admin"))
        self.assertTrue(body["label"])

    async def test_a_path_problem_is_distinguished_from_a_missing_service(self):
        path = (await self.form(OWNER_SESSION, "/service-import/test", {"link": "https://p.example/hamid/tok-plat-0000001"})).json()
        missing = (await self.form(OWNER_SESSION, "/service-import/test", {"link": "tok-nope-000000009"})).json()
        invalid = (await self.form(OWNER_SESSION, "/service-import/test", {"link": "not a link"})).json()
        self.assertEqual((path["reason"], missing["reason"], invalid["reason"]), ("path_not_allowed", "not_found", "invalid_link"))

    async def test_a_shop_owner_tests_only_its_own_services_and_never_learns_other_admins(self):
        own = (await self.form(SHOP_SESSION, "/service-import/test", {"link": "tok-shop-0000001"})).json()
        self.assertTrue(own["ok"])
        self.assertNotIn("panel_username", own)
        foreign = (await self.form(SHOP_SESSION, "/service-import/test", {"link": "tok-other-000001"})).json()
        self.assertFalse(foreign["ok"])
        self.assertNotIn("owner_admin", foreign)
        self.assertNotIn("other-admin", str(foreign))

    async def test_the_link_is_never_echoed_or_logged(self):
        secret = "tok-plat-0000001"
        with patch.object(si, "logger") as log:
            response = await self.form(OWNER_SESSION, "/service-import/test", {"link": f"https://p.example/sub/{secret}"})
        self.assertNotIn(secret, response.text)
        self.assertNotIn(secret, str(log.mock_calls))

    async def test_testing_is_rate_limited_per_shop(self):
        statuses = []
        for _ in range(23):
            statuses.append((await self.form(OWNER_SESSION, "/service-import/test", {"link": "tok-nope-000000009"})).json().get("reason"))
        self.assertEqual(statuses[:20].count("rate_limited"), 0)
        self.assertEqual(statuses[20:], ["rate_limited"] * 3)

    async def test_nobody_without_access_can_test(self):
        response = await self.form({"role": "principal", "org_principal_id": 2, "org_depth": 1}, "/service-import/test", {"link": "x"})
        self.assertEqual(response.status_code, 303)


if __name__ == "__main__":
    unittest.main()
