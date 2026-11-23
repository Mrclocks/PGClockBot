"""Staff attach a customer's existing panel service from the edit screen: checks, isolation, idempotency."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from urllib.parse import unquote


from sqlalchemy import select, update

from app.db.models import BotUser, CustomerNotice, ResellerProfile, ServiceImportRequest, ShopStaffAction, UserService
from app.services import service_import_admin
from app.services.pasarguard import PasarGuardError
from app.services.service_import import ImportOutcome
from app.services.service_import_admin import TEST_BUDGET
from app.services.service_import_assign import (
    BAD_KIND,
    BAD_PREVIEW,
    BAD_USERNAME,
    CHANGED,
    NO_PERMISSION,
    STAFF_ACTION,
    TOO_MANY_LOOKUPS,
    AssignRefused,
    AssignRequest,
    ServiceLookup,
    assign_existing_service,
    may_assign_existing_services,
    parse_lookup,
    parse_panel_user_id,
    preview_existing_service,
)
from app.services.service_import_store import REQUEST_TTL
from app.services.shop_scope import ShopScopeError
from app.services.users import clear_settings_cache, set_settings_bulk
from tests.test_service_import import ImportCase
from tests.test_service_import_settings_tab import TabHarness
from tests.test_update_owner_only import OWNER_SESSION, SHOP_SESSION

OWNER = {
    "role": "admin", "username": "owner", "org_principal_id": 1, "org_depth": 0,
    "org_parent_id": None, "org_status": "active",
}
SHOP = {"role": "reseller", "bot_user_id": 5, "username": "shop", "permissions": ["customer_services", "service_import_assign"]}
OTHER_SHOP = {**SHOP, "bot_user_id": 9}
PLAT_LINK = "https://p.example/sub/tok-plat-0000001"
SHOP_LINK = "https://p.example/sub/tok-shop-0000001"
REASON = "مشتری لینکش را در پشتیبانی فرستاد"


def link(value: str = PLAT_LINK) -> ServiceLookup:
    return ServiceLookup("link", value)


class AssignCase(ImportCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        service_import_admin._test_calls.clear()
        self.addCleanup(service_import_admin._test_calls.clear)
        async with self.Session() as session:
            session.add(BotUser(id=9, telegram_id=109, referral_code="shop9", role="reseller"))
            session.add(BotUser(id=10, telegram_id=110, referral_code="u10", role="user", reseller_id=9))
            await session.commit()

    async def preview(self, user_id: int, lookup: ServiceLookup = link(), staff: dict = OWNER):
        async with self.Session() as session:
            user = await session.get(BotUser, user_id)
            return await preview_existing_service(session, staff=staff, user=user, lookup=lookup)

    async def assign(
        self, user_id: int, lookup: ServiceLookup = link(), *, staff: dict = OWNER, expected: int = 11,
        notify: bool = False, reason: str = REASON,
    ):
        request = AssignRequest(lookup=lookup, expected_panel_user_id=expected, reason=reason, notify_customer=notify)
        async with self.Session() as session:
            user = await session.get(BotUser, user_id)
            return await assign_existing_service(session, staff=staff, user=user, request=request)

    async def log_rows(self) -> list[ShopStaffAction]:
        return await self.rows(ShopStaffAction)


class AttachTests(AssignCase):
    async def test_the_owner_attaches_a_platform_service_while_customer_import_is_off(self):
        result = await self.assign(1)
        self.assertEqual(result.outcome, ImportOutcome.ADDED)
        (row,) = await self.rows()
        self.assertEqual((row.id, row.bot_user_id, row.pg_user_id, row.remark, row.shop_id), (result.service_id, 1, 11, "imported", None))
        self.assertEqual(row.subscription_token, "tok-plat-0000001")
        (log,) = await self.log_rows()
        self.assertEqual((log.action, log.target_type, log.target_id, log.shop_id, log.reason), (STAFF_ACTION, "service", row.id, None, REASON))

    async def test_a_panel_username_attaches_the_same_service(self):
        result = await self.assign(1, ServiceLookup("username", "plat-one"))
        self.assertEqual((result.outcome, result.panel_username), (ImportOutcome.ADDED, "plat-one"))
        (row,) = await self.rows()
        self.assertEqual((row.pg_user_id, row.subscription_token), (11, "plat-one"))

    async def test_the_preview_shows_the_service_and_attaches_nothing(self):
        self.platform_users["tok-plat-0000001"].update(status="active", used_traffic=1024**3, data_limit=0)
        preview = await self.preview(1)
        self.assertEqual((preview.panel_user_id, preview.panel_username, preview.limit, preview.already_yours), (11, "plat-one", "نامحدود", False))
        self.assertNotIn("tok-plat", repr(preview.as_json()))
        self.assertEqual(await self.rows(), [])
        self.assertEqual(await self.log_rows(), [])

    async def test_a_shop_attaches_its_own_admins_service_to_its_own_customer(self):
        result = await self.assign(6, link(SHOP_LINK), staff=SHOP, expected=21)
        (row,) = await self.rows()
        self.assertEqual((row.bot_user_id, row.shop_id, result.shop_id), (6, 5, 5))
        (log,) = await self.log_rows()
        self.assertEqual(log.shop_id, 5)

    async def test_the_per_customer_cap_does_not_limit_staff(self):
        async with self.Session() as session:
            await set_settings_bulk(session, {"service_import_max_per_user": "1"})
        clear_settings_cache()
        await self.assign(1)
        self.platform_users["tok-plat-0000002"]["subscription_url"] = "https://p.example/sub/tok-plat-0000002"
        await self.assign(1, link("tok-plat-0000002"), expected=12)
        self.assertEqual(sorted(r.pg_user_id for r in await self.rows()), [11, 12])

    async def test_a_view_only_link_of_the_same_customer_becomes_renewable(self):
        async with self.Session() as session:
            session.add(UserService(bot_user_id=1, pg_user_id=11, pg_username="plat-one", subscription_url="u",
                                    subscription_token="tok-plat-0000001", remark="linked"))
            await session.commit()
        self.assertEqual((await self.assign(1)).outcome, ImportOutcome.UPGRADED)
        (row,) = await self.rows()
        self.assertEqual(row.remark, "imported")

    async def test_the_customers_waiting_request_for_it_is_closed(self):
        async with self.Session() as session:
            session.add(ServiceImportRequest(bot_user_id=1, panel_user_id=11, panel_username="plat-one",
                                             expires_at=datetime.now(timezone.utc) + REQUEST_TTL))
            session.add(ServiceImportRequest(bot_user_id=2, panel_user_id=11, panel_username="plat-one",
                                             expires_at=datetime.now(timezone.utc) + REQUEST_TTL))
            await session.commit()
        await self.assign(1)
        status = {r.bot_user_id: r.status for r in await self.rows(ServiceImportRequest)}
        self.assertEqual(status, {1: "approved", 2: "pending"})


class IdempotencyTests(AssignCase):
    async def test_repeating_returns_the_same_service_and_logs_once(self):
        first = await self.assign(1)
        second = await self.assign(1)
        self.assertEqual((second.outcome, second.service_id), (ImportOutcome.ALREADY_YOURS, first.service_id))
        self.assertFalse(second.is_new)
        self.assertEqual(len(await self.rows()), 1)
        self.assertEqual(len(await self.log_rows()), 1)

    async def test_the_customer_notice_is_queued_once_and_only_when_asked(self):
        await self.assign(1, notify=True)
        await self.assign(1, notify=True)
        (notice,) = await self.rows(CustomerNotice)
        self.assertEqual(
            (notice.bot_user_id, notice.telegram_id, notice.shop_id, notice.action, notice.service_name, notice.reason),
            (1, 101, None, STAFF_ACTION, "plat-one", REASON),
        )
        self.assertNotIn("tok-plat", repr(vars(notice)))

    async def test_without_the_notice_switch_nothing_is_queued(self):
        await self.assign(1)
        self.assertEqual(await self.rows(CustomerNotice), [])

    async def test_a_shop_customers_notice_goes_through_the_shop(self):
        await self.assign(6, link(SHOP_LINK), staff=SHOP, expected=21, notify=True)
        (notice,) = await self.rows(CustomerNotice)
        self.assertEqual(notice.shop_id, 5)

    async def test_simultaneous_clicks_attach_once(self):
        results = await asyncio.gather(self.assign(1), self.assign(1), return_exceptions=True)
        self.assertFalse([r for r in results if isinstance(r, BaseException)], results)
        self.assertEqual(len(await self.rows()), 1)
        self.assertEqual(len(await self.log_rows()), 1)

    async def test_simultaneous_claims_for_two_customers_attach_to_one(self):
        results = await asyncio.gather(self.assign(1), self.assign(2), return_exceptions=True)
        self.assertEqual(sum(1 for r in results if not isinstance(r, BaseException)), 1)
        refused = [r for r in results if isinstance(r, BaseException)]
        self.assertTrue(all(isinstance(r, AssignRefused) for r in refused), refused)
        self.assertEqual(len(await self.rows()), 1)

    async def test_the_preview_marks_a_service_already_in_the_list(self):
        await self.assign(1)
        self.assertTrue((await self.preview(1)).already_yours)


class RefusalTests(AssignCase):
    async def assert_refused(self, call, message: str | None = None) -> str:
        with self.assertRaises(AssignRefused) as caught:
            await call
        if message is not None:
            self.assertEqual(str(caught.exception), message)
        self.assertEqual(await self.rows(), [])
        self.assertEqual(await self.log_rows(), [])
        return str(caught.exception)

    async def test_a_service_of_another_customer_is_refused_without_naming_them(self):
        async with self.Session() as session:
            session.add(UserService(bot_user_id=2, pg_user_id=11, pg_username="plat-one", subscription_url="u",
                                    subscription_token="tok-plat-0000001", remark="imported"))
            await session.commit()
        for call in (self.preview(1), self.assign(1)):
            with self.assertRaises(AssignRefused) as caught:
                await call
            self.assertNotIn("102", str(caught.exception))
        self.assertEqual([r.bot_user_id for r in await self.rows()], [2])

    async def test_a_previewed_service_that_changed_is_not_attached(self):
        await self.assert_refused(self.assign(1, expected=12), CHANGED)

    async def test_a_shop_cannot_attach_a_platform_service(self):
        await self.assert_refused(self.assign(6, link(PLAT_LINK), staff=SHOP, expected=11))

    async def test_the_platform_cannot_attach_a_shops_service(self):
        await self.assert_refused(self.assign(1, link(SHOP_LINK), expected=21))

    async def test_a_blocked_customer_is_refused(self):
        await self.assert_refused(self.assign(8))

    async def test_an_unknown_link_or_username_is_refused(self):
        await self.assert_refused(self.preview(1, link("tok-unknown-0000000")))
        await self.assert_refused(self.preview(1, ServiceLookup("username", "nobody")))

    async def test_a_username_of_another_admin_reads_like_an_unknown_one(self):
        unknown = await self.assert_refused(self.preview(1, ServiceLookup("username", "nobody")))
        foreign = await self.assert_refused(self.preview(1, ServiceLookup("username", "shop-one")))
        self.assertEqual(foreign, unknown)

    async def test_a_panel_outage_is_a_safe_message_without_the_token(self):
        self.platform_panel.fail_with = PasarGuardError("boom https://p.example/sub/tok-plat-0000001", status_code=500)
        message = await self.assert_refused(self.assign(1))
        self.assertNotIn("tok-plat", message)
        self.assertNotIn("boom", message)

    async def test_lookups_are_budgeted_per_shop(self):
        for _ in range(TEST_BUDGET):
            await self.preview(1)
        await self.assert_refused(self.preview(1), TOO_MANY_LOOKUPS)


class ScopeAndPermissionTests(AssignCase):
    async def assert_denied(self, call, error: type[BaseException]) -> None:
        with self.assertRaises(error):
            await call
        self.assertEqual(await self.rows(), [])

    async def test_a_shop_cannot_reach_a_platform_customer_or_another_shops_customer(self):
        await self.assert_denied(self.assign(1, link(SHOP_LINK), staff=SHOP, expected=21), ShopScopeError)
        await self.assert_denied(self.assign(10, link(SHOP_LINK), staff=SHOP, expected=21), ShopScopeError)
        await self.assert_denied(self.preview(6, link(SHOP_LINK), staff=OTHER_SHOP), ShopScopeError)

    async def test_the_owner_cannot_attach_to_a_shops_customer(self):
        await self.assert_denied(self.assign(6, link(SHOP_LINK), expected=21), ShopScopeError)

    async def test_both_permissions_are_required(self):
        for perms in (["customer_services"], ["service_import_assign"], []):
            staff = {**SHOP, "permissions": perms}
            with self.subTest(perms=perms):
                self.assertFalse(may_assign_existing_services(staff))
                await self.assert_denied(self.preview(6, link(SHOP_LINK), staff=staff), PermissionError)
                await self.assert_denied(self.assign(6, link(SHOP_LINK), staff=staff, expected=21), PermissionError)
        self.assertEqual(self.shop_panel.info_calls, [], "no panel call without permission")

    async def test_staff_without_a_shop_are_denied(self):
        for staff in ({"role": "admin", "permissions": []}, {"role": "pg_staff", "username": "x"}):
            with self.subTest(staff=staff["role"]):
                with self.assertRaises((PermissionError, ShopScopeError)):
                    await self.assign(1, staff=staff)
        self.assertEqual(await self.rows(), [])


class InputTests(AssignCase):
    def test_lookup_kind_and_values_are_validated(self):
        with self.assertRaisesRegex(AssignRefused, re.escape(BAD_KIND)):
            parse_lookup("sql", "x")
        for value in ("<script>alert(1)</script>", "ab", "a" * 129, "علی", "a b"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(AssignRefused, re.escape(BAD_USERNAME)):
                    parse_lookup("username", value)
        for value in ("", "x" * 2049):
            with self.assertRaises(AssignRefused):
                parse_lookup("link", value)
        self.assertEqual(parse_lookup("username", "  user.name-1 "), ServiceLookup("username", "user.name-1"))

    def test_the_previewed_id_must_be_a_positive_integer(self):
        for raw in ("", "0", "-1", "1.5", "True", "١١", "9" * 13, None, "nan"):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(AssignRefused, re.escape(BAD_PREVIEW)):
                    parse_panel_user_id(raw)
        self.assertEqual(parse_panel_user_id(" 11 "), 11)

    async def test_a_hostile_reason_is_stored_as_text(self):
        await self.assign(1, reason="<b>x</b>")
        (log,) = await self.log_rows()
        self.assertEqual(log.reason, "<b>x</b>")


SHOP_WITH_ASSIGN = "dashboard,customer_services,service_import_assign"


class EndpointTests(TabHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.users["tok-plat-0000001"]["subscription_url"] = "https://p.example/sub/tok-plat-0000001"
        self.users["tok-shop-0000001"]["subscription_url"] = "https://p.example/sub/tok-shop-0000001"

    async def grant_shop(self, perms: str) -> None:
        async with self._session_factory()() as session:
            await session.execute(update(ResellerProfile).where(ResellerProfile.user_id == 5).values(web_permissions=perms))
            await session.commit()

    async def services(self) -> list[UserService]:
        async with self._session_factory()() as session:
            return list((await session.scalars(select(UserService))).all())

    async def notices(self) -> list[CustomerNotice]:
        async with self._session_factory()() as session:
            return list((await session.scalars(select(CustomerNotice))).all())

    async def test_the_owner_previews_and_attaches_and_the_link_never_reaches_a_url(self):
        preview = await self.form(OWNER_SESSION, "/users/1/import-service/preview", {"kind": "link", "value": PLAT_LINK})
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual((preview.json()["panel_user_id"], preview.json()["panel_username"]), (11, "plat-one"))
        self.assertNotIn("tok-plat", preview.text)
        done = await self.form(OWNER_SESSION, "/users/1/import-service", {
            "kind": "link", "value": PLAT_LINK, "panel_user_id": "11", "reason": REASON, "notify": "1",
        })
        self.assertEqual(done.status_code, 303)
        location = unquote(done.headers["location"])
        self.assertIn("edit=1", location)
        self.assertIn("ok=", location)
        self.assertNotIn("tok-plat", location)
        self.assertEqual([(s.bot_user_id, s.pg_user_id) for s in await self.services()], [(1, 11)])
        (notice,) = await self.notices()
        self.assertEqual((notice.bot_user_id, notice.shop_id, notice.reason), (1, None, REASON))

    async def test_without_the_notice_switch_the_customer_is_not_messaged(self):
        await self.form(OWNER_SESSION, "/users/1/import-service", {
            "kind": "link", "value": PLAT_LINK, "panel_user_id": "11", "reason": REASON,
        })
        self.assertEqual(len(await self.services()), 1)
        self.assertEqual(await self.notices(), [])

    async def test_a_missing_reason_attaches_nothing(self):
        done = await self.form(OWNER_SESSION, "/users/1/import-service", {"kind": "link", "value": PLAT_LINK, "panel_user_id": "11"})
        self.assertIn("err=", done.headers["location"])
        self.assertEqual(await self.services(), [])

    async def test_a_shop_without_the_permission_is_refused(self):
        await self.grant_shop("dashboard,customer_services")
        preview = await self.form(SHOP_SESSION, "/users/6/import-service/preview", {"kind": "link", "value": SHOP_LINK})
        self.assertEqual(preview.status_code, 403)
        self.assertEqual(preview.json()["error"], NO_PERMISSION)
        done = await self.form(SHOP_SESSION, "/users/6/import-service", {
            "kind": "link", "value": SHOP_LINK, "panel_user_id": "21", "reason": REASON,
        })
        self.assertIn("err=", done.headers["location"])
        self.assertEqual(await self.services(), [])

    async def test_a_shop_with_the_permission_attaches_only_for_its_own_customer(self):
        await self.grant_shop(SHOP_WITH_ASSIGN)
        foreign = await self.form(SHOP_SESSION, "/users/1/import-service/preview", {"kind": "link", "value": PLAT_LINK})
        self.assertEqual(foreign.status_code, 403)
        done = await self.form(SHOP_SESSION, "/users/6/import-service", {
            "kind": "link", "value": SHOP_LINK, "panel_user_id": "21", "reason": REASON,
        })
        self.assertIn("ok=", done.headers["location"])
        self.assertEqual([(s.bot_user_id, s.shop_id) for s in await self.services()], [(6, 5)])

    async def test_the_edit_screen_offers_the_section_only_with_the_permission(self):
        owner = await self.call(OWNER_SESSION, "GET", "/users/1/edit?fragment=1")
        self.assertIn("/users/1/import-service", owner.text)
        await self.grant_shop("dashboard,customer_services")
        shop = await self.call(SHOP_SESSION, "GET", "/users/6/edit?fragment=1")
        self.assertEqual(shop.status_code, 200)
        self.assertNotIn("import-service", shop.text)
        await self.grant_shop(SHOP_WITH_ASSIGN)
        shop = await self.call(SHOP_SESSION, "GET", "/users/6/edit?fragment=1")
        self.assertIn("/users/6/import-service", shop.text)
