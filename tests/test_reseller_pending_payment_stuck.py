"""Stuck reseller apply: pending_payment invisible to admin + blocks re-apply."""

from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[1]


class SourceContractTests(unittest.TestCase):
    def test_admin_list_includes_pending_payment(self):
        src = (ROOT / "app/bot/handlers/admin.py").read_text(encoding="utf-8")
        chunk = src.split("async def adm_resapp_list", 1)[1].split(
            "async def adm_resapp_view", 1
        )[0]
        self.assertIn("list_open_applications", chunk)
        self.assertIn("release_stale_pending_payment_apps", chunk)
        self.assertIn("PENDING_PAYMENT", chunk)
        self.assertNotIn(
            "status=ResellerApplicationStatus.AWAITING_APPROVAL.value",
            chunk,
        )

    def test_web_reject_available_for_pending_payment(self):
        html = (ROOT / "app/web/templates/reseller_applications.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("pending_payment", html)
        self.assertIn("pending_payment'%}reject", html.replace(" ", ""))
        self.assertIn("رد / لغو", html)

    def test_order_cancel_closes_reseller_app(self):
        src = (ROOT / "app/services/orders.py").read_text(encoding="utf-8")
        self.assertIn("cancel_application_for_order", src)
        cancel_fn = src.split("async def cancel_order", 1)[1].split(
            "async def reject_order", 1
        )[0]
        self.assertIn("cancel_application_for_order", cancel_fn)
        stale = src.split("async def cancel_stale_pending_orders", 1)[1].split(
            "async def cancel_stale_pending_for_settings", 1
        )[0]
        # Released through the shared helper (also used by the purge).
        self.assertIn("release_unpaid_order_holds", stale)
        holds = src.split("async def release_unpaid_order_holds", 1)[1].split("async def cancel_stale_pending_orders", 1)[0]
        self.assertIn("cancel_application_for_order", holds)

    def test_reseller_app_review_can_hide_approve(self):
        src = (ROOT / "app/bot/keyboards.py").read_text(encoding="utf-8")
        self.assertIn("allow_approve: bool = True", src)
        tree = ast.parse(src)
        found = False
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "reseller_app_review":
                kw = [a.arg for a in node.args.kwonlyargs]
                self.assertIn("allow_approve", kw)
                found = True
        self.assertTrue(found)


class ReleaseStaleAppsTests(unittest.IsolatedAsyncioTestCase):
    async def test_release_stale_cancels_orphan_pending_payment(self):
        from app.db.models import OrderStatus, ResellerApplicationStatus
        from app.services.resellers import release_stale_pending_payment_apps

        app = MagicMock()
        app.order_id = 9
        app.status = ResellerApplicationStatus.PENDING_PAYMENT.value
        app.admin_note = None

        order = MagicMock()
        order.status = OrderStatus.CANCELLED.value

        session = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = [app]
        session.execute = AsyncMock(return_value=result)
        session.get = AsyncMock(return_value=order)
        session.flush = AsyncMock()

        n = await release_stale_pending_payment_apps(session, user_id=1)
        self.assertEqual(n, 1)
        self.assertEqual(app.status, ResellerApplicationStatus.CANCELLED.value)
        session.flush.assert_awaited()

    async def test_release_keeps_live_pending_order(self):
        from app.db.models import OrderStatus, ResellerApplicationStatus
        from app.services.resellers import release_stale_pending_payment_apps

        app = MagicMock()
        app.order_id = 9
        app.status = ResellerApplicationStatus.PENDING_PAYMENT.value
        app.admin_note = None

        order = MagicMock()
        order.status = OrderStatus.PENDING.value

        session = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = [app]
        session.execute = AsyncMock(return_value=result)
        session.get = AsyncMock(return_value=order)
        session.flush = AsyncMock()

        n = await release_stale_pending_payment_apps(session, user_id=1)
        self.assertEqual(n, 0)
        self.assertEqual(app.status, ResellerApplicationStatus.PENDING_PAYMENT.value)
        session.flush.assert_not_awaited()

    async def test_cancel_application_for_order(self):
        from app.db.models import ResellerApplicationStatus
        from app.services.resellers import cancel_application_for_order

        order = MagicMock()
        order.note = "reseller_app:42"
        app = MagicMock()
        app.status = ResellerApplicationStatus.PENDING_PAYMENT.value
        app.admin_note = None

        session = AsyncMock()
        session.get = AsyncMock(return_value=app)
        session.flush = AsyncMock()

        out = await cancel_application_for_order(session, order, reason="test")
        self.assertIs(out, app)
        self.assertEqual(app.status, ResellerApplicationStatus.CANCELLED.value)


class BulkRejectPendingPaymentTests(unittest.TestCase):
    def test_bulk_allows_reject_pending_payment(self):
        src = (ROOT / "app/services/table_bulk_ext.py").read_text(encoding="utf-8")
        chunk = src.split("async def bulk_reseller_app_action", 1)[1].split(
            "async def bulk_revoke_resellers", 1
        )[0]
        self.assertIn("pending_payment", chunk)
        self.assertIn('app.status != "awaiting_approval"', chunk)


if __name__ == "__main__":
    unittest.main()
