"""Mini App security audit — authz, isolation, no leakage."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
PAGES = (ROOT / "app/api/miniapp_pages.py").read_text(encoding="utf-8")
AUTH = (ROOT / "app/services/miniapp_auth.py").read_text(encoding="utf-8")
ORDERS = (ROOT / "app/services/orders.py").read_text(encoding="utf-8")
JS = (ROOT / "app/web/static/miniapp.js").read_text(encoding="utf-8")


class MiniAppSecuritySourceTests(unittest.TestCase):
    def test_blocked_users_rejected(self):
        self.assertIn("is_blocked", AUTH)
        self.assertIn("دسترسی شما مسدود شده است", AUTH)

    def test_force_join_on_all_api_loads(self):
        """End-users must pass force-join before any Mini App API (bot parity)."""
        load = AUTH.split("async def load_mini_user")[1].split("def resolve_mini_persona")[0]
        self.assertIn("await assert_mini_force_join", load)
        self.assertIn("assert_miniapp_feature_enabled", load)

    def test_force_join_gate_for_commerce(self):
        self.assertIn("assert_mini_force_join", AUTH)
        self.assertIn("check_force_join_all", AUTH)
        self.assertIn("_require_commerce_ready", PAGES)
        buy = PAGES.split("async def mini_buy")[1].split("async def mini_renew")[0]
        renew = PAGES.split("async def mini_renew")[1]
        self.assertIn("await _require_commerce_ready", buy)
        self.assertIn("await _require_commerce_ready", renew)

    def test_admin_has_no_commerce_nav_or_payload(self):
        from app.api.miniapp_pages import _nav_for, commerce_allowed

        self.assertFalse(commerce_allowed("admin"))
        self.assertEqual([x["id"] for x in _nav_for("admin")], ["home", "ops"])
        me = PAGES.split("async def mini_me")[1].split("async def mini_service")[0]
        self.assertIn("_empty_customer()", me)

    def test_service_response_has_no_raw_pg_info(self):
        svc = PAGES.split("async def mini_service")[1].split("async def mini_service_qr")[0]
        self.assertNotIn('"info": info', svc)
        self.assertNotIn("'info': info", svc)
        self.assertIn("_serialize_service", svc)

    def test_serialize_allowlist_only(self):
        ser = PAGES.split("def _serialize_service")[1].split("async def _enrich_services")[0]
        for leak in (
            '"subscription_token"',
            "'subscription_token'",
            '"links"',
            '"inbounds"',
            '"proxy_settings"',
            '"note"',
        ):
            self.assertNotIn(leak, ser)
        self.assertIn("upstream_unavailable", ser)

    def test_catalog_platform_only(self):
        self.assertIn("Plan.owner_reseller_id.is_(None)", PAGES)
        buy = PAGES.split("async def mini_buy")[1].split("async def mini_renew")[0]
        self.assertIn("owner_reseller_id is not None", buy)

    def test_ownership_helper_on_service_paths(self):
        self.assertIn("_owned_service_or_404", PAGES)
        for name in ("mini_service", "mini_service_qr", "mini_renew"):
            chunk = PAGES.split(f"async def {name}")[1].split("async def ")[0]
            self.assertIn("_owned_service_or_404", chunk)

    def test_pay_with_wallet_asserts_order_owner(self):
        fn = ORDERS.split("async def pay_with_wallet")[1].split("async def mark_order_free_paid")[0]
        self.assertIn("سفارش متعلق به این کاربر نیست", fn)
        self.assertIn("order.user_id", fn)

    def test_buy_renew_no_exception_text_leak(self):
        buy = PAGES.split("async def mini_buy")[1].split("async def mini_renew")[0]
        renew = PAGES.split("async def mini_renew")[1]
        self.assertIn('raise HTTPException(500, "خرید ناموفق")', buy)
        self.assertIn('raise HTTPException(500, "تمدید ناموفق")', renew)
        self.assertNotIn("HTTPException(500, str(exc)", buy)
        self.assertNotIn("HTTPException(500, str(exc)", renew)
        self.assertNotIn("HTTPException(400, str(exc)", buy)
        self.assertNotIn("HTTPException(400, str(exc)", renew)
        self.assertIn("_safe_client_message", buy)
        self.assertIn("_safe_client_message", renew)

    def test_initdata_header_only_and_capped(self):
        self.assertIn("X-Telegram-Init-Data", AUTH)
        self.assertNotIn("query_params.get", AUTH)
        self.assertIn("hmac.compare_digest", AUTH)
        self.assertIn("_MAX_INIT_DATA_CHARS", AUTH)

    def test_subscription_info_auth_false(self):
        self.assertIn("auth=False", PAGES)
        self.assertIn("subscription_info", PAGES)

    def test_reseller_ops_fail_closed_on_foreign_profile(self):
        fn = PAGES.split("async def _reseller_ops_payload")[1].split("def register_miniapp_pages")[0]
        self.assertIn("profile.user_id", fn)
        self.assertIn("user.id", fn)

    def test_feature_gate_on_page_and_api(self):
        self.assertIn("assert_miniapp_feature_enabled", AUTH)
        self.assertIn("assert_miniapp_feature_enabled()", PAGES)

    def test_js_panel_path_and_url_hardening(self):
        self.assertIn('p.startsWith("/")', JS)
        self.assertIn('p.startsWith("//")', JS)
        self.assertIn("safeUrl(s.subscription_url", JS)

    def test_copy_button_flashes_copied_label(self):
        self.assertIn("function markCopied(", JS)
        self.assertIn("کپی شد", JS)
        self.assertIn("is-copied", JS)
        self.assertNotIn("لینک کپی شد", JS)

    def test_panel_open_uses_panel_base_not_public_url(self):
        self.assertIn("_mini_panel_base", PAGES)
        self.assertIn("public_panel_base_url", PAGES)
        self.assertNotIn("public_base_url", PAGES)
        me = PAGES.split("async def mini_me")[1].split("async def mini_service")[0]
        self.assertIn("_mini_panel_base()", me)
        admin = PAGES.split("async def _admin_ops_payload")[1].split(
            "async def _reseller_ops_payload"
        )[0]
        self.assertIn("_mini_panel_base()", admin)
        self.assertNotIn("public_base_url", admin)

    def test_serialize_status_plain_no_emoji_dot(self):
        ser = PAGES.split("def _serialize_service")[1].split("async def _enrich_services")[0]
        self.assertIn("status_label_plain", ser)
        self.assertNotIn("status_label(status_raw)", ser)


class MiniAppSafeClientMessageTests(unittest.TestCase):
    def test_blocks_english_internal(self):
        from app.api.miniapp_pages import _safe_client_message

        self.assertEqual(
            _safe_client_message(
                ValueError("service has no panel user"), fallback="خرید ناموفق"
            ),
            "خرید ناموفق",
        )
        self.assertEqual(
            _safe_client_message(ValueError("plan missing"), fallback="x"),
            "x",
        )

    def test_keeps_short_persian(self):
        from app.api.miniapp_pages import _safe_client_message

        msg = "موجودی کیف پول کافی نیست"
        self.assertEqual(_safe_client_message(ValueError(msg), fallback="x"), msg)


class MiniAppPanelBaseTests(unittest.TestCase):
    def test_uses_live_panel_url_not_webhook_host(self):
        from app.api.miniapp_pages import _mini_panel_base

        with patch(
            "app.services.ssl_certs.public_panel_base_url",
            return_value="http://10.0.0.5:9000/",
        ):
            self.assertEqual(_mini_panel_base(), "http://10.0.0.5:9000")


class MiniAppSerializeLeakTests(unittest.TestCase):
    def test_raw_upstream_error_stripped(self):
        from app.api.miniapp_pages import _serialize_service

        svc = SimpleNamespace(
            id=1,
            pg_username="u1",
            subscription_url="https://example.com/sub/abc",
            plan_id=2,
        )
        out = _serialize_service(
            svc, {"error": "Connection refused to 10.0.0.5:443", "status": "active"}
        )
        self.assertIsNone(out["error"])
        out2 = _serialize_service(svc, {"error": "upstream_unavailable"})
        self.assertEqual(out2["error"], "upstream_unavailable")

    def test_status_fa_has_no_emoji_circle(self):
        from app.api.miniapp_pages import _serialize_service

        svc = SimpleNamespace(
            id=1,
            pg_username="u1",
            subscription_url="https://example.com/sub/abc",
            plan_id=2,
        )
        out = _serialize_service(svc, {"status": "active"})
        self.assertEqual(out["status_fa"], "فعال")
        self.assertNotIn("🟢", out["status_fa"])
        self.assertNotIn("🔴", _serialize_service(svc, {"status": "disabled"})["status_fa"])


class MiniAppOwnedServiceUnitTests(unittest.TestCase):
    def test_owned_service_rejects_foreign(self):
        from fastapi import HTTPException

        from app.api.miniapp_pages import _owned_service_or_404

        user = SimpleNamespace(id=1)
        foreign = SimpleNamespace(bot_user_id=2, id=9)
        with self.assertRaises(HTTPException) as ctx:
            _owned_service_or_404(foreign, user)
        self.assertEqual(ctx.exception.status_code, 404)
        own = SimpleNamespace(bot_user_id=1, id=3)
        self.assertIs(_owned_service_or_404(own, user), own)

    def test_commerce_matrix(self):
        from unittest.mock import patch

        from app.api.miniapp_pages import _require_commerce, commerce_allowed
        from fastapi import HTTPException

        self.assertTrue(commerce_allowed("user"))
        self.assertTrue(commerce_allowed("reseller"))
        self.assertFalse(commerce_allowed("admin"))
        # Live ADMIN_IDS member → admin persona → no commerce.
        with patch("app.services.miniapp_auth.get_settings") as gs:
            gs.return_value.admin_ids = [1]
            with self.assertRaises(HTTPException) as ctx:
                _require_commerce(SimpleNamespace(role="admin", telegram_id=1))
            self.assertEqual(ctx.exception.status_code, 403)
        # Sticky role=admin without ADMIN_IDS is treated as end-user (demoted path).
        with patch("app.services.miniapp_auth.get_settings") as gs:
            gs.return_value.admin_ids = []
            self.assertEqual(
                _require_commerce(SimpleNamespace(role="admin", telegram_id=99)),
                "user",
            )


class MiniAppBlockedUserUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_user_raises_403(self):
        from fastapi import HTTPException
        from starlette.requests import Request

        from app.services.miniapp_auth import load_mini_user

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/api/mini/me",
            "raw_path": b"/api/mini/me",
            "query_string": b"",
            "headers": [(b"x-telegram-init-data", b"dummy")],
            "client": ("127.0.0.1", 123),
            "server": ("test", 443),
        }
        request = Request(scope)
        blocked = SimpleNamespace(id=1, telegram_id=42, is_blocked=True, role="user")
        session = AsyncMock()
        result = SimpleNamespace(scalar_one_or_none=lambda: blocked)
        session.execute = AsyncMock(return_value=result)

        with patch(
            "app.services.miniapp_auth.validate_webapp_init_data",
            return_value={"id": 42},
        ), patch(
            "app.services.miniapp_auth.init_data_from_request",
            return_value="dummy",
        ), patch(
            "app.services.miniapp_auth.assert_miniapp_feature_enabled",
        ):
            with self.assertRaises(HTTPException) as ctx:
                await load_mini_user(session, request)
        self.assertEqual(ctx.exception.status_code, 403)


class MiniAppFeatureGateUnitTests(unittest.TestCase):
    def test_disabled_raises_404(self):
        from fastapi import HTTPException

        from app.services.miniapp_auth import assert_miniapp_feature_enabled

        with patch("app.services.miniapp_auth.get_settings") as gs:
            gs.return_value.miniapp_enabled = False
            with self.assertRaises(HTTPException) as ctx:
                assert_miniapp_feature_enabled()
            self.assertEqual(ctx.exception.status_code, 404)


class MiniAppPayWalletOwnerUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_foreign_order(self):
        from app.services.orders import pay_with_wallet

        order = SimpleNamespace(
            id=1,
            user_id=10,
            status="pending",
            amount=1000,
            note=None,
            service_id=None,
            plan_id=None,
        )
        user = SimpleNamespace(id=99, wallet_balance=5000)
        session = AsyncMock()
        with self.assertRaises(ValueError) as ctx:
            await pay_with_wallet(session, order, user)
        self.assertIn("متعلق", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
