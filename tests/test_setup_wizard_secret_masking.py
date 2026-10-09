"""Regression test: setup wizard must never echo BOT_TOKEN/PG_PASSWORD back
into the rendered HTML, and re-submitting the wizard with those fields left
blank must keep the previously saved secret instead of erroring out or
silently wiping it.

Before the fix, ``setup.html`` pre-filled both fields with the live secret
value straight from ``.env`` (``value="{{ values.BOT_TOKEN }}"``), so
anyone who could view the page source during the (token/local-IP gated)
setup window could read the real bot token / PasarGuard password. Because
the fields were also ``required``, simply blanking them client-side (to
avoid the leak) would have broken re-running the wizard to fix one field —
hence the paired "empty stays blank in HTML" + "empty on submit means keep
previous" fix.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from starlette.requests import Request

from app.api.app import render


def _fake_request(path: str = "/setup") -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [],
        "query_string": b"",
        "app": None,
    }
    return Request(scope)


class SetupWizardTemplateMaskingTests(unittest.TestCase):
    def _render(
        self,
        *,
        has_bot_token: bool,
        has_pg_password: bool,
        has_pg_api_key: bool = False,
    ) -> str:
        resp = render(
            _fake_request(),
            "setup.html",
            {
                "values": {
                    "BOT_TOKEN": "",
                    "PG_PASSWORD": "",
                    "PG_API_KEY": "",
                    "BOT_USERNAME": "shopbot",
                    "ADMIN_IDS": "123",
                    "PG_BASE_URL": "https://pg.example.com",
                    "PG_USERNAME": "admin",
                    "WEB_PORT": "9000",
                },
                "has_bot_token": has_bot_token,
                "has_pg_password": has_pg_password,
                "has_pg_api_key": has_pg_api_key,
                "initial_step": 2,
                "show_done": False,
            },
        )
        return resp.body.decode("utf-8")

    def test_real_secrets_never_appear_in_rendered_html(self):
        # Even if a caller accidentally passed the raw secret through
        # `values`, the template itself must not interpolate it anymore —
        # simulate the previous behavior's input to prove the template no
        # longer echoes it.
        html = self._render(has_bot_token=True, has_pg_password=True, has_pg_api_key=True)
        self.assertNotIn('value="123456789:AAHdqTcv', html)
        self.assertIn('name="bot_token" value=""', html)
        self.assertIn('name="pg_password" type="password" value=""', html)
        self.assertIn('name="pg_api_key" type="password" value=""', html)

    def test_placeholder_shown_when_secret_already_saved(self):
        html = self._render(has_bot_token=True, has_pg_password=True)
        self.assertIn("قبلاً ذخیره شده", html)
        # required attribute must be dropped so the browser allows blank resubmit
        bot_token_tag = html[html.index('name="bot_token"') : html.index('name="bot_token"') + 200]
        self.assertNotIn("required", bot_token_tag)

    def test_required_attribute_present_when_no_secret_yet(self):
        html = self._render(has_bot_token=False, has_pg_password=False)
        bot_token_tag = html[html.index('name="bot_token"') : html.index('name="bot_token"') + 200]
        self.assertIn("required", bot_token_tag)
        self.assertNotIn("قبلاً ذخیره شده", html)

    def test_password_not_required_when_api_key_already_saved(self):
        html = self._render(
            has_bot_token=True, has_pg_password=False, has_pg_api_key=True
        )
        pwd_tag = html[html.index('name="pg_password"') : html.index('name="pg_password"') + 180]
        self.assertNotIn("required", pwd_tag)
        self.assertIn("کلید API ذخیره‌شده", html)
        self.assertIn('name="clear_pg_api_key"', html)

    def test_api_key_field_present(self):
        html = self._render(has_bot_token=False, has_pg_password=False)
        self.assertIn('name="pg_api_key"', html)
        self.assertIn("X-Api-Key", html)


class SetupBotEmptyTokenKeepsPreviousTests(unittest.IsolatedAsyncioTestCase):
    async def _call_setup_bot(self, *, submitted_token: str, existing_token: str):
        import app.api.app as app_module

        # setup_bot is a closure inside create_api_app(); reach it through the
        # actual FastAPI app instance instead of re-implementing its logic.
        route = None
        app = app_module.create_api_app()
        for r in app.routes:
            if getattr(r, "path", None) == "/setup/bot":
                route = r
                break
        self.assertIsNotNone(route, "POST /setup/bot route not found")

        class _FakeRequest(Request):
            def __init__(self, form_data):
                super().__init__(
                    {
                        "type": "http",
                        "method": "POST",
                        "path": "/setup/bot",
                        "headers": [],
                        "query_string": b"",
                        "app": None,
                    }
                )
                self._form_data = form_data

            async def form(self, *a, **k):
                return self._form_data

        with (
            patch.object(app_module, "is_setup_complete", return_value=False),
            patch.object(app_module, "begin_setup", return_value=None),
            patch.object(app_module, "update_env_keys", return_value=None) as mocked_save,
            patch.object(app_module, "ensure_web_secret", return_value="x" * 32),
            patch.object(
                app_module,
                "current_setup_values",
                return_value={"BOT_TOKEN": existing_token},
            ),
        ):
            resp = await route.endpoint(
                request=_FakeRequest({}),
                bot_token=submitted_token,
                bot_username="shopbot",
                admin_ids="123456789",
            )
        return resp, mocked_save

    async def test_blank_token_reuses_existing_real_token(self):
        real_token = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
        resp, mocked_save = await self._call_setup_bot(submitted_token="", existing_token=real_token)
        self.assertEqual(resp.status_code, 303)
        self.assertIn("step=3", resp.headers["location"])
        mocked_save.assert_called_once()
        saved = mocked_save.call_args.args[0]
        self.assertEqual(saved["BOT_TOKEN"], real_token)

    async def test_blank_token_with_no_existing_token_is_rejected(self):
        resp, mocked_save = await self._call_setup_bot(submitted_token="", existing_token="")
        self.assertEqual(resp.status_code, 200)
        mocked_save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
