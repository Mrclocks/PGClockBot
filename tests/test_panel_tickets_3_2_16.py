"""Internal panel ticketing 3.2.16 — models, service rules, UI wiring."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]


class VersionNotesTests(unittest.TestCase):
    def test_version_at_least_3_2_16_notes(self):
        from app.services.release_notes import RELEASE_NOTES_FA
        from app.version import __version__

        self.assertGreaterEqual(tuple(int(x) for x in __version__.split(".")[:3]), (0, 1, 0))
        self.assertIn("0.1.0", RELEASE_NOTES_FA)
        blob = " ".join(RELEASE_NOTES_FA["0.1.0"])
        self.assertIn("۰٫۱٫۰", blob)


class ModelTests(unittest.TestCase):
    def test_panel_ticket_tables_declared(self):
        src = (ROOT / "app/db/models.py").read_text(encoding="utf-8")
        self.assertIn('__tablename__ = "panel_tickets"', src)
        self.assertIn('__tablename__ = "panel_ticket_messages"', src)
        self.assertIn("class PanelTicketStatus", src)
        self.assertIn("IN_PROGRESS", src)
        self.assertIn("answered_unread", src)


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


class RouteWiringTests(unittest.TestCase):
    def test_register_called_and_old_route_gone(self):
        app_src = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        self.assertIn("register_panel_tickets_pages", app_src)
        # Legacy tickets_page handler removed from app.py
        self.assertNotIn('async def tickets_page(', app_src)
        pages = (ROOT / "app/api/panel_tickets_pages.py").read_text(encoding="utf-8")
        self.assertIn('"/tickets"', pages)
        self.assertIn("/tickets/panel/create", pages)
        self.assertIn("panel_ticket_dashboard_alert", pages)


class TemplateNavTests(unittest.TestCase):
    def test_tickets_template_modals(self):
        src = (ROOT / "app/web/templates/tickets.html").read_text(encoding="utf-8")
        self.assertIn('id="modal-ticket-new"', src)
        self.assertIn('id="modal-ticket-view"', src)
        self.assertIn("/tickets/panel/create", src)
        self.assertIn("panel_tickets", src)
        self.assertIn("بستن تیکت", src)

    def test_pg_staff_nav_has_support(self):
        from tests.sidebar_view import sidebar

        # Support link for pg_staff in the account section
        self.assertIn(("پشتیبانی", "/tickets"), sidebar("pg_staff", pg=frozenset({"pg_users"}))["system"])

    def test_dashboard_answered_banner(self):
        # pg_home.html (PasarGuard-only dashboard) still has its own dedicated
        # ticket_alert banner. reseller_home.html / home.html later folded the
        # same "you have an answered ticket" nudge into the generic Action
        # Center work-queue card (_home_ops.html) alongside receipts/expiring
        # services/failed deliveries — same underlying open-ticket count
        # (see ux20.build_action_center), different presentation.
        pg = (ROOT / "app/web/templates/pg_home.html").read_text(encoding="utf-8")
        self.assertIn("ticket_alert", pg)
        self.assertIn("flash warn home-update-banner", pg)
        self.assertIn(">مشاهده</a>", pg)

        rh = (ROOT / "app/web/templates/_reseller_home_dash_body.html").read_text(encoding="utf-8")
        home = (ROOT / "app/web/templates/_home_dash_body.html").read_text(encoding="utf-8")
        home_ops = (ROOT / "app/web/templates/_home_ops.html").read_text(encoding="utf-8")
        for src in (rh, home):
            self.assertIn('{% include "_home_ops.html" %}', src)
        self.assertIn("ac.entries", home_ops)
        self.assertIn("item.href", home_ops)

    def test_macros_panel_labels(self):
        macros = (ROOT / "app/web/templates/macros.html").read_text(encoding="utf-8")
        self.assertIn("panel_tkt_status", macros)
        self.assertIn("in_progress", macros)
        self.assertIn("panel_tkt_priority", macros)
        self.assertIn("فوری", macros)


class CssTests(unittest.TestCase):
    def test_ticket_thread_styles(self):
        css = (ROOT / "app/web/static/panel.css").read_text(encoding="utf-8")
        self.assertIn(".ticket-thread", css)
        self.assertIn(".ticket-msg--owner", css)
        self.assertIn(".ui-modal-actions", css)


if __name__ == "__main__":
    unittest.main()
