"""Role-based Mini App — auth, persona, and UI wiring."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import unittest
import unittest.mock
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]


def _sign_init_data(bot_token: str, user: dict, *, auth_date: int | None = None) -> str:
    auth_date = int(auth_date if auth_date is not None else time.time())
    fields = {
        "auth_date": str(auth_date),
        "user": json.dumps(user, separators=(",", ":"), ensure_ascii=False),
    }
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class MiniAppAuthUnitTests(unittest.TestCase):
    def test_validate_accepts_fresh_signed_payload(self):
        from app.services.miniapp_auth import validate_webapp_init_data

        token = "123456:ABC-DEF"
        init = _sign_init_data(token, {"id": 42, "first_name": "A"})
        user = validate_webapp_init_data(init, bot_token=token)
        self.assertEqual(user["id"], 42)

    def test_validate_rejects_bad_hash(self):
        from fastapi import HTTPException

        from app.services.miniapp_auth import validate_webapp_init_data

        token = "123456:ABC-DEF"
        init = _sign_init_data(token, {"id": 1}) + "dead"
        with self.assertRaises(HTTPException) as ctx:
            validate_webapp_init_data(init, bot_token=token)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_persona_matrix(self):
        from types import SimpleNamespace

        from app.services.miniapp_auth import resolve_mini_persona

        admin = SimpleNamespace(role="admin", telegram_id=1)
        reseller = SimpleNamespace(role="reseller", telegram_id=2)
        user = SimpleNamespace(role="user", telegram_id=3)
        with unittest.mock.patch(
            "app.services.miniapp_auth.is_bot_platform_admin",
            side_effect=lambda u: getattr(u, "role", None) == "admin",
        ):
            self.assertEqual(resolve_mini_persona(admin), "admin")
            self.assertEqual(resolve_mini_persona(reseller), "reseller")
            self.assertEqual(resolve_mini_persona(user), "user")


class MiniAppSourceTests(unittest.TestCase):
    def test_static_js_escapes_and_safe_url(self):
        src = (ROOT / "app/web/static/miniapp.js").read_text(encoding="utf-8")
        self.assertIn("function esc(", src)
        self.assertIn("createTextNode", src)
        self.assertIn("function safeUrl(", src)
        self.assertNotIn("onclick=\"showSvc(", src)
        self.assertNotIn("tg.openLink(data.service.url)", src)

    def test_template_loads_panel_like_assets(self):
        html = (ROOT / "app/web/templates/miniapp.html").read_text(encoding="utf-8")
        self.assertIn("/static/miniapp.css", html)
        self.assertIn("/static/miniapp.js", html)
        self.assertIn("ma-nav", html)
        self.assertIn("role-badge", html)

    def test_css_matches_panel_brand_tokens(self):
        css = (ROOT / "app/web/static/miniapp.css").read_text(encoding="utf-8")
        self.assertIn("--brand: #f97316", css)
        self.assertIn("--background: #09090b", css)
        self.assertIn("Vazirmatn", css)

    def test_api_registered_via_module(self):
        app_src = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        self.assertIn("register_miniapp_pages", app_src)
        pages = (ROOT / "app/api/miniapp_pages.py").read_text(encoding="utf-8")
        self.assertIn("persona", pages)
        self.assertIn("_admin_ops_payload", pages)
        self.assertIn("_reseller_ops_payload", pages)
        self.assertIn('/api/mini/buy', pages)
        self.assertIn('/api/mini/renew', pages)
        self.assertIn('/api/mini/service/{service_id}/qr', pages)
        self.assertIn("list_activity", pages)
        self.assertIn("wallet_pay_enabled", pages)
        self.assertIn("_require_commerce", pages)
        self.assertIn("_owned_service_or_404", pages)
        self.assertIn("commerce_allowed", pages)
        self.assertIn("owner_reseller_id.is_(None)", pages)
        self.assertIn("auth=False", pages)

    def test_mobile_nav_and_actions(self):
        html = (ROOT / "app/web/templates/miniapp.html").read_text(encoding="utf-8")
        self.assertIn("ma-nav-wrap", html)
        js = (ROOT / "app/web/static/miniapp.js").read_text(encoding="utf-8")
        self.assertIn("data-checkout-buy", js)
        self.assertIn("data-checkout-renew", js)
        self.assertIn("data-qr", js)
        self.assertIn("data-copy", js)
        self.assertIn("data-topup", js)
        self.assertIn("/api/mini/order", js)
        self.assertIn("/api/mini/ops/pending-payments", js)
        self.assertIn("ICONS", js)
        self.assertIn("servicePeekHtml", js)
        self.assertIn("commerce_allowed", js)
        self.assertIn("function markCopied(", js)
        self.assertIn("کپی شد", js)
        # Link text must not be dumped into service cards
        self.assertNotIn("word-break:break-all", js)
        css = (ROOT / "app/web/static/miniapp.css").read_text(encoding="utf-8")
        self.assertIn("ma-nav-wrap", css)
        self.assertIn("svc-card", css)
        self.assertIn("meter", css)
        self.assertIn("svc-peek", css)
        self.assertIn("var(--brand)", css)
        self.assertIn("--bg-card", css)
        self.assertIn(".is-copied", css)
        # Panel language: no body glow / radial atmosphere
        self.assertNotIn("radial-gradient", css)

    def test_nav_includes_wallet(self):
        from app.api.miniapp_pages import _nav_for, commerce_allowed

        ids = [x["id"] for x in _nav_for("user")]
        self.assertEqual(ids, ["home", "services", "shop", "wallet", "support"])
        admin_ids = [x["id"] for x in _nav_for("admin")]
        self.assertEqual(admin_ids, ["home", "ops"])
        self.assertNotIn("shop", admin_ids)
        self.assertNotIn("wallet", admin_ids)
        self.assertNotIn("services", admin_ids)
        self.assertNotIn("support", admin_ids)
        reseller_ids = [x["id"] for x in _nav_for("reseller")]
        self.assertIn("ops", reseller_ids)
        self.assertIn("support", reseller_ids)
        self.assertFalse(commerce_allowed("admin"))
        self.assertTrue(commerce_allowed("user"))
        self.assertTrue(commerce_allowed("reseller"))

    def test_admin_buy_blocked_in_source(self):
        pages = (ROOT / "app/api/miniapp_pages.py").read_text(encoding="utf-8")
        buy = pages.split("async def mini_buy")[1].split("async def mini_renew")[0]
        renew = pages.split("async def mini_renew")[1]
        self.assertIn("_require_commerce_ready", buy)
        self.assertIn("_require_commerce_ready", renew)
        me = pages.split("async def mini_me")[1].split("async def mini_service")[0]
        self.assertIn("_empty_customer()", me)
        self.assertIn("commerce_allowed(persona)", me)

    def test_serialize_never_exposes_subscription_token(self):
        pages = (ROOT / "app/api/miniapp_pages.py").read_text(encoding="utf-8")
        ser = pages.split("def _serialize_service")[1].split("async def _enrich_services")[0]
        # Payload keys must not include the raw PG token
        self.assertNotIn('"subscription_token"', ser)
        self.assertNotIn("'subscription_token'", ser)
        self.assertIn("subscription_url", ser)

    def test_deep_link_helper(self):
        from app.config import Settings

        s = Settings.model_construct(
            public_base_url="https://bot.example.com",
            bot_token="",
        )
        self.assertTrue(s.miniapp_url.endswith("/miniapp/"))
        self.assertEqual(s.miniapp_deep_url("ops"), s.miniapp_url + "#ops")

    def test_csp_allows_telegram_for_miniapp(self):
        src = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        self.assertIn('path.startswith("/miniapp")', src)
        self.assertIn("https://telegram.org", src)
        self.assertIn("web.telegram.org", src)

    def test_admin_resellers_hub_offers_miniapp(self):
        src = (ROOT / "app/bot/handlers/reply_nav.py").read_text(encoding="utf-8")
        self.assertIn('view="ops"', src)
        self.assertIn("miniapp_inline_keyboard", src)

    def test_menu_button_webapp_sync(self):
        src = (ROOT / "app/bot/chat_menu.py").read_text(encoding="utf-8")
        self.assertIn("MenuButtonWebApp", src)
        self.assertIn("sync_telegram_menu_button", src)
        main = (ROOT / "app/main.py").read_text(encoding="utf-8")
        self.assertIn("sync_telegram_menu_button", main)


if __name__ == "__main__":
    unittest.main()
