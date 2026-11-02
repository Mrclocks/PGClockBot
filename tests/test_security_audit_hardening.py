"""Regression tests for the production security audit hardening pass."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.pg_access import map_pg_role_actions, staff_pg_action
from app.services.security_policy import (
    is_placeholder_bot_token,
    is_placeholder_password,
    request_host_allowed,
)


class PlaceholderCredentialTests(unittest.TestCase):
    def test_example_bot_token_rejected(self):
        self.assertTrue(is_placeholder_bot_token("123456:ABC-DEF"))
        self.assertTrue(is_placeholder_bot_token(""))
        self.assertFalse(is_placeholder_bot_token("123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"))

    def test_example_password_rejected(self):
        self.assertTrue(is_placeholder_password("Admin!234"))
        self.assertFalse(is_placeholder_password("Str0ng!Pass"))

    def test_env_example_has_empty_secrets(self):
        text = Path(".env.example").read_text(encoding="utf-8")
        self.assertIn('BOT_TOKEN=""', text)
        self.assertIn('WEB_ADMIN_PASSWORD=""', text)
        self.assertNotIn("Admin!234", text)


class SetupGateRotationTests(unittest.TestCase):
    def test_rotate_invalidates_previous(self):
        from app.services import setup_wizard as sw

        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            with (
                patch.object(sw, "DATA_DIR", data),
                patch.object(sw, "SETUP_GATE_FILE", data / "setup_gate.token"),
                patch.object(sw, "SETUP_GATE_META_FILE", data / "setup_gate.json"),
                patch.object(sw, "SETUP_FLAG", data / "setup_complete.flag"),
                patch.object(sw, "SETUP_IN_PROGRESS", data / "setup_in_progress.flag"),
                patch.object(sw, "is_setup_complete", return_value=False),
            ):
                a = sw.ensure_setup_gate_token()
                self.assertTrue(sw.setup_gate_ok(a))
                b = sw.rotate_setup_gate_token()
                self.assertNotEqual(a, b)
                self.assertFalse(sw.setup_gate_ok(a))
                self.assertTrue(sw.setup_gate_ok(b))


class PgActionMatrixTests(unittest.TestCase):
    def test_create_only_cannot_delete(self):
        role = {
            "is_owner": False,
            "permissions": {
                "templates": {"create": True, "read": True, "delete": False, "update": False},
                "groups": {"create": True, "update": False, "delete": False},
                "hosts": {"create": True, "update": False, "delete": False},
                "nodes": {"read": True, "reconnect": False},
            },
        }
        actions = map_pg_role_actions(role)
        self.assertTrue(actions["templates"]["create"])
        self.assertFalse(actions["templates"]["delete"])
        self.assertFalse(actions["nodes"]["reconnect"])

        staff = {"role": "pg_staff", "pg_actions": actions}
        self.assertTrue(staff_pg_action(staff, "templates", "create"))
        self.assertFalse(staff_pg_action(staff, "templates", "delete"))
        self.assertFalse(staff_pg_action(staff, "nodes", "reconnect"))

    def test_admin_has_all_actions(self):
        from app.services.pg_access import map_pg_role_actions

        staff = {
            "role": "admin",
            "pg_actions": map_pg_role_actions({"is_owner": True}),
            "pg_is_owner": True,
        }
        self.assertTrue(staff_pg_action(staff, "nodes", "reconnect"))
        self.assertTrue(staff_pg_action(staff, "hosts", "delete"))
        # Hybrid fail-closed without matrix
        self.assertFalse(staff_pg_action({"role": "admin"}, "nodes", "reconnect"))


class PaymentTenancyTests(unittest.IsolatedAsyncioTestCase):
    async def test_reseller_cannot_review_wallet_topup(self):
        from app.db.models import Role
        from app.services.resellers import reseller_can_review_payment

        reviewer = SimpleNamespace(role=Role.RESELLER.value, telegram_id=1, id=10)
        payment = SimpleNamespace(is_wallet_topup=True, order_id=None, user_id=99)
        session = AsyncMock()
        with patch(
            "app.services.users.current_shop_reseller_id",
            return_value=10,
        ), patch(
            "app.services.reseller_access.resolve_reseller_owner_id",
            new=AsyncMock(return_value=10),
        ):
            ok = await reseller_can_review_payment(session, reviewer, payment)
        self.assertFalse(ok)

    async def test_reseller_cannot_review_foreign_shop_order(self):
        from app.db.models import Role
        from app.services.resellers import reseller_can_review_payment

        reviewer = SimpleNamespace(role=Role.RESELLER.value, telegram_id=1, id=10)
        payment = SimpleNamespace(is_wallet_topup=False, order_id=5, user_id=99)
        order = SimpleNamespace(id=5, reseller_id=999)  # different shop
        session = AsyncMock()
        session.get = AsyncMock(return_value=order)
        profile = SimpleNamespace(bot_permissions="payments", web_permissions="payments", is_active=True)
        with patch(
            "app.services.users.current_shop_reseller_id",
            return_value=10,
        ), patch(
            "app.services.reseller_access.resolve_reseller_owner_id",
            new=AsyncMock(return_value=10),
        ), patch(
            "app.services.resellers.get_reseller_profile",
            new=AsyncMock(return_value=profile),
        ), patch(
            "app.services.resellers.has_bot_perm",
            return_value=True,
        ):
            ok = await reseller_can_review_payment(session, reviewer, payment)
        self.assertFalse(ok)


class CsrfOriginTests(unittest.TestCase):
    def test_same_host_allowed(self):
        self.assertTrue(request_host_allowed("example.com", "https://example.com/path"))
        self.assertFalse(request_host_allowed("example.com", "https://evil.com/"))


class TicketAttachmentPathTests(unittest.TestCase):
    def test_private_store_and_traversal_blocked(self):
        from app.services import panel_tickets as pt

        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            priv = data / "private" / "tickets"
            priv.mkdir(parents=True)
            f = priv / "t1_abc.pdf"
            f.write_bytes(b"%PDF")
            with patch.object(pt, "DATA_DIR", data):
                self.assertEqual(pt.resolve_ticket_attachment_path("private/tickets/t1_abc.pdf"), f.resolve())
                self.assertIsNone(pt.resolve_ticket_attachment_path("../private/tickets/t1_abc.pdf"))
                self.assertIsNone(pt.resolve_ticket_attachment_path("uploads/../private/tickets/t1_abc.pdf"))


class MiniAppInitDataSourceTests(unittest.TestCase):
    def test_query_string_initdata_removed(self):
        auth = Path("app/services/miniapp_auth.py").read_text(encoding="utf-8")
        pages = Path("app/api/miniapp_pages.py").read_text(encoding="utf-8")
        self.assertIn("X-Telegram-Init-Data", auth)
        self.assertNotIn('query_params.get("initData"', auth)
        self.assertNotIn('query_params.get("initData"', pages)
        self.assertIn("is_blocked", auth)
        self.assertIn("assert_mini_force_join", auth)


class WebhookCompareDigestTests(unittest.TestCase):
    def test_uses_compare_digest(self):
        src = Path("app/main.py").read_text(encoding="utf-8")
        self.assertIn("compare_digest", src)
        self.assertIn("WEBHOOK_MAX_BODY_BYTES", src)


class CertbotSudoHardeningTests(unittest.TestCase):
    def test_no_raw_certbot_in_sudoers_templates(self):
        ctl = Path("app/services/service_control.py").read_text(encoding="utf-8")
        self.assertNotIn("/usr/bin/certbot, /bin/certbot", ctl)
        sh = Path("pgclock.sh").read_text(encoding="utf-8")
        self.assertNotIn("/usr/bin/certbot, /bin/certbot", sh)
        helper = Path("scripts/pgclockbot-ctl").read_text(encoding="utf-8")
        self.assertIn("certbot-certonly", helper)


class PasswordByteLimitTests(unittest.TestCase):
    def test_rejects_over_72_bytes(self):
        from app.services.web_auth import validate_password_strength

        # 73 ascii bytes with required character classes
        # Over 72 UTF-8 bytes (PasarGuard limit) — must fail
        long_pw = "AaBb12!" + ("x" * 70)
        ok, err = validate_password_strength(long_pw)
        self.assertFalse(ok)
        self.assertIn("۷۲", err)


if __name__ == "__main__":
    unittest.main()
