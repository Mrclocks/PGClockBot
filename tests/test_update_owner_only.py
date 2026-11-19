"""Only the Owner can run, roll back or even hear about a panel update; shops and staff cannot."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch


import httpx
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.requests import Request

from app.api import app as app_module
from app.db import Base
from app.db.models import BotUser, ResellerProfile
from app.services import panel_update as pu
from app.services.panel_inbox import build_inbox_context
from app.services.setup_wizard import ensure_web_secret
from tests.postgres_support import scoped_async_engine

SESSION_VERSION = "session-version"
PASSWORD_HASH = "h" * 40
OWNER_SESSION = {
    "role": "admin", "sv": SESSION_VERSION, "org_principal_id": 1, "org_depth": 0,
    "org_parent_id": None, "org_status": "active",
}
SHOP_SESSION = {"role": "reseller", "bot_user_id": 5, "pv": PASSWORD_HASH[:24]}
PRINCIPAL_SESSION = {"role": "principal", "org_principal_id": 2, "org_depth": 1}
PG_STAFF_SESSION = {"role": "pg_staff", "username": "someone"}

UPDATE_ROUTES = (
    ("GET", "/update/status"),
    ("GET", "/update"),
    ("POST", "/update/start"),
    ("POST", "/update/channel"),
    ("POST", "/update/clear"),
)
BACKUP_ROUTES = (
    ("GET", "/backup/status"),
    ("POST", "/backup/create"),
    ("POST", "/backup/restore/abc"),
    ("POST", "/backup/delete/abc"),
    ("GET", "/backup/download/abc"),
)
UPDATE_AVAILABLE = {"update_available": True, "remote_version": "9.9.9", "version": "0.4.0", "checked": True}


class RoutesHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with Session() as session:
            session.add_all([
                BotUser(id=5, telegram_id=105, referral_code="shop5", role="reseller"),
                ResellerProfile(
                    user_id=5, is_active=True, web_username="shop", web_password_hash=PASSWORD_HASH,
                    setup_completed_at=datetime.now(timezone.utc),
                ),
            ])
            await session.commit()
        for target, value in (
            ("SessionLocal", Session),
            ("admin_session_version", lambda: SESSION_VERSION),
            ("is_setup_complete", lambda: True),
        ):
            patcher = patch.object(app_module, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.start_update = MagicMock(return_value={"ok": True})
        for target, mock in (
            ("app.services.panel_update.start_update", self.start_update),
        ):
            patcher = patch(target, mock)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.signer = URLSafeTimedSerializer(ensure_web_secret(), salt="pgclock-session")
        app = app_module.create_api_app()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://panel.test", follow_redirects=False
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.engine.dispose()

    async def call(self, session_payload: dict, method: str, path: str) -> httpx.Response:
        cookie = f"session={self.signer.dumps(session_payload)}; csrf=token"
        headers = {"cookie": cookie, "x-csrf-token": "token", "origin": "http://panel.test"}
        return await self.client.request(method, path, headers=headers, json={} if method == "POST" else None)


class UpdateAndBackupRoutesTests(RoutesHarness):
    async def test_the_owner_reaches_the_update_status(self):
        response = await self.call(OWNER_SESSION, "GET", "/update/status")
        self.assertEqual(response.status_code, 200)
        self.assertIn("current_version", response.json())

    async def test_a_shop_owner_is_turned_away_from_every_update_and_backup_route(self):
        for method, path in (*UPDATE_ROUTES, *BACKUP_ROUTES):
            response = await self.call(SHOP_SESSION, method, path)
            self.assertEqual(response.status_code, 303, (method, path))
            self.assertEqual(response.headers["location"], "/home", (method, path))
        self.start_update.assert_not_called()

    async def test_other_staff_roles_cannot_use_them_either(self):
        for payload in (PRINCIPAL_SESSION, PG_STAFF_SESSION):
            for method, path in (*UPDATE_ROUTES, *BACKUP_ROUTES):
                response = await self.call(payload, method, path)
                self.assertEqual(response.status_code, 303, (payload["role"], method, path))
                self.assertTrue(response.headers["location"].startswith("/login"), (payload["role"], method, path))
        self.start_update.assert_not_called()

    async def test_the_removed_rollback_endpoint_answers_nothing_even_for_the_owner(self):
        for body in ({"version": "0.4.0"}, {"snapshot_id": "abc"}, {}):
            response = await self.client.request(
                "POST", "/update/rollback", json=body,
                headers={"cookie": f"session={self.signer.dumps(OWNER_SESSION)}; csrf=token",
                         "x-csrf-token": "token", "origin": "http://panel.test"},
            )
            self.assertIn(response.status_code, {404, 405}, body)
        self.start_update.assert_not_called()

    async def test_the_update_tab_offers_an_update_and_no_version_rollback(self):
        context = pu._degraded_update_page_context()
        with patch("app.services.panel_update.update_page_context", AsyncMock(return_value=context)):
            response = await self.call(OWNER_SESSION, "GET", "/settings?tab=update")
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="upd-start"', response.text)
        self.assertIn('id="upd-channel"', response.text)
        self.assertNotIn("rollback", response.text.lower())
        self.assertNotIn("راه برگشت", response.text)

    async def test_without_a_session_nothing_runs(self):
        for method, path in UPDATE_ROUTES:
            response = await self.client.request(
                method, path, headers={"x-csrf-token": "t", "cookie": "csrf=t", "origin": "http://panel.test"},
                json={} if method == "POST" else None,
            )
            self.assertIn(response.status_code, {303, 401, 403}, (method, path))
        self.start_update.assert_not_called()

    async def test_a_forged_or_tampered_cookie_is_not_an_owner_session(self):
        tampered = self.signer.dumps(OWNER_SESSION)[:-3] + "xyz"
        response = await self.client.post(
            "/update/start",
            headers={"cookie": f"session={tampered}; csrf=token", "x-csrf-token": "token", "origin": "http://panel.test"},
            json={},
        )
        self.assertNotEqual(response.status_code, 200)
        self.start_update.assert_not_called()


class UpdateNoticeVisibilityTests(unittest.IsolatedAsyncioTestCase):
    SHOP_STAFF = {"role": "reseller", "bot_user_id": 5, "pv": PASSWORD_HASH[:24]}

    async def _inbox_context(self, staff: dict) -> dict:
        session = AsyncMock()
        request = MagicMock()
        request.state = MagicMock(panel_tickets_unread=0)
        with (
            patch("app.services.updates.check_github_update", AsyncMock(return_value=dict(UPDATE_AVAILABLE))),
            patch("app.services.updates.peek_update_cache", return_value=dict(UPDATE_AVAILABLE)),
            patch("app.api.panel_tickets_pages.panel_ticket_dashboard_alert", AsyncMock(return_value=None)),
            patch("app.services.users.get_all_settings", AsyncMock(return_value={})),
        ):
            return await build_inbox_context(session, request, staff)

    async def test_the_inbox_context_of_a_shop_owner_has_no_update(self):
        ctx = await self._inbox_context(self.SHOP_STAFF)
        self.assertIsNone(ctx["update"])

    async def test_the_inbox_context_of_the_owner_has_the_update(self):
        ctx = await self._inbox_context(OWNER_SESSION)
        self.assertEqual(ctx["update"]["remote_version"], "9.9.9")


def _request(path: str = "/inbox") -> Request:
    return Request(
        {
            "type": "http", "asgi": {"version": "3"}, "http_version": "1.1", "method": "GET", "scheme": "https",
            "path": path, "raw_path": path.encode(), "query_string": b"",
            "headers": [(b"host", b"example.test")], "client": ("127.0.0.1", 123),
            "server": ("example.test", 443), "state": {},
        }
    )


class UpdateNoticeRenderingTests(unittest.TestCase):
    def _inbox(self, staff: dict, **extra) -> str:
        ctx = {
            "staff": staff, "update": dict(UPDATE_AVAILABLE), "ticket_alert": None, "action_center": {},
            "shop_maintenance": False, "capacity_warn": False, "payg_risk": {}, "inbox_count": 1, "inbox_has": True,
        }
        if extra.pop("update_from_cache", False):
            del ctx["update"]
        return app_module.render(_request(), "inbox.html", ctx).body.decode("utf-8")

    def test_the_owner_sees_the_new_version_and_the_update_button(self):
        html = self._inbox(OWNER_SESSION)
        self.assertIn("9.9.9", html)
        self.assertIn('href="/settings?tab=update"', html)
        self.assertIn("brand-ver-dot-warn", html)

    def test_a_shop_owner_sees_no_new_version_no_update_button_and_no_warning_dot(self):
        html = self._inbox(UpdateNoticeVisibilityTests.SHOP_STAFF)
        self.assertNotIn("9.9.9", html)
        self.assertNotIn("tab=update", html)
        self.assertNotIn("brand-ver-dot-warn", html)

    def test_the_cached_update_never_reaches_a_page_a_shop_owner_opens(self):
        with patch("app.services.updates.peek_update_cache", return_value=dict(UPDATE_AVAILABLE)):
            for staff, expected in ((OWNER_SESSION, True), (UpdateNoticeVisibilityTests.SHOP_STAFF, False)):
                html = self._inbox(staff, update_from_cache=True)
                self.assertEqual("brand-ver-dot-warn" in html, expected, staff["role"])


if __name__ == "__main__":
    unittest.main()
