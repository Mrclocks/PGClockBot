"""3.7.5 — sync repair UX, login restart flash, limited-admin get_admin."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]


class Version375Tests(unittest.TestCase):
    def test_version(self):
        from app.version import __version__

        self.assertEqual(__version__, "3.7.5")
        self.assertEqual((ROOT / "VERSION").read_text(encoding="utf-8").strip(), "3.7.5")
        notes = (ROOT / "app/services/release_notes.py").read_text(encoding="utf-8")
        self.assertIn('"3.7.5"', notes)


class LoginRestartFlashTests(unittest.TestCase):
    def test_banner_only_when_restarting_param(self):
        html = (ROOT / "app/web/templates/login.html").read_text(encoding="utf-8")
        self.assertIn("request.query_params.get('restarting')", html)
        # Must not leave a always-rendered .flash.warn that CSS display:flex unhides
        self.assertNotIn(
            'class="flash warn" style="margin: 0" {% if not request.query_params.get(\'restarting\') %}hidden{% endif %}',
            html,
        )

    def test_css_respects_flash_hidden(self):
        css = (ROOT / "app/web/static/panel.css").read_text(encoding="utf-8")
        self.assertIn(".flash[hidden]", css)
        self.assertIn("display: none !important", css)


class NestedRepairFormTests(unittest.TestCase):
    def test_reseller_edit_repair_not_nested(self):
        html = (ROOT / "app/web/templates/reseller_edit.html").read_text(encoding="utf-8")
        # Repair action must appear after the edit form closes
        edit_close = html.index('action="/resellers/{{ user.id }}/edit"')
        # Find the closing of that form roughly by looking for repair action position
        repair = html.index("repair-pg-credentials")
        # Between edit open and repair there should be a </form> that closes edit
        chunk = html[edit_close:repair]
        self.assertIn("</form>", chunk)
        # Confirm dialog must not ask for reason
        repair_form = html[repair - 200 : repair + 250]
        self.assertNotIn("data-confirm-reason", repair_form)


class LimitedAdminGetAdminTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_admin_uses_current_for_login_user(self):
        from app.services.pasarguard import PasarGuardClient

        client = PasarGuardClient(username="shop1", password="Aa1!bbbb")
        client.request = AsyncMock(
            return_value={"username": "shop1", "total_users": 4, "used_traffic": 0}
        )
        admin = await client.get_admin("shop1")
        self.assertEqual(admin["username"], "shop1")
        client.request.assert_awaited()
        args = client.request.await_args
        self.assertEqual(args.args[0], "GET")
        self.assertEqual(args.args[1], "/api/admin")

    async def test_overview_uses_current_admin_fallback(self):
        from app.services.pg_overview import build_reseller_pg_overview

        pg = MagicMock()
        pg.get_admin = AsyncMock(return_value=None)
        pg.get_current_admin = AsyncMock(
            return_value={
                "username": "shop1",
                "total_users": 2,
                "used_traffic": 0,
                "status": "active",
            }
        )
        pg.get_users = AsyncMock(return_value={"users": []})
        with patch(
            "app.services.pasarguard.get_pg_for_staff",
            new=AsyncMock(return_value=(pg, False)),
        ):
            out = await build_reseller_pg_overview(
                {
                    "role": "reseller",
                    "pg_admin_username": "shop1",
                    "bot_user_id": 9,
                    "pg_client_ready": True,
                },
                session=AsyncMock(),
            )
        self.assertTrue(out["ready"])
        pg.get_current_admin.assert_awaited()


class RepairNoReasonTests(unittest.TestCase):
    def test_pg_admins_repair_confirm_has_no_reason(self):
        html = (ROOT / "app/web/templates/pg_admins.html").read_text(encoding="utf-8")
        idx = 0
        while True:
            i = html.find("repair-pg-credentials", idx)
            if i < 0:
                break
            chunk = html[max(0, i - 180) : i + 120]
            self.assertNotIn("data-confirm-reason", chunk)
            idx = i + 1


if __name__ == "__main__":
    unittest.main()
