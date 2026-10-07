"""Phase 4 — web harden: body limits, login Origin, PG SSRF fail-closed, safe errors."""

from __future__ import annotations

import inspect
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]


class CardWebhookBodyLimitTests(unittest.TestCase):
    def test_content_length_checked_before_body_buffer(self):
        src = (ROOT / "app/api/settlement_pages.py").read_text(encoding="utf-8")
        start = src.find("async def _card_auto_webhook_for_shop")
        self.assertGreater(start, 0)
        block = src[start : start + 2500]
        cl_at = block.find("content_length_ok")
        body_at = block.find("await request.body()")
        self.assertGreater(cl_at, 0)
        self.assertGreater(body_at, 0)
        self.assertLess(cl_at, body_at)
        self.assertIn("CARD_WEBHOOK_MAX_BODY_BYTES", block)
        self.assertIn("413", block)

    def test_constants_defined(self):
        from app.services.security_policy import (
            CARD_WEBHOOK_MAX_BODY_BYTES,
            MINIAPP_MAX_BODY_BYTES,
        )

        self.assertEqual(CARD_WEBHOOK_MAX_BODY_BYTES, 64 * 1024)
        self.assertEqual(MINIAPP_MAX_BODY_BYTES, 256 * 1024)


class MiniAppBodyLimitTests(unittest.TestCase):
    def test_buy_renew_use_bounded_json_reader(self):
        src = (ROOT / "app/api/miniapp_pages.py").read_text(encoding="utf-8")
        self.assertIn("async def _read_mini_json", src)
        self.assertIn("MINIAPP_MAX_BODY_BYTES", src)
        self.assertIn("content_length_ok", src)
        self.assertIn("await _read_mini_json(request)", src)
        # Direct request.json() on buy/renew would bypass the size gate.
        buy = src.find("@app.post(\"/api/mini/buy\")")
        renew = src.find("@app.post(\"/api/mini/renew\")")
        self.assertGreater(buy, 0)
        self.assertGreater(renew, 0)
        buy_block = src[buy:renew]
        renew_block = src[renew : renew + 2000]
        self.assertNotIn("await request.json()", buy_block)
        self.assertNotIn("await request.json()", renew_block)


class LoginOriginGuardTests(unittest.TestCase):
    def test_middleware_requires_origin_or_loopback_for_login(self):
        src = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        start = src.find("async def csrf_origin_guard")
        self.assertGreater(start, 0)
        block = src[start : start + 4500]
        self.assertIn('path_now == "/login"', block)
        self.assertIn("is_loopback_ip", block)
        self.assertIn("transport_peer_ip", block)
        # Login handled before the generic has_session Origin hard-require.
        login_at = block.find('path_now == "/login"')
        elif_session = block.find("elif has_session:")
        self.assertGreater(login_at, 0)
        self.assertGreater(elif_session, login_at)


class SafePgCandidatesTests(unittest.TestCase):
    def test_filters_out_metadata_urls(self):
        from app.services.security_policy import (
            UnsafePgUrlError,
            assert_safe_pg_base_url,
            safe_pg_api_base_candidates,
        )

        self.assertEqual(
            safe_pg_api_base_candidates(
                "http://169.254.169.254/", resolve_dns=False
            ),
            [],
        )
        self.assertEqual(
            safe_pg_api_base_candidates(
                "http://metadata.google.internal/", resolve_dns=False
            ),
            [],
        )
        # Userinfo is rejected on the raw URL before candidates are dialed.
        with self.assertRaises(UnsafePgUrlError):
            assert_safe_pg_base_url(
                "http://user:pass@evil.example/", resolve_dns=False
            )

    def test_keeps_private_lan_by_default(self):
        from app.services.security_policy import safe_pg_api_base_candidates

        cands = safe_pg_api_base_candidates(
            "http://10.0.0.8:8080/dashboard", resolve_dns=False
        )
        self.assertEqual(cands[0], "http://10.0.0.8:8080/dashboard")
        self.assertIn("http://10.0.0.8:8080", cands)

    def test_client_does_not_reintroduce_cleared_unsafe_settings(self):
        from app.services.pasarguard import PasarGuardClient

        settings = MagicMock()
        settings.pg_base_url = "http://169.254.169.254/"
        settings.pg_access_token = None
        settings.pg_username = ""
        settings.pg_password = ""

        with patch("app.services.pasarguard.get_settings", return_value=settings):
            client = PasarGuardClient()
        self.assertEqual(client.base_url, "")
        self.assertEqual(client._safe_base_seed(), "")
        self.assertEqual(client._safe_api_candidates(), [])

    def test_public_pg_helpers_fail_closed_on_unsafe(self):
        from app.services.pasarguard import public_pg_api_base, public_pg_sub_origin

        with patch("app.services.pasarguard._pg", None), patch(
            "app.services.pasarguard.get_settings"
        ) as gs:
            gs.return_value.pg_base_url = "http://169.254.169.254/meta"
            self.assertEqual(public_pg_api_base(), "")
            self.assertEqual(public_pg_sub_origin(), "")

    def test_public_pg_sub_origin_still_allows_lan(self):
        from app.services.pasarguard import public_pg_sub_origin

        with patch("app.services.pasarguard._pg", None), patch(
            "app.services.pasarguard.get_settings"
        ) as gs:
            gs.return_value.pg_base_url = "http://10.0.0.8:8080/MrClock"
            self.assertEqual(public_pg_sub_origin(), "http://10.0.0.8:8080")


class WebUserSafeErrorTests(unittest.TestCase):
    def test_redirect_msg_sanitizes_err(self):
        from app.api.app import _redirect_msg

        resp = _redirect_msg(
            "/finance",
            err='Traceback (most recent call last):\n  File "/app/x.py"\ntoken=123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ',
        )
        loc = resp.headers.get("location", "")
        self.assertIn("err=", loc)
        self.assertNotIn("Traceback", loc)
        self.assertNotIn("/app/x.py", loc)
        self.assertNotIn("123456:ABCDEF", loc)

    def test_finance_and_backup_use_user_safe_error(self):
        app_src = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        # Central path: _redirect_msg wraps err via user_safe_error.
        fn = inspect.getsource(
            __import__("app.api.app", fromlist=["_redirect_msg"])._redirect_msg
        )
        self.assertIn("user_safe_error", fn)

        backup = (ROOT / "app/api/backup_pages.py").read_text(encoding="utf-8")
        self.assertIn("user_safe_error(e)", backup)

        ux20 = (ROOT / "app/api/ux20_pages.py").read_text(encoding="utf-8")
        self.assertIn("user_safe_error", ux20)
        self.assertNotIn("quote(str(exc)", ux20)

        # Finance mutation handlers go through _redirect_msg (sanitized).
        self.assertIn('err=str(e)', app_src)  # still pass exceptions in
        # but flash goes through sanitizer — no raw quote(str(e)) on finance tabs
        finance_block_start = app_src.find('@app.post("/orders/{order_id}/approve")')
        finance_block_end = app_src.find('@app.post("/payments/{payment_id}/reject")')
        block = app_src[finance_block_start:finance_block_end]
        self.assertNotIn("quote(str(e))", block)


if __name__ == "__main__":
    unittest.main()
