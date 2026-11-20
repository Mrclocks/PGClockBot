"""Regression tests for dashboard Internal Server Error root causes."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from jinja2 import DictLoader, Environment


class SessionRollbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_rollback_quiet_calls_session(self):
        from app.services.db_safe import rollback_quiet

        session = AsyncMock()
        await rollback_quiet(session)
        session.rollback.assert_awaited_once()

    async def test_rollback_quiet_none(self):
        from app.services.db_safe import rollback_quiet

        await rollback_quiet(None)


class HomeOverviewFailSoftTests(unittest.IsolatedAsyncioTestCase):
    async def test_gather_exception_returns_complete_shell(self):
        from app.services.home_overview import build_home_overview, empty_home_overview

        session = AsyncMock()
        with (
            patch(
                "app.services.home_overview.check_bot_connection",
                new_callable=AsyncMock,
                side_effect=RuntimeError("bot boom"),
            ),
            patch(
                "app.services.home_overview.bot_panel_summary",
                new_callable=AsyncMock,
                side_effect=RuntimeError("db boom"),
            ),
            patch(
                "app.services.home_overview.pg_home_bundle",
                new_callable=AsyncMock,
                side_effect=RuntimeError("pg boom"),
            ),
            patch(
                "app.services.home_overview.current_setup_values",
                return_value={"BOT_TOKEN": "1:x"},
            ),
        ):
            overview = await build_home_overview(session)

        shell = empty_home_overview()
        for key in shell:
            self.assertIn(key, overview)
            self.assertIsInstance(overview[key], dict)
        self.assertIn("cpu_percent", overview["host"])
        self.assertIn("users", overview["bot_summary"])
        self.assertIn("nodes", overview["nodes"])


class HomeTemplateGaugeTests(unittest.TestCase):
    def test_missing_cpu_percent_does_not_raise(self):
        tpl = (
            "{% set host = ov.host %}"
            "{% if host.cpu_percent is number %}"
            "{{ '%.0f'|format(host.cpu_percent) }}٪"
            "{% else %}—{% endif %}"
        )
        env = Environment(loader=DictLoader({"t": tpl}))
        # Incomplete host used to 500 via is not none + format(Undefined).
        html = env.get_template("t").render(ov={"host": {}})
        self.assertEqual(html, "—")

    def test_home_html_uses_number_test(self):
        gauges = Path("app/web/templates/_host_resource_gauges.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("host.cpu_percent is number", gauges)
        self.assertIn("host.memory_percent is number", gauges)
        self.assertNotIn("host.cpu_percent is not none", gauges)
        home = Path("app/web/templates/home.html").read_text(encoding="utf-8")
        self.assertNotIn("host.cpu_percent is not none", home)


class WebAdminCorruptFileTests(unittest.TestCase):
    def test_corrupt_json_does_not_raise(self):
        from app.services import web_auth

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "web_admin.json"
            path.write_text("{not-json", encoding="utf-8")
            with patch.object(web_auth, "AUTH_FILE", path), patch.object(
                web_auth, "DATA_DIR", Path(tmp)
            ):
                creds = web_auth.load_web_admin()
                self.assertEqual(creds.get("password"), "")
                self.assertEqual(web_auth.admin_session_version(), "")


class SafeHelpersRollbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_safe_funnel_rolls_back(self):
        from app.api import home_pages

        session = AsyncMock()
        with patch(
            "app.services.funnel_metrics.funnel_summary",
            new_callable=AsyncMock,
            side_effect=RuntimeError("missing table"),
        ):
            out = await home_pages._safe_funnel(session, reseller_id=None)
        self.assertEqual(out["shop_open"], 0)
        session.rollback.assert_awaited()

    async def test_finance_page_keeps_fail_soft_delivery_failures(self):
        src = Path("app/api/finance_pages.py").read_text(encoding="utf-8")
        self.assertIn("rollback_quiet", src)
        self.assertIn("list_open_delivery_failures", src)

    async def test_funnel_page_renders_when_the_funnel_query_fails(self):
        import httpx
        from fastapi import FastAPI

        from app.api.app import render
        from app.api.funnel_pages import register_funnel_pages

        session = AsyncMock()
        owner = {
            "role": "admin",
            "permissions": [],
            "org_principal_id": 1,
            "org_depth": 0,
            "org_parent_id": None,
            "org_status": "active",
        }

        async def get_db():
            yield session

        app = FastAPI()
        register_funnel_pages(app, render=render, require_staff=lambda: owner, get_db=get_db)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(client.aclose)
        with patch("app.services.db_safe.recover_session", AsyncMock()), patch(
            "app.services.funnel_metrics.funnel_summary",
            new_callable=AsyncMock,
            side_effect=RuntimeError("missing table"),
        ):
            response = await client.get("/funnel")
        self.assertEqual(response.status_code, 200)
        session.rollback.assert_awaited()

    async def test_funnel_page_uses_shared_panel(self):
        page = Path("app/web/templates/funnel.html").read_text(encoding="utf-8")
        self.assertIn('_funnel_panel.html', page)
        self.assertNotIn("رها می‌کنند", page)

    async def test_pg_quota_gauge_uses_number_test(self):
        src = Path("app/web/templates/_pg_quota_gauges.html").read_text(encoding="utf-8")
        self.assertIn("remain_pct is number", src)
        self.assertNotIn("remain_pct is not none", src)


class RecoverSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_recover_heals_pending_rollback(self):
        from sqlalchemy.exc import PendingRollbackError

        from app.services.db_safe import recover_session

        session = AsyncMock()
        session.connection = AsyncMock(side_effect=PendingRollbackError())
        session.rollback = AsyncMock()
        await recover_session(session)
        session.rollback.assert_awaited()


class EnrichAdminFailClosedTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_exception_keeps_admin_session(self):
        from app.services.pg_access import enrich_platform_admin_staff

        with patch(
            "app.services.pg_access.resolve_platform_pg_capabilities",
            new_callable=AsyncMock,
            side_effect=RuntimeError("pg timeout"),
        ):
            out = await enrich_platform_admin_staff({"role": "admin", "username": "owner"})
        self.assertEqual(out.get("role"), "admin")
        self.assertFalse(out.get("pg_capabilities_ok"))
        self.assertEqual(out.get("pg_permissions") or [], [])


class PanelRedirectAndSecretTests(unittest.TestCase):
    def test_panel_redirect_never_raises_on_ssl_failure(self):
        from app.api.app import _panel_redirect

        class Req:
            headers = {"host": "panel.example"}
            url = type("U", (), {"netloc": "panel.example", "scheme": "http"})()

        with patch(
            "app.services.ssl_certs.https_is_active",
            side_effect=RuntimeError("ssl boom"),
        ):
            resp = _panel_redirect(Req(), "/home")
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/home", resp.headers.get("location", ""))

    def test_ensure_web_secret_survives_env_write_failure(self):
        from app.services import setup_wizard

        with (
            patch.object(setup_wizard, "_existing_valid_web_secret", return_value=""),
            patch.object(
                setup_wizard,
                "update_env_keys",
                side_effect=OSError("read-only fs"),
            ),
        ):
            with self.assertRaises(setup_wizard.WebSecretPersistenceError) as ctx:
                setup_wizard.ensure_web_secret()
        msg = str(ctx.exception)
        self.assertIn("WEB_SECRET", msg)
        self.assertNotIn("change-me", msg)
        self.assertNotIn("read-only fs", msg.lower())

    def test_home_action_center_entries_render(self):
        """Jinja ``ac.items`` would call dict.items — must use ``entries`` key."""
        from jinja2 import Environment, DictLoader

        from app.services.formatting import format_number

        # Snippet mirrors home.html action-center loop.
        tpl = (
            "{% set ac = action_center or {} %}"
            "{% if ac.has_items %}"
            "{% for item in ac.entries %}{{ item.title }};{% endfor %}"
            "{% endif %}"
        )
        env = Environment(loader=DictLoader({"t": tpl}))
        env.filters["num"] = format_number
        html = env.get_template("t").render(
            action_center={
                "has_items": True,
                "entries": [
                    {
                        "title": "۲ رسید",
                        "detail": "x",
                        "href": "/finance",
                        "tone": "warn",
                    }
                ],
            }
        )
        self.assertIn("۲ رسید", html)
        # Guard against regressing to ac.items (dict method).
        home = Path("app/web/templates/_home_dash_body.html").read_text(encoding="utf-8")
        ops = Path("app/web/templates/_home_ops.html").read_text(encoding="utf-8")
        reseller = Path("app/web/templates/_reseller_home_dash_body.html").read_text(encoding="utf-8")
        self.assertIn("_home_ops.html", home)
        self.assertIn("_home_ops.html", reseller)
        self.assertIn("ac.entries", ops)
        self.assertIn("home-action-leading", ops)
        self.assertIn("action_center_icon", ops)
        self.assertNotIn("for item in ac.items", ops)
        self.assertNotIn("for item in ac.items", home)
        self.assertNotIn("for item in ac.items", reseller)


if __name__ == "__main__":
    unittest.main()
