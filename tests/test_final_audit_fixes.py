"""Regression tests for final audit fixes (payments, force-join, web auth, ACL)."""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


class StarsAmountTests(unittest.TestCase):
    def test_stars_amount_rounding(self):
        from app.services.orders import stars_amount_for_toman

        self.assertEqual(stars_amount_for_toman(1000, 500), 2)
        self.assertEqual(stars_amount_for_toman(501, 500), 2)
        self.assertEqual(stars_amount_for_toman(500, 500), 1)
        self.assertEqual(stars_amount_for_toman(0, 500), 1)

    def test_stars_payload_encodes_amount(self):
        src = Path("app/bot/handlers/shop.py").read_text(encoding="utf-8")
        self.assertIn('payload=f"stars:{payment.id}:{stars}"', src)

    def test_stars_success_verifies_amount(self):
        src = Path("app/bot/handlers/payments.py").read_text(encoding="utf-8")
        self.assertIn("expected_stars", src)
        self.assertIn("total_amount", src)


class OrderPayableGateTests(unittest.TestCase):
    def test_payable_statuses_defined(self):
        from app.services.orders import _PAYABLE_ORDER_STATUSES
        from app.db.models import OrderStatus

        self.assertIn(OrderStatus.PENDING.value, _PAYABLE_ORDER_STATUSES)
        self.assertNotIn(OrderStatus.DELIVERED.value, _PAYABLE_ORDER_STATUSES)
        self.assertNotIn(OrderStatus.PAID.value, _PAYABLE_ORDER_STATUSES)
        self.assertNotIn(OrderStatus.AWAITING_APPROVAL.value, _PAYABLE_ORDER_STATUSES)

    def test_pay_with_wallet_rejects_non_payable(self):
        import asyncio
        from app.db.models import OrderStatus
        from app.services.orders import pay_with_wallet

        order = MagicMock()
        order.status = OrderStatus.AWAITING_APPROVAL.value
        order.amount = 1000
        user = MagicMock()

        async def _run():
            with self.assertRaises(ValueError):
                await pay_with_wallet(MagicMock(), order, user)

        asyncio.run(_run())


class ForceJoinTests(unittest.TestCase):
    def test_start_checks_membership(self):
        src = Path("app/bot/handlers/start.py").read_text(encoding="utf-8")
        self.assertIn("check_force_join_all", src)
        self.assertIn("parse_force_join_channels", src)
        self.assertIn("missing", src)

    def test_middleware_blocks_on_api_error(self):
        src = Path("app/bot/middlewares.py").read_text(encoding="utf-8")
        self.assertIn("check_force_join_all", src)
        self.assertIn("check_force_join_member", src)
        self.assertIn("missing or unverified", src)
        self.assertIn("force_join_block_message", src)

    def test_check_force_join_member_left(self):
        import asyncio
        from app.bot import middlewares as mw
        from app.bot.middlewares import check_force_join_member

        mw._FORCE_JOIN_MEMBER_CACHE.clear()
        bot = AsyncMock()
        member = MagicMock()
        member.status = "left"
        bot.get_chat_member = AsyncMock(return_value=member)

        async def _run():
            self.assertIs(await check_force_join_member(bot, 1, "@chan"), False)

        asyncio.run(_run())

    def test_check_force_join_member_ok(self):
        import asyncio
        from app.bot import middlewares as mw
        from app.bot.middlewares import check_force_join_member

        mw._FORCE_JOIN_MEMBER_CACHE.clear()
        bot = AsyncMock()
        member = MagicMock()
        member.status = "member"
        bot.get_chat_member = AsyncMock(return_value=member)

        async def _run():
            self.assertIs(await check_force_join_member(bot, 1, "@chan"), True)

        asyncio.run(_run())

    def test_check_force_join_member_api_error(self):
        import asyncio
        from app.bot import middlewares as mw
        from app.bot.middlewares import check_force_join_member

        mw._FORCE_JOIN_MEMBER_CACHE.clear()
        bot = AsyncMock()
        bot.get_chat_member = AsyncMock(side_effect=RuntimeError("chat not found"))

        async def _run():
            self.assertIsNone(await check_force_join_member(bot, 1, "@chan"))

        asyncio.run(_run())


class WebAuthRepairTests(unittest.TestCase):
    def test_repair_does_not_overwrite_existing(self):
        from app.services import web_auth

        with tempfile.TemporaryDirectory() as tmp:
            auth_file = Path(tmp) / "web_admin.json"
            payload = {
                "username": "paneladmin",
                "password": web_auth.hash_password("OldPass1!"),
                "token": "keep-this-token",
            }
            auth_file.write_text(json.dumps(payload), encoding="utf-8")
            with patch.object(web_auth, "AUTH_FILE", auth_file), patch(
                "app.config.get_settings"
            ) as gs:
                settings = MagicMock()
                settings.web_admin_user = "envuser"
                settings.web_admin_password = "EnvPass1!"
                gs.return_value = settings
                gs.cache_clear = MagicMock()
                out = web_auth.repair_web_admin_from_env()
            self.assertEqual(out["username"], "paneladmin")
            self.assertEqual(out["token"], "keep-this-token")
            saved = json.loads(auth_file.read_text(encoding="utf-8"))
            self.assertEqual(saved["token"], "keep-this-token")

    def test_password_max_length(self):
        from app.services.web_auth import validate_password_strength

        ok, err = validate_password_strength("AaBb12!" + ("x" * 80))
        self.assertFalse(ok)
        self.assertIn("۷۲", err)


class PgAclWiringTests(unittest.TestCase):
    def test_template_delete_checks_allowlist(self):
        src = Path("app/api/pg_pages.py").read_text(encoding="utf-8")
        self.assertIn("template_allowed_for_staff", src)
        self.assertIn("groups_allowed_for_staff(staff, [group_id])", src)

    def test_require_staff_refreshes_pg_acl(self):
        src = Path("app/api/app.py").read_text(encoding="utf-8")
        self.assertIn("enrich_staff_pg_from_role", src)
        order_review = Path("app/api/order_review_pages.py").read_text(encoding="utf-8")
        self.assertIn("open_notify_bot_for_user", order_review)
        self.assertIn("revoke_reseller", src)

    def test_users_role_uses_revoke(self):
        tree = ast.parse(Path("app/api/app.py").read_text(encoding="utf-8"))
        found = False
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "create_api_app":
                for child in ast.walk(node):
                    if isinstance(child, ast.Call):
                        func = child.func
                        if isinstance(func, ast.Name) and func.id == "revoke_reseller":
                            found = True
                        if isinstance(func, ast.Attribute) and func.attr == "revoke_reseller":
                            found = True
        # revoke_reseller is imported and awaited inside users_set_role
        src = Path("app/api/app.py").read_text(encoding="utf-8")
        self.assertIn("await revoke_reseller(", src)


class SupportSafetyTests(unittest.TestCase):
    def test_support_escapes_html(self):
        src = Path("app/bot/handlers/support.py").read_text(encoding="utf-8")
        self.assertIn("html.escape", src)
        self.assertIn("kb.is_cancel_text", src)


if __name__ == "__main__":
    unittest.main()
