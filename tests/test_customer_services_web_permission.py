"""The web users screens enforce the same ``customer_services`` key as the shop bot."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, Plan, ResellerProfile, UserService
from app.services.bot_user_admin import customer_service_actions, may_manage_customer_services
from tests.test_update_owner_only import OWNER_SESSION, SHOP_SESSION, RoutesHarness

SHOP_ID = 5
CUSTOMER_ID = 7
SERVICE_ID = 12
PLAN_ID = 3
FULL_PG_ROLE = {
    "pg_user_actions": {"create": True, "read": True, "update": True, "delete": True, "reset_usage": True}
}


class CustomerServicesWebPermissionTests(RoutesHarness):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        async with AsyncSession(bind=self.engine, expire_on_commit=False) as session:
            session.add(
                BotUser(id=CUSTOMER_ID, telegram_id=107, referral_code="cust", role="user", reseller_id=SHOP_ID)
            )
            session.add(Plan(id=PLAN_ID, name="Gold", price=1, duration_days=30, data_limit_gb=10, owner_reseller_id=SHOP_ID))
            await session.flush()
            session.add(
                UserService(
                    id=SERVICE_ID,
                    bot_user_id=CUSTOMER_ID,
                    plan_id=PLAN_ID,
                    pg_user_id=SERVICE_ID,
                    pg_username="svc12",
                    subscription_url="https://sub.example/svc12",
                    remark="order:1",
                    shop_id=SHOP_ID,
                )
            )
            await session.commit()

    async def set_shop_permissions(self, raw: str | None) -> None:
        async with AsyncSession(bind=self.engine) as session:
            profile = await session.get(ResellerProfile, 1)
            profile.web_permissions = raw
            profile.bot_permissions = raw
            await session.commit()

    async def post(self, path: str, data: dict | None = None):
        cookie = f"session={self.signer.dumps(SHOP_SESSION)}; csrf=token"
        headers = {"cookie": cookie, "x-csrf-token": "token", "origin": "http://panel.test"}
        return await self.client.post(path, headers=headers, data=data or {})

    def patched_service_ops(self) -> dict[str, AsyncMock]:
        mocks = {
            "admin_renew_service": AsyncMock(),
            "admin_extend_service": AsyncMock(),
            "admin_delete_service": AsyncMock(return_value={}),
        }
        for name, mock in mocks.items():
            patcher = patch(f"app.services.bot_user_admin.{name}", mock)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch("app.services.pg_access.staff_user_actions", return_value=FULL_PG_ROLE["pg_user_actions"])
        patcher.start()
        self.addCleanup(patcher.stop)
        return mocks

    async def test_shop_without_customer_services_cannot_change_a_service_from_the_web(self) -> None:
        await self.set_shop_permissions("dashboard")
        mocks = self.patched_service_ops()
        base = f"/users/{CUSTOMER_ID}/services/{SERVICE_ID}"
        for path, data in (
            (f"{base}/renew", {"plan_id": str(PLAN_ID)}),
            (f"{base}/extend", {"extra_days": "5", "extra_gb": "0"}),
            (f"{base}/delete", {}),
            (f"/users/{CUSTOMER_ID}/quick-renew", {}),
        ):
            response = await self.post(path, data)
            self.assertEqual(response.status_code, 303, path)
            self.assertNotIn(f"/users?uid={CUSTOMER_ID}", response.headers["location"], path)
        for mock in mocks.values():
            mock.assert_not_awaited()

    async def test_shop_without_customer_services_cannot_read_a_subscription_link(self) -> None:
        await self.set_shop_permissions("dashboard")
        response = await self.call(SHOP_SESSION, "GET", f"/users/{CUSTOMER_ID}/services/{SERVICE_ID}/link")
        self.assertEqual(response.status_code, 303)
        self.assertNotIn("sub.example", response.text)

    async def test_shop_with_customer_services_reaches_the_service_action(self) -> None:
        await self.set_shop_permissions("dashboard,customer_services")
        mocks = self.patched_service_ops()
        response = await self.post(
            f"/users/{CUSTOMER_ID}/services/{SERVICE_ID}/extend", {"extra_days": "5", "extra_gb": "0"}
        )
        self.assertEqual(response.status_code, 303)
        mocks["admin_extend_service"].assert_awaited_once()
        self.assertEqual(mocks["admin_extend_service"].await_args.kwargs["extra_days"], 5)

    async def test_bulk_renew_requires_customer_services(self) -> None:
        await self.set_shop_permissions("dashboard")
        self.patched_service_ops()
        with patch("app.services.table_bulk.bulk_renew_users", AsyncMock(return_value=(1, 0))) as bulk:
            response = await self.post("/users/bulk-action", {"action": "renew", "ids": str(CUSTOMER_ID)})
        self.assertEqual(response.status_code, 303)
        bulk.assert_not_awaited()

    async def test_edit_page_hides_services_without_customer_services(self) -> None:
        await self.set_shop_permissions("dashboard")
        with patch(
            "app.services.bot_user_admin.list_service_snapshots", AsyncMock(return_value=[])
        ) as listed:
            response = await self.call(SHOP_SESSION, "GET", f"/users/{CUSTOMER_ID}/edit?fragment=1")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('id="user-services"', response.text)
        listed.assert_not_awaited()


class CustomerServiceActionsTests(RoutesHarness):
    def test_platform_owner_keeps_service_actions(self) -> None:
        self.assertTrue(may_manage_customer_services(OWNER_SESSION))

    def test_shop_staff_without_the_key_keeps_only_plan_assignment(self) -> None:
        staff = {"role": "reseller", "bot_user_id": SHOP_ID, "permissions": ["dashboard"], **FULL_PG_ROLE}
        actions = customer_service_actions(staff)
        self.assertFalse(may_manage_customer_services(staff))
        self.assertTrue(actions["create"])
        self.assertFalse(any(allowed for action, allowed in actions.items() if action != "create"))

    def test_shop_staff_with_the_key_keeps_its_panel_role_actions(self) -> None:
        staff = {
            "role": "reseller",
            "bot_user_id": SHOP_ID,
            "permissions": ["dashboard", "customer_services"],
            "pg_user_actions": {"update": True, "delete": False},
        }
        actions = customer_service_actions(staff)
        self.assertTrue(actions["update"])
        self.assertFalse(actions["delete"])
