"""Internal panel ticketing 3.2.16 — models, service rules, UI wiring."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


class ServiceRulesTests(unittest.TestCase):
    def test_actor_roles(self):
        from app.services.panel_tickets import actor_from_staff

        admin = actor_from_staff({"role": "admin", "username": "root"})
        self.assertTrue(admin["is_owner"])
        self.assertFalse(admin["is_opener"])

        res = actor_from_staff({"role": "reseller", "bot_user_id": 9, "username": "shop"})
        self.assertTrue(res["is_opener"])
        self.assertEqual(res["reseller_user_id"], 9)

        staff = actor_from_staff({"role": "pg_staff", "pg_staff_id": 3, "username": "sub"})
        self.assertTrue(staff["is_opener"])
        self.assertEqual(staff["pg_staff_id"], 3)

    def test_normalize_priority_status(self):
        from app.services.panel_tickets import normalize_priority, normalize_status

        self.assertEqual(normalize_priority("urgent"), "urgent")
        self.assertEqual(normalize_priority("nope"), "normal")
        self.assertEqual(normalize_status("in_progress"), "in_progress")
        self.assertIsNone(normalize_status("weird"))

    def test_opener_cannot_set_in_progress(self):
        import asyncio

        from app.db.models import PanelTicketStatus
        from app.services import panel_tickets as pt

        ticket = SimpleNamespace(
            id=1,
            status=PanelTicketStatus.OPEN.value,
            answered_unread=False,
            opener_reseller_user_id=5,
            opener_pg_staff_id=None,
            closed_at=None,
            updated_at=None,
        )

        async def _run():
            with patch.object(pt, "get_ticket", new=AsyncMock(return_value=ticket)):
                session = MagicMock()
                session.commit = AsyncMock()
                with self.assertRaises(PermissionError):
                    await pt.set_status(
                        session,
                        {"role": "reseller", "bot_user_id": 5, "username": "r"},
                        1,
                        status="in_progress",
                    )

        asyncio.run(_run())


class TemplateNavTests(unittest.TestCase):

    def test_pg_staff_nav_has_support(self):
        from tests.sidebar_view import sidebar

        # Support link for pg_staff in the account section
        self.assertIn(("پشتیبانی", "/tickets"), sidebar("pg_staff", pg=frozenset({"pg_users"}))["system"])


if __name__ == "__main__":
    unittest.main()
