"""The setup wizard's routes, driven over HTTP: a placeholder bot token is refused before anything is written, a
real one is saved, the finish page carries the plain-HTTP login link, and finishing never sends the browser to a
host the wizard did not show."""

from __future__ import annotations

import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from app.api.app import create_api_app

# The module that holds the wizard's routes: its names are what the routes call.
ROUTES = "app.api.app"
# Where the PasarGuard login check and the restart are looked up when a route runs.
LOOKUPS = {"pg_login": "app.services.pg_access", "restart": "app.services.service_control"}
STRONG_PASSWORD = "Ab12Cd34!@xyZ"
PG_FORM = {"pg_base_url": "https://pg.example:8443", "pg_username": "panel-admin", "pg_password": "pg-secret",
           "public_base_url": "https://bot.example.com/"}
REAL_TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
BOT_FORM = {"bot_username": "@shop_bot", "admin_ids": "1001, 1002"}


class SetupWizardRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch("app.api.app.is_setup_complete", return_value=False))
        for name in ("begin_setup", "ensure_web_secret", "mark_setup_complete"):
            stack.enter_context(patch(f"{ROUTES}.{name}"))
        self.stored: dict[str, str] = {}
        for module in {ROUTES, "app.services.setup_wizard"}:
            stack.enter_context(patch(f"{module}.current_setup_values", side_effect=lambda: dict(self.stored)))
        self.saved = stack.enter_context(patch(f"{ROUTES}.update_env_keys", MagicMock()))
        self.admin = stack.enter_context(patch(f"{ROUTES}.save_web_admin", MagicMock()))
        # The URL guard stays real; only its DNS lookup is answered here.
        stack.enter_context(patch(f"{ROUTES}.assert_safe_pg_base_url", side_effect=lambda url: url))
        self.pg_login = stack.enter_context(patch(f"{LOOKUPS['pg_login']}.resolve_platform_pg_capabilities",
                                                  AsyncMock(return_value={"ok": True})))
        stack.enter_context(patch(f"{LOOKUPS['pg_login']}.clear_platform_pg_capability_cache"))
        self.restart = stack.enter_context(patch(f"{LOOKUPS['restart']}.schedule_panel_restart"))
        transport = httpx.ASGITransport(app=create_api_app(), client=("127.0.0.1", 50000))
        # A browser sends its Origin; the wizard refuses a POST without one once the setup_gate cookie is set.
        self.client = httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:9000",
                                        headers={"origin": "http://127.0.0.1:9000"})
        self.addAsyncCleanup(self.client.aclose)

    async def test_a_placeholder_bot_token_is_refused_before_anything_is_saved(self) -> None:
        for token in ("123456:ABC-DEF", "YOUR_BOT_TOKEN"):
            with self.subTest(token=token):
                response = await self.client.post("/setup/bot", data={"bot_token": token, **BOT_FORM})
                self.assertEqual(response.status_code, 200)
                self.assertIn("توکن ربات نامعتبر است", response.text)
        self.saved.assert_not_called()

    async def test_a_real_bot_token_is_saved_with_the_admins(self) -> None:
        response = await self.client.post("/setup/bot", data={"bot_token": REAL_TOKEN, **BOT_FORM})
        self.assertEqual(response.headers["location"], "/setup?step=3")
        self.saved.assert_called_once_with(
            {"BOT_TOKEN": REAL_TOKEN, "BOT_USERNAME": "shop_bot", "ADMIN_IDS": "1001,1002"}
        )

    async def test_the_finish_step_carries_the_login_link(self) -> None:
        link = "http://203.0.113.7:9000/login"
        with patch(f"{ROUTES}.setup_finish_login_url", return_value=link), \
                patch(f"{ROUTES}.setup_pg_access_audit", return_value=None):
            page = (await self.client.get("/setup?step=4")).text
        self.assertIn(link, page)

    async def test_finishing_sends_the_browser_to_the_plain_http_login_link(self) -> None:
        link = "http://203.0.113.7:9000/login?restarting=1"
        with patch("app.services.setup_wizard.setup_finish_login_url", return_value=link), \
                patch("app.services.ssl_certs.https_is_active", return_value=False):
            empty = await self.client.post("/setup/finish", data={})
            shown = await self.client.post("/setup/finish", data={"next": "https://203.0.113.7:9000/login"})
        self.assertEqual(empty.headers["location"], link)
        self.assertTrue(shown.headers["location"].startswith("http://203.0.113.7:9000/login"), shown.headers)

    async def test_finishing_never_redirects_to_a_host_the_wizard_did_not_show(self) -> None:
        response = await self.client.post("/setup/finish", data={"next": "https://evil.example/login"})
        self.assertEqual(response.status_code, 303)
        self.assertNotIn("evil.example", response.headers["location"])
        self.restart.assert_called_once()

    async def test_the_admin_step_refuses_a_mismatch_or_a_weak_password(self) -> None:
        for first, second, message in ((STRONG_PASSWORD, STRONG_PASSWORD + "x", "یکسان نیستند"),
                                        ("short", "short", "حداقل ۱۲ کاراکتر")):
            with self.subTest(message=message):
                response = await self.client.post("/setup/admin", data={
                    "username": "owner", "password": first, "password_confirm": second})
                self.assertIn(message, response.text)
        self.admin.assert_not_called()
        self.saved.assert_not_called()

    async def test_the_admin_step_saves_a_strong_password(self) -> None:
        response = await self.client.post("/setup/admin", data={
            "username": " owner ", "password": STRONG_PASSWORD, "password_confirm": STRONG_PASSWORD})
        self.assertEqual(response.headers["location"], "/setup?step=2")
        self.admin.assert_called_once_with("owner", STRONG_PASSWORD)
        self.saved.assert_called_once_with({"WEB_ADMIN_USER": "owner"})

    async def post_pg(self, json: bool = False, **fields: str):
        headers = {"x-requested-with": "setup-probe"} if json else {}
        return await self.client.post("/setup/other", data={**PG_FORM, **fields}, headers=headers)

    async def test_the_pasarguard_step_saves_after_a_good_login(self) -> None:
        response = await self.post_pg()
        self.assertEqual(response.headers["location"], "/setup?step=4")
        self.pg_login.assert_awaited_once()
        self.saved.assert_called_once_with({
            "PG_BASE_URL": "https://pg.example:8443", "PG_SUBSCRIPTION_PATH": "/sub", "PG_USERNAME": "panel-admin",
            "PG_PASSWORD": "pg-secret", "PG_API_KEY": "", "WEB_PORT": "9000",
            "PUBLIC_BASE_URL": "http://bot.example.com", "CURRENCY": "تومان",
        })
        self.saved.reset_mock()
        response = await self.post_pg(json=True)
        self.assertEqual(response.json(), {"ok": True, "next": "/setup?step=4"})

    async def test_an_empty_secret_keeps_the_saved_one_and_the_key_can_be_cleared(self) -> None:
        self.stored.update({"PG_PASSWORD": "stored-secret", "PG_API_KEY": "k" * 20})
        await self.post_pg(pg_password="")
        self.assertEqual(self.saved.call_args.args[0]["PG_PASSWORD"], "stored-secret")
        self.assertEqual(self.saved.call_args.args[0]["PG_API_KEY"], "k" * 20)
        await self.post_pg(clear_pg_api_key="on")
        self.assertEqual(self.saved.call_args.args[0]["PG_API_KEY"], "")

    async def test_the_pasarguard_step_refuses_bad_fields_and_saves_nothing(self) -> None:
        cases = (
            ({"pg_base_url": "   "}, "آدرس پاسارگارد الزامی است"),
            ({"pg_subscription_path": "https://x/sub"}, "مسیر سابسکریپشن نامعتبر است"),
            ({"pg_username": ""}, "نام کاربری پاسارگارد الزامی است"),
            ({"pg_api_key": "short"}, "کلید API پاسارگارد کوتاه است"),
            ({"pg_password": ""}, "رمز یا کلید API پاسارگارد الزامی است"),
            ({"web_port": "70000"}, "پورت وب نامعتبر است"),
            ({"web_port": "abc"}, "پورت وب نامعتبر است"),
        )
        for fields, message in cases:
            with self.subTest(fields=fields):
                self.assertIn(message, (await self.post_pg(**fields)).text)
                response = await self.post_pg(json=True, **fields)
                self.assertEqual(response.status_code, 400)
                self.assertIn(message, response.json()["error"])
        self.saved.assert_not_called()
        self.pg_login.assert_not_awaited()

    async def test_a_failed_pasarguard_login_saves_nothing(self) -> None:
        self.pg_login.return_value = {"ok": False, "error": "401"}
        response = await self.post_pg(json=True)
        self.assertEqual(response.status_code, 400)
        self.assertIn("ورود به پاسارگارد ناموفق بود", response.json()["error"])
        self.saved.assert_not_called()


if __name__ == "__main__":
    unittest.main()
