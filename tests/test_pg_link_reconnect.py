"""Owner can manually reconnect the PasarGuard link from the home health card."""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import unquote

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from jinja2 import Environment, FileSystemLoader

from app.api.pg_link_pages import DENIED_MESSAGE
from app.services import pg_link_health as health
from app.services import pg_link_reconnect as reconnect
from app.services import pg_read_resilience as resilience
from app.services.csrf import CSRF_COOKIE, CSRF_FORM_FIELD, extract_csrf_from_request
from app.services.pasarguard import PasarGuardError
from app.services.resellers import DEFAULT_FEATURE_PERMS, FEATURE_PERMS, parse_perms

BASE = "https://panel.example"
TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "web" / "templates"
HOSTILE = "https://evil.example/secret-token SECRET_API_KEY=abc Traceback (most recent call last)"


class _FakePanel:
    """Minimal PasarGuard client surface used by reconnect_panel."""

    def __init__(self, handler) -> None:
        self._handler = handler
        self._client = httpx.AsyncClient(
            base_url=BASE,
            transport=httpx.MockTransport(handler),
        )

    def _http(self) -> httpx.AsyncClient:
        return self._client

    async def _headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer test"}

    async def _ensure_safe_target(self, *, protected: bool = True) -> None:
        return None

    async def get_system_probe(self, *, timeout: httpx.Timeout) -> httpx.Response:
        await self._ensure_safe_target(protected=True)
        headers = await self._headers()
        return await resilience.panel_read_request(
            self._http(),
            "GET",
            "/api/system",
            headers=headers,
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


class PermissionDeclarationTests(unittest.TestCase):
    def test_reconnect_key_is_declared_and_denied_by_default(self) -> None:
        keys = {key for key, _label in FEATURE_PERMS}
        self.assertIn(reconnect.PG_LINK_RECONNECT_PERMISSION, keys)
        self.assertNotIn(reconnect.PG_LINK_RECONNECT_PERMISSION, parse_perms(DEFAULT_FEATURE_PERMS))


class ReconnectServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        health.reset()
        resilience.reset_read_circuits()
        reconnect.reset_reconnect_gate()
        self.answer: object = 200
        self.calls = 0
        self.panel = _FakePanel(self._handler)

    async def asyncTearDown(self) -> None:
        await self.panel.aclose()
        reconnect.reset_reconnect_gate()
        health.reset()
        resilience.reset_read_circuits()

    def _handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return httpx.Response(int(self.answer), json={"status": "ok"})

    def _pause_circuit(self) -> None:
        state = resilience._panel_state(BASE)
        state.blocked_until = resilience.time.monotonic() + 60.0
        state.pause_count = 2

    async def _run(self, *, expect_panel: bool = True) -> reconnect.ReconnectResult:
        with (
            patch.object(reconnect, "reset_pg") as reset_mock,
            patch.object(reconnect, "get_pg", return_value=self.panel) as get_mock,
        ):
            result = await reconnect.reconnect_panel()
            if expect_panel:
                reset_mock.assert_called_once()
                get_mock.assert_called_once()
            else:
                reset_mock.assert_not_called()
                get_mock.assert_not_called()
            return result

    async def test_success_unpauses_circuit_and_returns_latency(self) -> None:
        self._pause_circuit()
        self.assertTrue(resilience.circuit_status()["paused"])

        def _reset_and_clear() -> None:
            resilience.reset_read_circuits()

        with (
            patch.object(reconnect, "reset_pg", side_effect=_reset_and_clear),
            patch.object(reconnect, "get_pg", return_value=self.panel),
        ):
            result = await reconnect.reconnect_panel()

        self.assertTrue(result.ok)
        self.assertIsNotNone(result.latency_ms)
        self.assertGreaterEqual(result.latency_ms, 0)
        self.assertIsNone(result.failure_kind)
        self.assertFalse(resilience.circuit_status()["paused"])
        self.assertEqual(self.calls, 1)
        key, message = reconnect.flash_for_result(result)
        self.assertEqual(key, "ok")
        self.assertIn("اتصال برقرار شد", message)
        self.assertIn("ms", message)

    async def test_timeout_maps_to_timeout_kind(self) -> None:
        self.answer = httpx.ReadTimeout(HOSTILE)
        result = await self._run()
        self.assertFalse(result.ok)
        self.assertEqual(result.failure_kind, "timeout")
        _key, message = reconnect.flash_for_result(result)
        self.assertEqual(message, health.KIND_LABELS["timeout"])
        self.assertNotIn("SECRET", message)
        self.assertNotIn("evil.example", message)

    async def test_connect_error_maps_to_connect_kind(self) -> None:
        self.answer = httpx.ConnectError(f"Connection refused {HOSTILE}")
        result = await self._run()
        self.assertFalse(result.ok)
        self.assertEqual(result.failure_kind, "connect")

    async def test_gateway_502_maps_to_gateway_kind(self) -> None:
        self.answer = 502
        result = await self._run()
        self.assertFalse(result.ok)
        self.assertEqual(result.failure_kind, "gateway")
        _key, message = reconnect.flash_for_result(result)
        self.assertEqual(message, health.KIND_LABELS["gateway"])
        self.assertNotIn(HOSTILE, message)

    async def test_http_401_maps_to_auth_without_polluting_link_health(self) -> None:
        before = health.snapshot()["recent_failure_count"]
        self.answer = 401
        result = await self._run()
        self.assertFalse(result.ok)
        self.assertEqual(result.failure_kind, "auth")
        _key, message = reconnect.flash_for_result(result)
        self.assertEqual(message, health.KIND_LABELS["auth"])
        self.assertEqual(health.snapshot()["recent_failure_count"], before)

    async def test_http_403_maps_to_auth_without_polluting_link_health(self) -> None:
        before = health.snapshot()["recent_failure_count"]
        self.answer = 403
        result = await self._run()
        self.assertFalse(result.ok)
        self.assertEqual(result.failure_kind, "auth")
        _key, message = reconnect.flash_for_result(result)
        self.assertEqual(message, health.KIND_LABELS["auth"])
        self.assertEqual(health.snapshot()["recent_failure_count"], before)

    async def test_http_404_maps_to_client_error_without_polluting_link_health(self) -> None:
        before = health.snapshot()["recent_failure_count"]
        self.answer = 404
        result = await self._run()
        self.assertFalse(result.ok)
        self.assertEqual(result.failure_kind, "client_error")
        _key, message = reconnect.flash_for_result(result)
        self.assertEqual(message, health.KIND_LABELS["client_error"])
        self.assertEqual(health.snapshot()["recent_failure_count"], before)

    async def test_http_503_maps_to_gateway_and_is_counted(self) -> None:
        before = health.snapshot()["recent_failure_count"]
        self.answer = 503
        result = await self._run()
        self.assertFalse(result.ok)
        self.assertEqual(result.failure_kind, "gateway")
        snap = health.snapshot()
        self.assertGreater(snap["recent_failure_count"], before)
        counts = {row["kind"]: row["count"] for row in snap["recent_failures"]}
        self.assertGreaterEqual(counts.get("gateway", 0), 1)

    async def test_rate_limit_does_not_call_panel(self) -> None:
        first = await self._run()
        self.assertTrue(first.ok)
        self.assertEqual(self.calls, 1)
        second = await self._run(expect_panel=False)
        self.assertTrue(second.rate_limited)
        self.assertFalse(second.ok)
        self.assertEqual(self.calls, 1)
        _key, message = reconnect.flash_for_result(second)
        self.assertEqual(message, reconnect.RATE_LIMIT_MESSAGE)

    async def test_concurrent_calls_execute_once(self) -> None:
        gate = asyncio.Event()
        release = asyncio.Event()

        async def slow_probe(*, timeout: httpx.Timeout) -> httpx.Response:
            gate.set()
            await release.wait()
            return await _FakePanel.get_system_probe(self.panel, timeout=timeout)

        self.panel.get_system_probe = slow_probe  # type: ignore[method-assign]

        async def one() -> reconnect.ReconnectResult:
            with (
                patch.object(reconnect, "reset_pg"),
                patch.object(reconnect, "get_pg", return_value=self.panel),
            ):
                return await reconnect.reconnect_panel()

        task_a = asyncio.create_task(one())
        await gate.wait()
        task_b = asyncio.create_task(one())
        # Give B a chance to block on the lock while A still holds it.
        await asyncio.sleep(0.05)
        self.assertTrue(reconnect.is_reconnect_locked())
        release.set()
        results = await asyncio.gather(task_a, task_b)
        oks = [r for r in results if r.ok]
        limited = [r for r in results if r.rate_limited]
        self.assertEqual(len(oks), 1)
        self.assertEqual(len(limited), 1)
        self.assertEqual(self.calls, 1)


class CardButtonTests(unittest.TestCase):
    def _render(self, snap: dict, *, link_overrides: dict | None = None) -> str:
        from app.services.formatting import format_number

        env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True)
        env.filters["num"] = format_number
        ctx = {"pg_link_health": snap, "csrf_token": "csrf-test-token"}
        if link_overrides:
            ctx.update(link_overrides)
        return env.get_template("_pg_link_health.html").render(**ctx)

    def test_button_is_secondary_when_healthy(self) -> None:
        health.reset()
        health.record_success(0.05)
        html = self._render(health.snapshot())
        self.assertIn("ارتباط مجدد", html)
        self.assertIn('action="/pg-link/reconnect"', html)
        self.assertIn('name="csrf_token"', html)
        self.assertIn("btn-ghost", html)
        self.assertIn('aria-label="ارتباط مجدد با پاسارگارد"', html)

    def test_button_is_primary_when_paused(self) -> None:
        health.reset()
        health.record_success(0.05)
        html = self._render(health.snapshot({"paused": True, "retry_in_sec": 5, "pause_count": 1}))
        self.assertIn("متوقف", html)
        self.assertIn("ارتباط مجدد", html)
        self.assertNotIn("btn-ghost", html)


class RouteAuthzTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        reconnect.reset_reconnect_gate()
        health.reset()
        resilience.reset_read_circuits()
        self.staff: dict = {}
        self.reconnect_mock = AsyncMock(
            return_value=reconnect.ReconnectResult(ok=True, latency_ms=12, failure_kind=None)
        )

        async def require_admin() -> dict:
            # Mirror production role gate only; owner check lives in the route
            # so the denied-flash path is reachable for non-owner admins.
            if self.staff.get("role") != "admin":
                raise HTTPException(status_code=403, detail="forbidden")
            return self.staff

        app = FastAPI()

        @app.middleware("http")
        async def csrf_mw(request: Request, call_next):
            if request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
                cookie = (request.cookies.get(CSRF_COOKIE) or "").strip()
                submitted = await extract_csrf_from_request(request)
                if not cookie or not submitted or cookie != submitted:
                    return JSONResponse({"detail": "csrf"}, status_code=403)
            return await call_next(request)

        from app.api.pg_link_pages import register_pg_link_pages

        register_pg_link_pages(app, require_admin=require_admin)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        self._patcher = patch("app.api.pg_link_pages.reconnect_panel", self.reconnect_mock)
        self._patcher.start()

    async def asyncTearDown(self) -> None:
        self._patcher.stop()
        await self.client.aclose()
        reconnect.reset_reconnect_gate()

    def _owner(self) -> dict:
        return {
            "role": "admin",
            "username": "owner",
            "org_principal_id": 1,
            "org_depth": 0,
            "org_parent_id": None,
            "org_status": "active",
            "permissions": [reconnect.PG_LINK_RECONNECT_PERMISSION],
        }

    async def _post(self, *, csrf: bool = True) -> httpx.Response:
        data = {}
        headers = {}
        cookies = {}
        if csrf:
            token = "reconnect-csrf-token"
            data[CSRF_FORM_FIELD] = token
            cookies[CSRF_COOKIE] = token
        return await self.client.post(
            "/pg-link/reconnect",
            data=data,
            cookies=cookies,
            headers=headers,
            follow_redirects=False,
        )

    async def test_owner_success_redirects_with_persian_flash(self) -> None:
        self.staff = self._owner()
        response = await self._post()
        self.assertEqual(response.status_code, 303)
        location = unquote(response.headers["location"])
        self.assertTrue(location.startswith("/home?ok="))
        self.assertIn("اتصال برقرار شد", location)
        self.assertIn("12 ms", location)
        self.reconnect_mock.assert_awaited_once()

    async def test_non_owner_admin_gets_denied_flash_and_skips_panel(self) -> None:
        self.staff = {"role": "admin", "username": "legacy-admin"}  # no owner principal
        response = await self._post()
        self.assertEqual(response.status_code, 303)
        location = unquote(response.headers["location"])
        self.assertTrue(location.startswith("/home?err="))
        self.assertIn(DENIED_MESSAGE, location)
        self.reconnect_mock.assert_not_awaited()

    async def test_reseller_staff_and_anonymous_are_forbidden(self) -> None:
        cases = [
            {"role": "reseller", "bot_user_id": 9, "permissions": [reconnect.PG_LINK_RECONNECT_PERMISSION]},
            {"role": "pg_staff", "permissions": [reconnect.PG_LINK_RECONNECT_PERMISSION]},
            {},
        ]
        for staff in cases:
            self.staff = staff
            with self.subTest(staff=staff):
                response = await self._post()
                self.assertEqual(response.status_code, 403)
        self.reconnect_mock.assert_not_awaited()

    async def test_csrf_rejection_does_not_touch_panel(self) -> None:
        self.staff = self._owner()
        response = await self._post(csrf=False)
        self.assertEqual(response.status_code, 403)
        self.reconnect_mock.assert_not_awaited()

    async def test_hostile_exception_text_never_appears_in_flash(self) -> None:
        self.reconnect_mock.return_value = reconnect.ReconnectResult(
            ok=False, latency_ms=None, failure_kind="connect"
        )
        self.staff = self._owner()
        response = await self._post()
        location = unquote(response.headers["location"])
        self.assertTrue(location.startswith("/home?err="))
        self.assertIn(health.KIND_LABELS["connect"], location)
        self.assertNotIn("SECRET", location)
        self.assertNotIn("evil", location)
        self.assertNotIn("Traceback", location)


class HostileLoginFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        health.reset()
        resilience.reset_read_circuits()
        reconnect.reset_reconnect_gate()

    async def asyncTearDown(self) -> None:
        reconnect.reset_reconnect_gate()
        health.reset()
        resilience.reset_read_circuits()

    async def test_login_error_flash_stays_safe(self) -> None:
        panel = MagicMock()
        panel.get_system_probe = AsyncMock(
            side_effect=PasarGuardError(HOSTILE, status_code=502, body=HOSTILE)
        )

        with (
            patch.object(reconnect, "reset_pg"),
            patch.object(reconnect, "get_pg", return_value=panel),
        ):
            result = await reconnect.reconnect_panel()

        self.assertFalse(result.ok)
        self.assertEqual(result.failure_kind, "gateway")
        _key, message = reconnect.flash_for_result(result)
        self.assertEqual(message, health.KIND_LABELS["gateway"])
        self.assertNotIn("SECRET", message)
        self.assertNotIn("evil.example", message)

    async def test_login_401_maps_to_auth(self) -> None:
        panel = MagicMock()
        panel.get_system_probe = AsyncMock(
            side_effect=PasarGuardError("login rejected", status_code=401, body="bad creds")
        )

        with (
            patch.object(reconnect, "reset_pg"),
            patch.object(reconnect, "get_pg", return_value=panel),
        ):
            result = await reconnect.reconnect_panel()

        self.assertFalse(result.ok)
        self.assertEqual(result.failure_kind, "auth")
        _key, message = reconnect.flash_for_result(result)
        self.assertEqual(message, health.KIND_LABELS["auth"])


if __name__ == "__main__":
    unittest.main()
