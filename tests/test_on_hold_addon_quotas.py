"""Addon activation must preserve both quotas on pending-start services."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import Base
from app.db.models import BotUser, Order, OrderStatus, Plan, ServiceAddonPack, UserService
from app.services.service_addons import _apply_pack_to_service, apply_service_addon
from tests.postgres_support import add_rows, scoped_async_engine

NOW = 2_000_000_000
DAY = 86400
GB = 1024**3


class UnloadedPlanService(SimpleNamespace):
    @property
    def plan(self):
        raise AssertionError("must load the plan through AsyncSession")


class OnHoldAddonQuotaTests(unittest.IsolatedAsyncioTestCase):
    def service(self, *, plan_days=30, unloaded=False):
        cls = UnloadedPlanService if unloaded else SimpleNamespace
        svc = cls(
            id=1,
            pg_user_id=9,
            plan_id=7,
            quota_expire_at=None,
            quota_data_limit_bytes=None,
            quota_synced_at=None,
        )
        if not unloaded:
            svc.plan = SimpleNamespace(duration_days=plan_days)
        return svc

    async def apply(self, info, *, kind, amount=5, service=None, reseller_id=None):
        svc = service if service is not None else self.service()
        state = dict(info)
        pg = MagicMock()
        pg.get_user_by_id = AsyncMock(return_value=dict(info))

        async def modify(user_id, payload):
            # PG applies supplied quotas and retains omitted ones; changing
            # on_hold to active alone does not start the expiration timer.
            state.update(payload)
            return {}  # Delivery must also work with a partial write response.

        pg.modify_user_by_id = AsyncMock(side_effect=modify)
        session = AsyncMock()
        session.get.return_value = Plan(id=7, name="base", price=1, duration_days=30)
        with (
            patch("app.services.pasarguard.get_pg", return_value=pg),
            patch(
                "app.services.pasarguard.get_pg_for_reseller",
                AsyncMock(return_value=pg),
            ) as get_reseller,
            patch("app.services.users.current_shop_reseller_id", return_value=None),
            patch("time.time", return_value=NOW),
        ):
            await _apply_pack_to_service(
                session, svc, kind=kind, amount=amount, order_reseller_id=reseller_id
            )
        pg.modify_user_by_id.assert_awaited_once()
        if reseller_id:
            get_reseller.assert_awaited_once_with(session, reseller_id)
        else:
            get_reseller.assert_not_awaited()
        return pg.modify_user_by_id.await_args.args[1], state, svc, session

    async def test_volume_starts_existing_hold_duration_and_updates_both_caches(self):
        for field in (
            "expire_duration",
            "on_hold_expire_duration",
            "hold_expire_duration",
        ):
            with self.subTest(duration_field=field):
                payload, state, svc, _ = await self.apply(
                    {
                        "status": "on_hold",
                        "expire": 0,
                        field: 10 * DAY,
                        "data_limit": 5 * GB,
                        "used_traffic": GB,
                    },
                    kind="volume",
                )
                self.assertEqual(state["status"], "active")
                self.assertEqual(state["expire"], NOW + 10 * DAY)
                self.assertEqual(state["data_limit"], 10 * GB)
                self.assertEqual(state["used_traffic"], GB)
                self.assertEqual(payload["expire"], NOW + 10 * DAY)
                self.assertEqual(
                    svc.quota_expire_at,
                    datetime.fromtimestamp(NOW + 10 * DAY, timezone.utc),
                )
                self.assertEqual(svc.quota_data_limit_bytes, 10 * GB)

    async def test_duration_preserves_live_volume_and_updates_both_caches(self):
        for data_limit in (5 * GB, 0, None):
            with self.subTest(data_limit=data_limit):
                payload, state, svc, _ = await self.apply(
                    {
                        "status": "on_hold",
                        "expire": None,
                        "on_hold_expire_duration": 10 * DAY,
                        "data_limit": data_limit,
                        "used_traffic": GB,
                    },
                    kind="duration",
                )
                self.assertEqual(state["expire"], NOW + 15 * DAY)
                self.assertEqual(payload["data_limit"], data_limit or 0)
                self.assertEqual(state["data_limit"], data_limit or 0)
                self.assertEqual(state["used_traffic"], GB)
                self.assertEqual(svc.quota_data_limit_bytes, data_limit or 0)
                self.assertEqual(
                    svc.quota_expire_at,
                    datetime.fromtimestamp(NOW + 15 * DAY, timezone.utc),
                )

    async def test_hold_without_duration_loads_plan_without_lazy_loading(self):
        for kind in ("volume", "duration"):
            with self.subTest(kind=kind):
                payload, _, _, session = await self.apply(
                    {"status": "on_hold", "expire": 0, "data_limit": 5 * GB},
                    kind=kind,
                    service=self.service(unloaded=True),
                )
                extra_days = 5 if kind == "duration" else 0
                self.assertEqual(payload["expire"], NOW + (30 + extra_days) * DAY)
                session.get.assert_awaited_once_with(Plan, 7)

    async def test_hold_without_duration_uses_loaded_plan(self):
        for kind in ("volume", "duration"):
            with self.subTest(kind=kind):
                payload, _, _, session = await self.apply(
                    {"status": "on_hold", "expire": 0, "data_limit": 5 * GB},
                    kind=kind,
                    service=self.service(plan_days=12),
                )
                extra_days = 5 if kind == "duration" else 0
                self.assertEqual(payload["expire"], NOW + (12 + extra_days) * DAY)
                session.get.assert_not_awaited()

    async def test_unknown_hold_duration_rejects_before_panel_write(self):
        for kind in ("volume", "duration"):
            with self.subTest(kind=kind):
                svc = self.service(plan_days=0)
                svc.plan_id = None
                pg = MagicMock()
                pg.get_user_by_id = AsyncMock(
                    return_value={
                        "status": "on_hold",
                        "expire": 0,
                        "data_limit": 5 * GB,
                    }
                )
                pg.modify_user_by_id = AsyncMock()
                with (
                    patch("app.services.pasarguard.get_pg", return_value=pg),
                    patch(
                        "app.services.users.current_shop_reseller_id", return_value=None
                    ),
                ):
                    with self.assertRaisesRegex(ValueError, "مدت"):
                        await _apply_pack_to_service(
                            AsyncMock(),
                            svc,
                            kind=kind,
                            amount=5,
                            order_reseller_id=None,
                        )
                pg.modify_user_by_id.assert_not_awaited()
                self.assertIsNone(svc.quota_synced_at)

    async def test_hold_with_absolute_expire_keeps_that_expire_for_volume(self):
        payload, state, _, session = await self.apply(
            {
                "status": "on_hold",
                "expire": NOW + 3 * DAY,
                "on_hold_expire_duration": 10 * DAY,
                "data_limit": 5 * GB,
            },
            kind="volume",
        )
        self.assertEqual(payload["expire"], NOW + 3 * DAY)
        self.assertEqual(state["data_limit"], 10 * GB)
        session.get.assert_not_awaited()

    async def test_active_and_expired_duration_extension_preserves_volume(self):
        for expire, expected in (
            (NOW + 3 * DAY, NOW + 8 * DAY),
            (NOW - DAY, NOW + 5 * DAY),
        ):
            with self.subTest(expire=expire):
                _, state, _, _ = await self.apply(
                    {"status": "active", "expire": expire, "data_limit": 5 * GB},
                    kind="duration",
                )
                self.assertEqual(state["expire"], expected)
                self.assertEqual(state["data_limit"], 5 * GB)

    async def test_active_volume_keeps_existing_time(self):
        for expire in (NOW + 3 * DAY, 0):
            with self.subTest(expire=expire):
                payload, state, _, _ = await self.apply(
                    {"status": "active", "expire": expire, "data_limit": 5 * GB},
                    kind="volume",
                )
                self.assertNotIn("expire", payload)
                self.assertEqual(state["expire"], expire)
                self.assertEqual(state["data_limit"], 10 * GB)

    async def test_reseller_hold_volume_uses_shop_client(self):
        _, state, _, _ = await self.apply(
            {
                "status": "on_hold",
                "expire": 0,
                "expire_duration": 10 * DAY,
                "data_limit": 5 * GB,
            },
            kind="volume",
            reseller_id=42,
        )
        self.assertEqual(state["expire"], NOW + 10 * DAY)
        self.assertEqual(state["data_limit"], 10 * GB)

    async def test_truly_unlimited_target_quota_still_rejects(self):
        for kind, info in (
            ("volume", {"status": "active", "expire": NOW + DAY, "data_limit": 0}),
            ("duration", {"status": "active", "expire": 0, "data_limit": 5 * GB}),
        ):
            with self.subTest(kind=kind):
                pg = MagicMock()
                pg.get_user_by_id = AsyncMock(return_value=info)
                pg.modify_user_by_id = AsyncMock()
                with (
                    patch("app.services.pasarguard.get_pg", return_value=pg),
                    patch(
                        "app.services.users.current_shop_reseller_id", return_value=None
                    ),
                ):
                    with self.assertRaisesRegex(ValueError, "نامحدود"):
                        await _apply_pack_to_service(
                            AsyncMock(),
                            self.service(),
                            kind=kind,
                            amount=5,
                            order_reseller_id=None,
                        )
                pg.modify_user_by_id.assert_not_awaited()


class OnHoldAddonDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add(BotUser(id=1, telegram_id=1, referral_code="buyer"))
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def seed_order(self, kind, *, plan_days):
        async with self.Session() as session:
            await add_rows(session,
                [
                    Plan(id=7, name="base", price=1, duration_days=plan_days),
                    ServiceAddonPack(id=1, name="extra", kind=kind, amount=5, price=1),
                    UserService(
                        id=1, bot_user_id=1, plan_id=7, pg_user_id=9, pg_username="u1"
                    ),
                    Order(
                        id=1,
                        user_id=1,
                        amount=1,
                        status=OrderStatus.PAID.value,
                        service_id=1,
                        note=f"svc_addon:1:1:{kind}:5",
                    ),
                ]
            )
            await session.commit()

    async def test_delivery_loads_plan_persists_quotas_and_retry_does_not_double_apply(
        self,
    ):
        for kind in ("volume", "duration"):
            with self.subTest(kind=kind):
                await self.seed_order(kind, plan_days=30)
                pg = MagicMock()
                pg.get_user_by_id = AsyncMock(
                    return_value={
                        "status": "on_hold",
                        "expire": 0,
                        "data_limit": 5 * GB,
                    }
                )
                pg.modify_user_by_id = AsyncMock(return_value={})
                with (
                    patch("app.services.pasarguard.get_pg", return_value=pg),
                    patch(
                        "app.services.users.current_shop_reseller_id", return_value=None
                    ),
                    patch("time.time", return_value=NOW),
                ):
                    # A fresh session reproduces the real unloaded plan relationship.
                    async with self.Session() as session:
                        order = await session.get(Order, 1)
                        service = await session.get(UserService, 1)
                        self.assertNotIn("plan", service.__dict__)
                        await apply_service_addon(session, order)
                        await apply_service_addon(session, order)
                        self.assertEqual(order.status, OrderStatus.DELIVERED.value)
                    async with self.Session() as session:
                        service = await session.get(UserService, 1)
                        expected_days = 35 if kind == "duration" else 30
                        self.assertEqual(
                            int(
                                service.quota_expire_at.replace(
                                    tzinfo=timezone.utc
                                ).timestamp()
                            ),
                            NOW + expected_days * DAY,
                        )
                        self.assertEqual(
                            service.quota_data_limit_bytes,
                            (10 if kind == "volume" else 5) * GB,
                        )
                        for model in (Order, UserService, ServiceAddonPack, Plan):
                            await session.delete(
                                await session.get(model, 1 if model is not Plan else 7)
                            )
                            # Children first: the unit of work does not order deletes by foreign key.
                            await session.flush()
                        await session.commit()
                pg.modify_user_by_id.assert_awaited_once()

    async def test_unknown_duration_releases_delivery_claim_without_panel_mutation(
        self,
    ):
        await self.seed_order("volume", plan_days=0)
        pg = MagicMock()
        pg.get_user_by_id = AsyncMock(
            return_value={
                "status": "on_hold",
                "expire": 0,
                "data_limit": 5 * GB,
            }
        )
        pg.modify_user_by_id = AsyncMock()
        with (
            patch("app.services.pasarguard.get_pg", return_value=pg),
            patch("app.services.users.current_shop_reseller_id", return_value=None),
        ):
            async with self.Session() as session:
                order = await session.get(Order, 1)
                with self.assertRaisesRegex(ValueError, "مدت"):
                    await apply_service_addon(session, order)
            async with self.Session() as session:
                order = await session.get(Order, 1)
                service = await session.get(UserService, 1)
                self.assertEqual(order.status, OrderStatus.PAID.value)
                self.assertIsNone(service.quota_synced_at)
        pg.modify_user_by_id.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
