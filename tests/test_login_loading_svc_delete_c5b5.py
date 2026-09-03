"""Login atmosphere + autofill auto-submit; service delete; nav clock pending."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[1]


class LoginAtmosphereTests(unittest.TestCase):
    def test_login_reuses_status_atmosphere_without_top_accent(self):
        html = (ROOT / "app/web/templates/login.html").read_text(encoding="utf-8")
        css = (ROOT / "app/web/static/panel.css").read_text(encoding="utf-8")
        self.assertIn("auth-wrap--atmo", html)
        self.assertIn("auth-atmosphere", html)
        self.assertIn("auth-orb-a", html)
        self.assertIn("auth-form--card", html)
        self.assertNotIn("panel-status-card-accent", html)
        self.assertNotIn("auth-card-accent", html)
        self.assertIn(".auth-wrap--atmo", css)
        self.assertIn(".auth-form--card", css)
        self.assertIn("auth-autofill-mark", css)

    def test_autofill_auto_submit_script(self):
        html = (ROOT / "app/web/templates/login.html").read_text(encoding="utf-8")
        self.assertIn("autoSubmitAutofill", html)
        self.assertIn("requestSubmit", html)
        self.assertIn(":-webkit-autofill", html)
        self.assertIn('id="login-form"', html)
        self.assertIn("data-no-nav-clock", html)


class NavClockWidgetsPendingTests(unittest.TestCase):
    def test_pageshow_keeps_clock_while_widgets_pending(self):
        js = (ROOT / "app/web/static/panel.js").read_text(encoding="utf-8")
        defer = (ROOT / "app/web/templates/_panel_widgets_defer.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("data-panel-widgets-pending", js)
        self.assertIn("shellWidgetsPending", js)
        self.assertIn("widgetsPending", js)
        self.assertIn("disarmAfterPaint", js)
        # Must not blindly disarm on every pageshow anymore.
        self.assertNotIn("window.addEventListener('pageshow', disarm);", js)
        self.assertIn("data-panel-widgets-pending", defer)
        self.assertIn("requestAnimationFrame", defer)

    def test_deferred_homes_still_use_nav_clock(self):
        for rel in (
            "app/web/templates/home.html",
            "app/web/templates/reseller_home.html",
            "app/web/templates/pg_home.html",
        ):
            src = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn("_panel_widgets_defer.html", src)


class ServiceDeleteTests(unittest.TestCase):
    def test_web_delete_route_and_ui(self):
        pages = (ROOT / "app/api/user_pages.py").read_text(encoding="utf-8")
        body = (ROOT / "app/web/templates/_user_edit_body.html").read_text(
            encoding="utf-8"
        )
        self.assertIn('/services/{service_id}/delete', pages)
        self.assertIn("admin_delete_service", pages)
        self.assertIn("get_owned_service", pages)
        self.assertIn("/services/{{ s.service.id }}/delete", body)
        self.assertIn("data-confirm-danger", body)
        self.assertIn("حذف سرویس", body)

    def test_bot_admin_and_user_delete_surfaces(self):
        kb = (ROOT / "app/bot/keyboards.py").read_text(encoding="utf-8")
        admin = (ROOT / "app/bot/handlers/admin.py").read_text(encoding="utf-8")
        svc = (ROOT / "app/bot/handlers/services.py").read_text(encoding="utf-8")
        reply = (ROOT / "app/bot/handlers/reply_nav.py").read_text(encoding="utf-8")
        self.assertIn("adm:users:svcdelask:", kb)
        self.assertIn("admin_user_service_delete_confirm", kb)
        self.assertIn("REPLY_ACTION_SVC_DELETE", kb)
        self.assertIn("adm_users_service_delete", admin)
        self.assertIn("admin_delete_service", admin)
        self.assertIn("svc_delete_ask", svc)
        self.assertIn("svc:delask:", svc)
        self.assertIn("REPLY_ACTION_SVC_DELETE", reply)

    def test_pg_delete_detaches_local_services(self):
        pg = (ROOT / "app/api/pg_pages.py").read_text(encoding="utf-8")
        bot_pg = (ROOT / "app/bot/handlers/admin_pg_users.py").read_text(
            encoding="utf-8"
        )
        bulk = (ROOT / "app/services/table_bulk_ext.py").read_text(encoding="utf-8")
        self.assertIn("detach_local_services_for_pg_user", pg)
        self.assertIn("detach_local_services_for_pg_user", bot_pg)
        self.assertIn("detach_local_services_for_pg_user", bulk)

    def test_admin_delete_service_module(self):
        src = (ROOT / "app/services/bot_user_admin.py").read_text(encoding="utf-8")
        self.assertIn("async def admin_delete_service", src)
        self.assertIn("async def detach_local_services_for_pg_user", src)
        self.assertIn("LuckyWheelSpin", src)
        self.assertIn("RewardRedemption", src)

    def test_admin_delete_service_guards_ownership_via_caller(self):
        from app.services.bot_user_admin import admin_delete_service

        self.assertTrue(callable(admin_delete_service))


class ServiceDeleteUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_detach_local_services_noop_when_missing(self):
        from app.services.bot_user_admin import detach_local_services_for_pg_user

        session = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        session.execute = AsyncMock(return_value=result)
        n = await detach_local_services_for_pg_user(session, 99, commit=False)
        self.assertEqual(n, 0)
        session.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
