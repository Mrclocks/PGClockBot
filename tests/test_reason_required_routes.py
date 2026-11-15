"""Every web operation that needs a reason refuses to run, and touches nothing, without one."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch
from urllib.parse import unquote

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from tests.test_customer_services_web_permission import (
    CUSTOMER_ID,
    FULL_PG_ROLE,
    SERVICE_ID,
    CustomerServicesWebPermissionTests,
)
from tests.test_update_owner_only import OWNER_SESSION

OWNER_WITH_PG = {**OWNER_SESSION, "pg_permissions": ["pg_users"]}
SERVICE_BASE = f"/users/{CUSTOMER_ID}/services/{SERVICE_ID}"
PANEL_USER = 5
# (path, extra form fields): every route that must carry a reason. Add new reasoned routes here.
REASONED_ROUTES = (
    (f"{SERVICE_BASE}/renew", {"plan_id": "3"}),
    (f"{SERVICE_BASE}/extend", {"extra_days": "5", "extra_gb": "0"}),
    (f"{SERVICE_BASE}/delete", {}),
    (f"/users/{CUSTOMER_ID}/quick-renew", {}),
    (f"/pg/users/{PANEL_USER}/disable", {}),
    (f"/pg/users/{PANEL_USER}/enable", {}),
    (f"/pg/users/{PANEL_USER}/reset", {}),
    (f"/pg/users/{PANEL_USER}/revoke", {}),
    (f"/pg/users/{PANEL_USER}/delete", {}),
    ("/users/bulk-action", {"action": "block", "ids": str(CUSTOMER_ID)}),
    ("/users/bulk-action", {"action": "unblock", "ids": str(CUSTOMER_ID)}),
    ("/users/bulk-action", {"action": "renew", "ids": str(CUSTOMER_ID)}),
    ("/users/bulk-action", {"action": "delete", "ids": str(CUSTOMER_ID)}),
    ("/pg/users/bulk-action", {"action": "disable", "ids": str(PANEL_USER)}),
    ("/pg/users/bulk-action", {"action": "delete", "ids": str(PANEL_USER)}),
)
ALL_ACTIONS = {"create": True, "read": True, "update": True, "delete": True, "disable": True, "enable": True, "reset_usage": True, "revoke_sub": True}
SERVICE_OPS = ("admin_renew_service", "admin_extend_service", "admin_delete_service", "quick_renew_user")


class ReasonRequiredRoutesTests(CustomerServicesWebPermissionTests):
    async def post_as_owner(self, path: str, data: dict):
        cookie = f"session={self.signer.dumps(OWNER_WITH_PG)}; csrf=token"
        headers = {"cookie": cookie, "x-csrf-token": "token", "origin": "http://panel.test"}
        return await self.client.post(path, headers=headers, data=data)

    async def post_for(self, path: str, data: dict):
        """Customer routes belong to the shop that owns the customer; panel and bulk routes to the owner."""
        shop_route = path.startswith("/users/") and not path.startswith("/users/bulk")
        return await (self.post(path, data) if shop_route else self.post_as_owner(path, data))

    async def test_every_reasoned_route_refuses_a_missing_or_short_reason_and_changes_nothing(self) -> None:
        await self.set_shop_permissions("dashboard,customer_services")
        mocks = {
            name: AsyncMock()
            for name in (*SERVICE_OPS, "bulk_toggle_block", "bulk_renew_users", "bulk_delete_users", "bulk_pg_user_action")
        }
        patchers = [
            patch(f"app.services.bot_user_admin.{name}", mocks[name]) for name in SERVICE_OPS[:3]
        ] + [
            patch("app.services.users_quick.quick_renew_user", mocks["quick_renew_user"]),
            patch("app.services.table_bulk.bulk_toggle_block", mocks["bulk_toggle_block"]),
            patch("app.services.table_bulk.bulk_renew_users", mocks["bulk_renew_users"]),
            patch("app.services.table_bulk.bulk_delete_users", mocks["bulk_delete_users"]),
            patch("app.services.table_bulk_ext.bulk_pg_user_action", mocks["bulk_pg_user_action"]),
            patch("app.services.pg_access.staff_user_actions", return_value=FULL_PG_ROLE["pg_user_actions"]),
            patch("app.api.user_pages.staff_user_actions", return_value=FULL_PG_ROLE["pg_user_actions"]),
            patch("app.api.pg_pages.staff_user_actions", return_value=ALL_ACTIONS),
            patch("app.services.authz.can_pg_page", return_value=True),
            patch("app.api.pg_pages._staff_pg", AsyncMock(side_effect=AssertionError("panel must not be reached"))),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        for path, fields in REASONED_ROUTES:
            for reason in ({}, {"reason": "x"}, {"reason": "  "}):
                response = await self.post_for(path, {**fields, **reason})
                location = unquote(response.headers.get("location", ""))
                self.assertEqual(response.status_code, 303, path)
                self.assertIn("err=", location, path)
                self.assertIn("علت", location, path)
        for name, mock in mocks.items():
            mock.assert_not_awaited()


del CustomerServicesWebPermissionTests
