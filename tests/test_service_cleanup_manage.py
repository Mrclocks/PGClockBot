"""Staff keep a service out of the cleanup or give it more days: scoped, reasoned, logged."""

from __future__ import annotations

import os
import unittest
from datetime import timedelta
from urllib.parse import unquote

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy import select

from app.db.models import ShopStaffAction, UserService
from app.services import service_cleanup_manage as manage
from app.services.service_cleanup import CleanupPolicy, CleanupPolicyError
from tests.test_service_cleanup import (
    NOW,
    OTHER_SHOP,
    SHOP,
    CleanupCase,
    SHOP_SESSION,
)

POLICY = CleanupPolicy(time_on=True, time_days=7, time_enabled_at=NOW - timedelta(days=60))
ROUTE = "/service-cleanup"


class ParseDaysTests(unittest.TestCase):
    def test_only_whole_days_in_range_are_accepted(self) -> None:
        self.assertEqual(manage.parse_hold_days("30"), 30)
        for bad in (None, "", "0", "366", "-1", "abc", "7.5", "99999"):
            with self.assertRaises(CleanupPolicyError, msg=bad):
                manage.parse_hold_days(bad)


class ManageServiceTests(CleanupCase):
    async def test_exempt_and_back_only_for_this_scope(self) -> None:
        await self.add_service(1, 20, SHOP, quota_expire_at=NOW - timedelta(days=20))
        await self.add_service(2, 21, OTHER_SHOP, quota_expire_at=NOW - timedelta(days=20))
        async with await self.session() as session:
            await manage.set_exempt(session, shop_id=SHOP, service_id=1, exempt=True)
            with self.assertRaises(ValueError):
                await manage.set_exempt(session, shop_id=SHOP, service_id=2, exempt=True)  # another shop's
            with self.assertRaises(ValueError):
                await manage.set_exempt(session, shop_id=None, service_id=1, exempt=True)  # the platform's scope
            self.assertEqual([r.service_id for r in await manage.list_exempt(session, shop_id=SHOP)], [1])
            self.assertEqual(await manage.list_exempt(session, shop_id=OTHER_SHOP), [])
            await manage.set_exempt(session, shop_id=SHOP, service_id=1, exempt=False)
            self.assertEqual(await manage.list_exempt(session, shop_id=SHOP), [])

    async def test_extending_adds_days_to_the_current_deadline_and_rewarns(self) -> None:
        await self.add_service(1, 20, SHOP, quota_expire_at=NOW - timedelta(days=5), cleanup_warn_stage=3, cleanup_volume_warn_stage=2)
        async with await self.session() as session:
            until = await manage.extend_deadline(session, shop_id=SHOP, service_id=1, days=10, policy=POLICY, now=NOW)
            row = await session.get(UserService, 1)
            self.assertEqual(until, (NOW + timedelta(days=2 + 10)).strftime("%Y-%m-%d"))  # deadline was in 2 days
            self.assertEqual((row.cleanup_warn_stage, row.cleanup_volume_warn_stage), (1, 1))

    async def test_extending_an_overdue_service_counts_from_now_and_never_lowers_a_hold(self) -> None:
        await self.add_service(1, 20, SHOP, quota_expire_at=NOW - timedelta(days=50))
        async with await self.session() as session:
            first = await manage.extend_deadline(session, shop_id=SHOP, service_id=1, days=5, policy=POLICY, now=NOW)
            self.assertEqual(first, (NOW + timedelta(days=5)).strftime("%Y-%m-%d"))
            again = await manage.extend_deadline(session, shop_id=SHOP, service_id=1, days=5, policy=POLICY, now=NOW)
            self.assertEqual(again, (NOW + timedelta(days=10)).strftime("%Y-%m-%d"))

    async def test_extending_another_scopes_service_changes_nothing(self) -> None:
        await self.add_service(2, 21, OTHER_SHOP, quota_expire_at=NOW - timedelta(days=20), cleanup_warn_stage=3)
        async with await self.session() as session:
            with self.assertRaises(ValueError):
                await manage.extend_deadline(session, shop_id=SHOP, service_id=2, days=5, policy=POLICY, now=NOW)
            row = await session.get(UserService, 2)
            self.assertEqual((row.cleanup_hold_until, row.cleanup_warn_stage), (None, 3))


class ManageRoutesTests(CleanupCase):
    async def call(self, method: str, path: str, data: dict | None = None):
        cookie = f"session={self.signer.dumps(SHOP_SESSION)}; csrf=token"
        headers = {"cookie": cookie, "x-csrf-token": "token", "origin": "http://panel.test"}
        return await self.client.request(method, path, headers=headers, data=data)

    async def logged(self):
        async with await self.session() as session:
            return list((await session.scalars(select(ShopStaffAction).order_by(ShopStaffAction.id))).all())

    async def setUp_service(self) -> None:
        await self.set_shop_permissions("dashboard,service_cleanup")
        await self.add_service(1, 20, SHOP, quota_expire_at=NOW - timedelta(days=20))

    async def test_without_a_reason_nothing_changes_and_nothing_is_logged(self) -> None:
        await self.setUp_service()
        for path, data in ((f"{ROUTE}/1/exempt", {"value": "1"}), (f"{ROUTE}/1/hold", {"days": "7"})):
            for reason in ({}, {"reason": "x"}):
                response = await self.call("POST", path, {**data, **reason})
                self.assertIn("علت", unquote(response.headers["location"]))
        async with await self.session() as session:
            row = await session.get(UserService, 1)
            self.assertEqual((row.cleanup_exempt, row.cleanup_hold_until), (False, None))
        self.assertEqual(await self.logged(), [])

    async def test_a_reasoned_exempt_and_hold_are_applied_and_logged_for_the_shop(self) -> None:
        await self.setUp_service()
        await self.call("POST", f"{ROUTE}/1/exempt", {"value": "1", "reason": "مشتری ویژه"})
        await self.call("POST", f"{ROUTE}/1/hold", {"days": "7", "reason": "درخواست مشتری"})
        async with await self.session() as session:
            row = await session.get(UserService, 1)
            self.assertTrue(row.cleanup_exempt)
            self.assertIsNotNone(row.cleanup_hold_until)
        self.assertEqual([(a.action, a.target_id, a.shop_id, a.reason) for a in await self.logged()],
                         [("service_cleanup_exempt", 1, SHOP, "مشتری ویژه"), ("service_cleanup_hold", 1, SHOP, "درخواست مشتری")])

    async def test_an_invalid_day_count_and_a_foreign_service_are_refused(self) -> None:
        await self.setUp_service()
        await self.add_service(2, 21, OTHER_SHOP, quota_expire_at=NOW - timedelta(days=20))
        bad = await self.call("POST", f"{ROUTE}/1/hold", {"days": "0", "reason": "درخواست مشتری"})
        self.assertIn("err=", bad.headers["location"])
        foreign = await self.call("POST", f"{ROUTE}/2/exempt", {"value": "1", "reason": "درخواست مشتری"})
        self.assertIn("err=", foreign.headers["location"])
        async with await self.session() as session:
            self.assertFalse((await session.get(UserService, 2)).cleanup_exempt)
        self.assertEqual(await self.logged(), [])

    async def test_without_the_permission_every_action_goes_home_and_changes_nothing(self) -> None:
        await self.set_shop_permissions("dashboard")
        await self.add_service(1, 20, SHOP, quota_expire_at=NOW - timedelta(days=20))
        for path, data in ((f"{ROUTE}/1/exempt", {"value": "1"}), (f"{ROUTE}/1/hold", {"days": "7"})):
            response = await self.call("POST", path, {**data, "reason": "درخواست مشتری"})
            self.assertEqual(response.headers["location"], "/home")
        async with await self.session() as session:
            self.assertFalse((await session.get(UserService, 1)).cleanup_exempt)

    async def test_the_page_lists_exempt_services_and_escapes_their_names(self) -> None:
        await self.set_shop_permissions("dashboard,service_cleanup")
        await self.add_service(1, 20, SHOP, pg_username="keep<b>", cleanup_exempt=True)
        response = await self.call("GET", ROUTE)
        self.assertEqual(response.status_code, 200)
        self.assertIn("keep&lt;b&gt;", response.text)
        self.assertNotIn("keep<b>", response.text)


del CleanupCase
