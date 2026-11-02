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
    # Escaping, link safety, copy feedback and actions are behavior-tested in
    # tests/miniapp_app.test.cjs (run by tests/test_miniapp_frontend.py).

    def test_template_loads_scripts_in_dependency_order(self):
        from jinja2 import Environment, FileSystemLoader, select_autoescape

        env = Environment(loader=FileSystemLoader(ROOT / "app/web/templates"), autoescape=select_autoescape())
        html = env.get_template("miniapp.html").render(app_version="t")
        order = [html.index(path) for path in (
            "/static/catalog_price.js", "/static/miniapp/core.js",
            "/static/miniapp/render.js", "/static/miniapp/app.js",
        )]
        self.assertEqual(order, sorted(order))
        self.assertIn("/static/miniapp.css", html)
        for element_id in ('id="root"', 'id="nav"', 'id="sheet"', 'id="sheet-body"', 'id="sheet-close"'):
            self.assertIn(element_id, html)
        for name in ("core.js", "render.js", "app.js"):
            self.assertTrue((ROOT / "app/web/static/miniapp" / name).is_file())

    def test_a_blocked_telegram_sdk_cannot_hold_the_page(self):
        from html.parser import HTMLParser

        from jinja2 import Environment, FileSystemLoader, select_autoescape

        class Scripts(HTMLParser):
            def __init__(self) -> None:
                super().__init__()
                self.tags: list[dict] = []

            def handle_starttag(self, tag, attrs):
                if tag == "script":
                    self.tags.append(dict(attrs))

        env = Environment(loader=FileSystemLoader(ROOT / "app/web/templates"), autoescape=select_autoescape())
        parser = Scripts()
        parser.feed(env.get_template("miniapp.html").render(app_version="t"))
        srcs = [t.get("src", "") for t in parser.tags]
        sdk = next(t for t in parser.tags if "telegram-web-app.js" in t.get("src", ""))
        self.assertIn("async", sdk)
        self.assertEqual(sdk.get("id"), "tg-sdk")
        launch = next(i for i, s in enumerate(srcs) if "/static/miniapp/launch.js" in s)
        # Right after the SDK tag and before every app script, so it reads the address first.
        self.assertEqual(launch, srcs.index(sdk["src"]) + 1)
        self.assertTrue(all(launch < i for i, s in enumerate(srcs) if "/static/miniapp/" in s and i != launch))
        self.assertNotIn("defer", parser.tags[launch])
        self.assertNotIn("async", parser.tags[launch])

    def test_css_matches_panel_brand_tokens(self):
        css = (ROOT / "app/web/static/miniapp.css").read_text(encoding="utf-8")
        self.assertIn("--brand: #f97316", css)
        self.assertIn("--background: #09090b", css)
        self.assertIn("Vazirmatn", css)

    def test_api_routes_registered(self):
        from app.api.app import create_api_app

        paths = {getattr(route, "path", "") for route in create_api_app().routes}
        for path in (
            "/miniapp", "/api/mini/me", "/api/mini/services", "/api/mini/service/{service_id}",
            "/api/mini/service/{service_id}/qr", "/api/mini/plans", "/api/mini/activity",
            "/api/mini/ops", "/api/mini/buy", "/api/mini/renew", "/api/mini/addon",
        ):
            self.assertIn(path, paths)

    def test_nav_includes_account(self):
        from app.api.miniapp_pages import _nav_for, commerce_allowed

        ids = [x["id"] for x in _nav_for("user")]
        self.assertEqual(ids, ["home", "services", "shop", "account"])
        admin_ids = [x["id"] for x in _nav_for("admin")]
        self.assertEqual(admin_ids, ["home", "ops"])
        self.assertNotIn("shop", admin_ids)
        self.assertNotIn("account", admin_ids)
        self.assertNotIn("services", admin_ids)
        self.assertIn("ops", [x["id"] for x in _nav_for("reseller")])
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

    def test_miniapp_prefers_live_https_panel_over_http_public_base(self):
        """When TLS is on, WebApp URL must follow the live panel HTTPS origin."""
        from unittest.mock import patch

        from app.config import Settings

        s = Settings.model_construct(
            public_base_url="http://203.0.113.10:9000",
            bot_token="",
        )
        with (
            patch("app.services.ssl_certs.https_is_active", return_value=True),
            patch(
                "app.services.ssl_certs.public_panel_base_url",
                return_value="https://panel.example.com",
            ),
        ):
            self.assertTrue(s.miniapp_enabled)
            self.assertEqual(s.miniapp_url, "https://panel.example.com/miniapp/")

        # HTTP PUBLIC_BASE_URL alone must never enable WebApp (Telegram rejects it).
        with patch("app.services.ssl_certs.https_is_active", return_value=False):
            self.assertFalse(s.miniapp_enabled)
            self.assertEqual(s.miniapp_url, "")

    def test_csp_allows_telegram_for_miniapp(self):
        src = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        self.assertIn('path.startswith("/miniapp")', src)
        self.assertIn("https://telegram.org", src)
        self.assertIn("web.telegram.org", src)

    def test_admin_resellers_hub_has_no_miniapp_inline(self):
        # Mini App is MenuButton Open only — never an ops web_app row on hubs.
        hub = (ROOT / "app/bot/nav_inline.py").read_text(encoding="utf-8")
        self.assertNotIn("miniapp_inline_keyboard", hub)
        self.assertNotIn('view="ops"', hub)
        self.assertIn("def admin_resellers_hub_keyboard", hub)
        reply = (ROOT / "app/bot/handlers/reply_nav.py").read_text(encoding="utf-8")
        self.assertIn("admin_resellers_hub_keyboard", reply)

    def test_menu_button_webapp_sync(self):
        src = (ROOT / "app/bot/chat_menu.py").read_text(encoding="utf-8")
        self.assertIn("MenuButtonWebApp", src)
        self.assertIn("sync_telegram_menu_button", src)
        main = (ROOT / "app/main.py").read_text(encoding="utf-8")
        self.assertIn("sync_telegram_menu_button", main)


if __name__ == "__main__":
    unittest.main()
