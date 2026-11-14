"""«ظرفیت فعلی»: the limits of an existing PasarGuard admin are read, never guessed as unlimited."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.db.models import BotUser, ResellerPlan, ResellerProfile
from app.services import pg_admin_subscription as subs
from app.services.pg_admin_subscription import PgCapacityUnreadable, read_live_pg_admin_capacity
from app.services.pg_quota import extract_admin_capacity
from app.services.reseller_capacity_repair import repair_current_capacity_snapshots
from app.services.resellers import ensure_current_capacity_reseller_plan, provision_existing_pg_admin
from tests.test_update_owner_only import OWNER_SESSION, SHOP_SESSION, RoutesHarness

GB = 1024**3


class FakePg:
    def __init__(self, admins: dict[str, dict | Exception]) -> None:
        self.admins = admins
        self.modified: list[tuple[str, dict]] = []

    async def get_admin(self, username: str) -> dict:
        value = self.admins.get(username)
        if isinstance(value, Exception):
            raise value
        return value or {}

    async def modify_admin(self, username: str, payload: dict) -> None:
        self.modified.append((username, payload))


class ExtractTests(unittest.TestCase):
    def test_every_known_place_and_alias_is_read(self):
        cases = {
            "admin fields": ({"data_limit": 500 * GB, "max_users": 50}, None),
            "role limits": ({}, {"limits": {"data_limit": 500 * GB, "max_users": 50}}),
            "overrides": ({"permission_overrides": {"data_limit": 500 * GB, "max_users": 50}}, None),
            "aliases": ({"max_traffic": 500 * GB}, {"limits": {"users_max": 50}}),
        }
        for name, (admin, role) in cases.items():
            with self.subTest(name):
                self.assertEqual(extract_admin_capacity(admin, role), (500 * GB, 50))

    def test_missing_or_zero_means_unlimited_and_junk_is_ignored(self):
        self.assertEqual(extract_admin_capacity({"data_limit": 0, "max_users": None}), (0, 0))
        self.assertEqual(extract_admin_capacity(None), (0, 0))
        self.assertEqual(extract_admin_capacity({"data_limit": "x", "max_users": "-3"}), (0, 0))

    def test_the_admins_own_traffic_limit_beats_the_role_but_overrides_win_for_users(self):
        admin = {"data_limit": 10 * GB, "max_users": 5, "permission_overrides": {"max_users": 7}}
        role = {"limits": {"data_limit": 99 * GB, "max_users": 99}}
        self.assertEqual(extract_admin_capacity(admin, role), (10 * GB, 7))


class LiveReadTests(unittest.IsolatedAsyncioTestCase):
    async def read(self, admins):
        with patch("app.services.pasarguard.get_pg", lambda: FakePg(admins)):
            return await read_live_pg_admin_capacity("a")

    async def test_the_limits_are_returned_in_gb_and_users(self):
        self.assertEqual(await self.read({"a": {"data_limit": 500 * GB, "max_users": 50}}), (500, 50))

    async def test_a_limit_below_one_gb_rounds_up_instead_of_becoming_unlimited(self):
        self.assertEqual(await self.read({"a": {"data_limit": GB // 2, "max_users": 3}}), (1, 3))

    async def test_a_real_unlimited_admin_is_zero_zero(self):
        self.assertEqual(await self.read({"a": {"username": "a"}}), (0, 0))

    async def test_a_failed_or_empty_read_is_not_unlimited(self):
        for admins in ({"a": RuntimeError("down")}, {}):
            with self.subTest(admins=admins), self.assertRaises(PgCapacityUnreadable):
                await self.read(admins)


class DbCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 'c.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            self.plan_id = (await ensure_current_capacity_reseller_plan(session)).id
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        self._tmp.cleanup()

    async def subscription(self, username: str):
        async with self.Session() as session:
            return await subs.get_subscription(session, username)


class CreateSubscriptionTests(DbCase):
    async def test_new_subscription_copies_plan_id_from_the_shop_profile(self):
        async with self.Session() as session:
            session.add(
                BotUser(id=7, telegram_id=77, referral_code="r7", role="reseller", wallet_balance=0)
            )
            await session.flush()
            session.add(
                ResellerProfile(
                    user_id=7, pg_admin_username="newadmin", plan_id=self.plan_id, is_active=True
                )
            )
            await session.commit()
            sub = await subs.get_or_create_subscription(session, "newadmin")
            await session.commit()
        self.assertEqual(sub.plan_id, self.plan_id)


class SnapshotTests(DbCase):
    async def start(self, admins, username="a", **kw):
        pg = FakePg(admins)
        async with self.Session() as session:
            plan = await session.get(ResellerPlan, self.plan_id)
            with patch("app.services.pasarguard.get_pg", lambda: pg):
                sub = await subs.start_or_refresh_subscription(session, pg_username=username, plan=plan, **kw)
            await session.commit()
        return sub, pg

    async def test_the_existing_limits_are_recorded_and_nothing_is_written_to_the_panel(self):
        sub, pg = await self.start({"a": {"data_limit": 500 * GB, "max_users": 50}})
        self.assertEqual((sub.base_gb, sub.base_users), (500, 50))
        self.assertEqual(pg.modified, [])

    async def test_a_first_grant_fails_instead_of_recording_unlimited_when_the_panel_cannot_be_read(self):
        with self.assertRaises(PgCapacityUnreadable):
            await self.start({"a": RuntimeError("down")})
        self.assertIsNone(await self.subscription("a"))

    async def test_a_refresh_keeps_the_recorded_limits_when_the_panel_cannot_be_read(self):
        await self.start({"a": {"data_limit": 500 * GB, "max_users": 50}})
        sub, _pg = await self.start({"a": RuntimeError("down")})
        self.assertEqual((sub.base_gb, sub.base_users), (500, 50))

    async def test_the_owner_budget_does_not_block_recording_what_the_admin_already_has(self):
        with patch.object(subs, "_assert_owner_capacity_budget", AsyncMock(side_effect=ValueError("over"))) as budget:
            sub, _pg = await self.start({"a": {"data_limit": 500 * GB, "max_users": 50}})
        budget.assert_not_awaited()
        self.assertEqual(sub.base_gb, 500)


class GrantTests(DbCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        async with self.Session() as session:
            session.add(BotUser(id=5, telegram_id=105, referral_code="s5", role="user"))
            await session.commit()

    async def grant(self, admins, **patches):
        pg = FakePg(admins)
        async with self.Session() as session:
            with patch("app.services.pasarguard.get_pg", lambda: pg):
                return await provision_existing_pg_admin(
                    session, pg_username="admin-a", web_username="admin-a", password="Zx9-Strong-Pass-42", plan_id=self.plan_id, note="t",
                )

    async def profiles(self):
        async with self.Session() as session:
            return list((await session.scalars(select(ResellerProfile))).all())

    async def test_nothing_is_created_when_the_existing_limits_cannot_be_read(self):
        profile, _hint, err = await self.grant({"admin-a": RuntimeError("down")})
        self.assertIsNone(profile)
        self.assertIn("خوانده نشد", err)
        self.assertEqual(await self.profiles(), [])
        self.assertIsNone(await self.subscription("admin-a"))

    async def test_a_failed_subscription_step_is_reported_not_swallowed(self):
        admins = {"admin-a": {"data_limit": 500 * GB, "max_users": 50}}
        with patch.object(subs, "start_or_refresh_subscription", AsyncMock(side_effect=ValueError("بودجه"))):
            profile, _hint, err = await self.grant(admins)
        self.assertIsNotNone(profile)
        self.assertIn("اشتراک پلن ثبت نشد", err)
        self.assertIn("بودجه", err)
        self.assertIsNone(await self.subscription("admin-a"))

    async def test_a_successful_grant_records_the_existing_limits(self):
        profile, _hint, err = await self.grant({"admin-a": {"data_limit": 500 * GB, "max_users": 50}})
        self.assertIsNone(err)
        self.assertIsNotNone(profile)
        sub = await self.subscription("admin-a")
        self.assertEqual((sub.base_gb, sub.base_users), (500, 50))


class RepairTests(DbCase):
    async def seed(self):
        async with self.Session() as session:
            other = ResellerPlan(name="ثابت", price=0, plan_kind="subscription")
            session.add(other)
            await session.flush()
            for name, plan_id, gb, users in (
                ("zero1", self.plan_id, 0, 0), ("zero2", self.plan_id, 0, 0), ("zero3", self.plan_id, 0, 0),
                ("zero4", self.plan_id, 0, 0), ("kept", self.plan_id, 10, 2), ("fixedplan", other.id, 0, 0),
            ):
                session.add(subs.PgAdminSubscription(pg_username=name, plan_id=plan_id, base_gb=gb, base_users=users))
            await session.commit()

    async def run_repair(self, admins):
        async with self.Session() as session:
            with patch("app.services.pasarguard.get_pg", lambda: FakePg(admins)):
                return await repair_current_capacity_snapshots(session)

    async def test_only_zero_current_capacity_rows_with_a_readable_limit_change(self):
        await self.seed()
        admins = {
            "zero1": {"data_limit": 500 * GB, "max_users": 50},
            "zero2": RuntimeError("down"),
            "zero3": {"username": "zero3"},
            "zero4": {"max_users": 9},
            "kept": {"data_limit": 999 * GB, "max_users": 99},
            "fixedplan": {"data_limit": 1 * GB},
        }
        report = await self.run_repair(admins)
        self.assertEqual((report.fixed, report.unreadable, report.unlimited), (2, 1, 1))
        now = {n: await self.subscription(n) for n in admins}
        self.assertEqual((now["zero1"].base_gb, now["zero1"].base_users), (500, 50))
        self.assertEqual((now["zero4"].base_gb, now["zero4"].base_users), (0, 9))
        self.assertEqual((now["zero2"].base_gb, now["zero3"].base_gb), (0, 0))
        self.assertEqual((now["kept"].base_gb, now["kept"].base_users), (10, 2))
        self.assertEqual(now["fixedplan"].base_gb, 0)

    async def test_running_it_twice_changes_nothing_more(self):
        await self.seed()
        admins = {"zero1": {"data_limit": 500 * GB}, "zero2": {"data_limit": 5 * GB}}
        await self.run_repair(admins)
        again = await self.run_repair(admins)
        self.assertEqual(again.fixed, 0)

    async def test_repair_subtracts_purchased_extras_from_live_base(self):
        await self.seed()
        async with self.Session() as session:
            row = await session.scalar(
                select(subs.PgAdminSubscription).where(subs.PgAdminSubscription.pg_username == "zero1")
            )
            row.extra_gb_purchased = 200
            await session.commit()
        report = await self.run_repair({"zero1": {"data_limit": 400 * GB, "max_users": 10}})
        self.assertEqual(report.fixed, 1)
        fixed = await self.subscription("zero1")
        self.assertEqual((fixed.base_gb, fixed.extra_gb_purchased, fixed.base_users), (200, 200, 10))

    async def test_repair_copies_missing_plan_id_from_the_shop_profile(self):
        await self.seed()
        async with self.Session() as session:
            session.add(
                BotUser(id=9, telegram_id=99, referral_code="r9", role="reseller", wallet_balance=0)
            )
            await session.flush()
            session.add(
                ResellerProfile(
                    user_id=9, pg_admin_username="orphan", plan_id=self.plan_id, is_active=True
                )
            )
            session.add(
                subs.PgAdminSubscription(
                    pg_username="orphan", plan_id=None, base_gb=0, base_users=0, access_status="active"
                )
            )
            await session.commit()
        report = await self.run_repair({"orphan": {"data_limit": 300 * GB, "max_users": 8}})
        self.assertEqual(report.plan_linked, 1)
        self.assertEqual(report.fixed, 1)
        fixed = await self.subscription("orphan")
        self.assertEqual((fixed.plan_id, fixed.base_gb, fixed.base_users), (self.plan_id, 300, 8))


class RepairRouteTests(RoutesHarness):
    async def post(self, session_payload: dict):
        cookie = f"session={self.signer.dumps(session_payload)}; csrf=token"
        headers = {"cookie": cookie, "x-csrf-token": "token", "origin": "http://panel.test"}
        return await self.client.post("/resellers/repair-capacity", headers=headers, data={})

    async def test_the_owner_runs_it_and_is_told_the_result(self):
        with patch("app.api.reseller_capacity_repair_pages.repair_current_capacity_snapshots", AsyncMock(
            return_value=type(
                "R", (), {"fixed": 3, "unreadable": 1, "unlimited": 0, "plan_linked": 2}
            )()
        )) as repair:
            response = await self.post(OWNER_SESSION)
        self.assertEqual(response.status_code, 303)
        repair.assert_awaited_once()
        self.assertIn("ok=", response.headers["location"])

    async def test_a_shop_cannot_run_it(self):
        with patch("app.api.reseller_capacity_repair_pages.repair_current_capacity_snapshots", AsyncMock()) as repair:
            response = await self.post(SHOP_SESSION)
        repair.assert_not_awaited()
        self.assertIn("err=", response.headers["location"])
