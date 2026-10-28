"""Real legacy delivery/import/manual constructors record the same canonical adapter."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select

from app.db.customer_models import Customer
from app.db.models import BotUser, Plan, UserService
from app.services.bot_user_admin import admin_provision_service
from test_integration_customers import database
import test_order_delivery_scope as delivery
import test_service_import as imports


class ActiveIdentityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        active = patch("app.services.customer_identity.integration_execution_active", return_value=True)
        active.start()
        self.addCleanup(active.stop)


class LegacyDeliveryIdentityTests(ActiveIdentityTests):
    asyncSetUp = delivery.OrderDeliveryScopeTests.asyncSetUp
    asyncTearDown = delivery.OrderDeliveryScopeTests.asyncTearDown
    _deliver = delivery.OrderDeliveryScopeTests._deliver

    async def test_wholesale_delivery_writes_one_customer_and_stable_service_ids(self) -> None:
        self.order.quantity = 3
        await self.session.commit()
        await self._deliver()
        rows = list((await self.session.execute(select(UserService))).scalars())
        assert len(rows) == 3 and len({row.public_id for row in rows}) == 3
        canonical = (await self.session.scalars(select(Customer))).one()
        assert canonical.legacy_bot_user_id == self.buyer.id
        assert all(row.customer_id == canonical.id and row.bot_user_id == self.buyer.id for row in rows)
        ids = {row.public_id for row in rows}
        await self._deliver()
        assert {row.public_id for row in (await self.session.scalars(select(UserService)))} == ids
        assert await self.session.scalar(select(func.count()).select_from(Customer)) == 1


class LegacyImportIdentityTests(ActiveIdentityTests):
    asyncSetUp = imports.ImportCase.asyncSetUp
    asyncTearDown = imports.ImportCase.asyncTearDown
    run_import = imports.ImportCase.run_import

    async def test_verified_legacy_import_records_adapter_and_repeat_preserves_ids(self) -> None:
        first = await self.run_import(1, "tok-plat-0000001")
        second = await self.run_import(1, "tok-plat-0000001")
        assert first.service.id == second.service.id
        assert first.service.public_id == second.service.public_id
        async with self.Session() as session:
            customer = (await session.scalars(select(Customer))).one()
            assert first.service.customer_id == customer.id
            assert customer.legacy_bot_user_id == 1


class ManualServiceIdentityTests(ActiveIdentityTests):
    async def test_manual_provision_uses_canonical_owner_and_preserves_shop(self) -> None:
        async with database() as (sessions, integration, key), sessions() as session:
            user = await session.get(BotUser, 11)
            plan = Plan(name="synthetic", price=1, pg_template_id=1)
            session.add(plan)
            await session.commit()
            pg = AsyncMock()
            pg.create_user_from_template.return_value = {"id": 701, "username": "synthetic"}
            with patch("app.services.pasarguard.get_pg", return_value=pg), patch(
                "app.services.orders.generate_pg_username", AsyncMock(return_value="synthetic")), patch(
                "app.services.orders._reseller_pg_link", AsyncMock(return_value=(None, None))):
                service = await admin_provision_service(session, user, plan, shop_id=None)
            customer = (await session.scalars(select(Customer))).one()
            assert service.customer_id == customer.id and customer.legacy_bot_user_id == user.id
            assert service.bot_user_id == user.id and service.shop_id is None
            assert service.public_id.startswith("svc_")
