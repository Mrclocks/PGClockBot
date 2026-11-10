"""Operations on a customer's service need a reason; the reason lands in the shop's own log."""

from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ShopStaffAction
from app.services.action_reasons import REASON_MIN, ReasonRequired, clean_reason, reason_from_form
from app.services.staff_audit import REASON_MAX
from tests.test_update_owner_only import OWNER_SESSION
from tests.test_customer_services_web_permission import (
    CUSTOMER_ID,
    SERVICE_ID,
    SHOP_ID,
    CustomerServicesWebPermissionTests,
)

EXTEND_PATH = f"/users/{CUSTOMER_ID}/services/{SERVICE_ID}/extend"
DELETE_PATH = f"/users/{CUSTOMER_ID}/services/{SERVICE_ID}/delete"


class CleanReasonTests(unittest.TestCase):
    def test_a_missing_or_short_reason_is_refused(self) -> None:
        for raw in (None, "", "  ", "ab", " a b "[: REASON_MIN - 1]):
            with self.assertRaises(ReasonRequired):
                clean_reason(raw)

    def test_whitespace_is_collapsed_and_length_bounded(self) -> None:
        self.assertEqual(clean_reason("  درخواست   مشتری \n"), "درخواست مشتری")
        self.assertEqual(len(clean_reason("ب" * 5000)), REASON_MAX)

    def test_a_confirm_dialog_reason_is_found_after_an_empty_placeholder(self) -> None:
        class Form(dict):
            def getlist(self, key):  # mimics a form with an empty and a filled value
                return ["", "تخلف از قوانین"] if key == "reason" else []

        self.assertEqual(reason_from_form(Form()), "تخلف از قوانین")


class ReasonedServiceActionTests(CustomerServicesWebPermissionTests):
    async def logged(self) -> list[ShopStaffAction]:
        async with AsyncSession(bind=self.engine) as session:
            return list((await session.scalars(select(ShopStaffAction))).all())

    async def test_extend_without_a_reason_changes_nothing(self) -> None:
        await self.set_shop_permissions("dashboard,customer_services")
        mocks = self.patched_service_ops()
        response = await self.post(EXTEND_PATH, {"extra_days": "5", "extra_gb": "0"})
        self.assertEqual(response.status_code, 303)
        self.assertIn("err=", response.headers["location"])
        mocks["admin_extend_service"].assert_not_awaited()
        self.assertEqual(await self.logged(), [])

    async def test_delete_without_a_reason_changes_nothing(self) -> None:
        await self.set_shop_permissions("dashboard,customer_services")
        mocks = self.patched_service_ops()
        response = await self.post(DELETE_PATH, {"reason": "x"})
        self.assertIn("err=", response.headers["location"])
        mocks["admin_delete_service"].assert_not_awaited()

    async def test_a_reasoned_extend_is_logged_for_the_acting_shop(self) -> None:
        await self.set_shop_permissions("dashboard,customer_services")
        mocks = self.patched_service_ops()
        response = await self.post(EXTEND_PATH, {"extra_days": "5", "extra_gb": "0", "reason": "جبران قطعی سرویس"})
        self.assertEqual(response.status_code, 303)
        mocks["admin_extend_service"].assert_awaited_once()
        rows = await self.logged()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].action, rows[0].target_type, rows[0].target_id), ("service_extend", "service", SERVICE_ID))
        self.assertEqual(rows[0].reason, "جبران قطعی سرویس")
        self.assertEqual(rows[0].shop_id, SHOP_ID)

    async def test_a_failed_operation_leaves_no_log_record(self) -> None:
        await self.set_shop_permissions("dashboard,customer_services")
        mocks = self.patched_service_ops()
        mocks["admin_delete_service"].side_effect = ValueError("سرویس پیدا نشد")
        await self.post(DELETE_PATH, {"reason": "درخواست مشتری"})
        self.assertEqual(await self.logged(), [])

    async def post_as_owner(self, path: str, data: dict):
        cookie = f"session={self.signer.dumps(OWNER_SESSION)}; csrf=token"
        headers = {"cookie": cookie, "x-csrf-token": "token", "origin": "http://panel.test"}
        return await self.client.post(path, headers=headers, data=data)

    async def test_a_bulk_block_without_a_reason_changes_nothing(self) -> None:
        with patch("app.services.table_bulk.bulk_toggle_block", AsyncMock(return_value=(1, 0))) as bulk:
            response = await self.post_as_owner(
                "/users/bulk-action", {"action": "block", "ids": str(CUSTOMER_ID), "reason": "x"}
            )
        self.assertIn("err=", response.headers["location"])
        bulk.assert_not_awaited()
        self.assertEqual(await self.logged(), [])

    async def test_a_reasoned_bulk_block_is_logged_once_with_the_count(self) -> None:
        with patch("app.services.table_bulk.bulk_toggle_block", AsyncMock(return_value=(1, 0))) as bulk:
            response = await self.post_as_owner(
                "/users/bulk-action", {"action": "block", "ids": str(CUSTOMER_ID), "reason": "ارسال هرزنامه"}
            )
        self.assertEqual(response.status_code, 303)
        bulk.assert_awaited_once()
        rows = await self.logged()
        self.assertEqual([(r.action, r.amount, r.reason) for r in rows], [("customer_block", 1, "ارسال هرزنامه")])


# The inherited permission tests run again in this module; keep only the new cases here.
del CustomerServicesWebPermissionTests

if __name__ == "__main__":
    unittest.main()
