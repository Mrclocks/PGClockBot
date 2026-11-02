"""Web reads remain bounded, isolated and fail closed during panel outages."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
import unittest
from collections.abc import Awaitable, Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.panel_connection import PanelConnectionMiddleware, finish_panel_authentication
from app.config import Settings
from app.db import Base
from app.services import pg_read_resilience as resilience
from app.services.pasarguard import PasarGuardClient, PasarGuardError
from app.services.pg_access import resolve_acl_from_client
from app.services.pg_staff_access import enforce_pg_admin_web_gate
from app.services.ux20 import check_pg_connection


class PanelReadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        resilience.reset_read_circuits()
        self.clients: list[PasarGuardClient] = []
        self.settings = Settings(
            _env_file=None, PG_BASE_URL="https://127.0.0.1", PG_USERNAME="owner",
            PG_PASSWORD="test-password", PG_API_KEY="", PG_ACCESS_TOKEN="",
        )

    async def asyncTearDown(self) -> None:
        for client in self.clients:
            await client.close()
        resilience.reset_read_circuits()

    async def make_client(
        self, handler: Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]],
        *, username: str = "shop_a",
    ) -> PasarGuardClient:
        with patch("app.services.pasarguard.get_settings", return_value=self.settings):
            client = PasarGuardClient(username=username, access_token="test-token")
        await client._client.aclose()
        client._api_base_resolved = True
        client._client = httpx.AsyncClient(base_url=client.base_url, transport=httpx.MockTransport(handler))
        self.clients.append(client)
        return client

    async def test_sequential_reads_share_one_deadline(self) -> None:
        calls = []

        async def slow(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            await asyncio.sleep(0.04)
            return httpx.Response(200, json={"username": "shop_a"})

        client = await self.make_client(slow)
        started = time.monotonic()
        with resilience.web_read_budget(seconds=0.06):
            await client.request("GET", "/first")
            with self.assertRaises(resilience.PgReadUnavailable):
                await client.request("GET", "/second")
            with self.assertRaises(resilience.PgReadUnavailable):
                await client.request("GET", "/third")
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertEqual(calls, ["/first", "/second"])

    async def test_outage_stops_fallbacks_without_revoking_staff(self) -> None:
        calls = []

        def offline(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            return httpx.Response(503, text="private provider detail")

        client = await self.make_client(offline)
        session = AsyncMock()
        with (
            patch("app.services.pasarguard.get_pg", return_value=client),
            patch("app.services.pg_staff_access._PG_GATE_CACHE", {}),
            patch("app.services.pg_staff_access.revoke_web_access", new=AsyncMock()) as revoke,
            resilience.web_read_budget(),
        ):
            allowed, message = await enforce_pg_admin_web_gate(session, "shop_a")
        self.assertFalse(allowed)
        self.assertNotIn("private provider detail", message)
        revoke.assert_not_awaited()
        self.assertEqual(calls, ["/api/admin"])

    async def test_failed_acl_never_reuses_cookie_or_owner_permissions(self) -> None:
        def offline(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("offline", request=request)

        client = await self.make_client(offline, username="shop_b")
        with resilience.web_read_budget():
            features, role, admin = await resolve_acl_from_client(client, username="shop_b")
        self.assertEqual(features, [])
        self.assertIsNone(role)
        self.assertIsNone(admin)

    async def test_circuit_shares_health_but_never_shop_data(self) -> None:
        # Pause mechanics: confirm the outage on the first failure.
        self.enterContext(patch.object(resilience, "FAILURES_TO_PAUSE", 1))
        calls = []

        def respond(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            return httpx.Response(503, json={})

        first = await self.make_client(respond, username="shop_a")
        second = await self.make_client(respond, username="shop_b")
        with resilience.web_read_budget():
            with self.assertRaises(resilience.PgReadUnavailable):
                await first.get_current_admin()
        with resilience.web_read_budget():
            with self.assertRaises(resilience.PgReadUnavailable):
                await second.get_current_admin()
        self.assertEqual(len(calls), 1)

    async def test_read_cache_isolated_between_shops_and_recreated_credentials(self) -> None:
        def response(name: str) -> Callable[[httpx.Request], httpx.Response]:
            return lambda request: httpx.Response(200, json={"username": name})

        first = await self.make_client(response("shop_a"))
        second = await self.make_client(response("shop_b"), username="shop_b")
        rotated = await self.make_client(response("shop_a_new_credentials"))
        with resilience.web_read_budget():
            self.assertEqual((await first.get_current_admin())["username"], "shop_a")
            self.assertEqual((await second.get_current_admin())["username"], "shop_b")
            self.assertEqual((await rotated.get_current_admin())["username"], "shop_a_new_credentials")

    async def test_forbidden_endpoint_does_not_open_circuit(self) -> None:
        def respond(request: httpx.Request) -> httpx.Response:
            status = 403 if request.url.path == "/forbidden" else 200
            return httpx.Response(status, json={"username": "shop_a"})

        client = await self.make_client(respond)
        with resilience.web_read_budget():
            with self.assertRaises(PasarGuardError):
                await client.request("GET", "/forbidden")
            self.assertEqual((await client.get_current_admin())["username"], "shop_a")

    async def test_parallel_reads_are_bounded_without_a_waiting_queue(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def slow(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == resilience.MAX_CONCURRENT_READS:
                entered.set()
            await release.wait()
            return httpx.Response(200, json={})

        client = await self.make_client(slow)
        with resilience.web_read_budget():
            pending = [asyncio.create_task(client.request("GET", f"/read/{i}"))
                       for i in range(resilience.MAX_CONCURRENT_READS)]
            await asyncio.wait_for(entered.wait(), timeout=1)
            with self.assertRaises(resilience.PgReadUnavailable):
                await client.request("GET", "/overflow")
            release.set()
            await asyncio.gather(*pending)
        self.assertEqual(calls, resilience.MAX_CONCURRENT_READS)

    async def test_only_one_read_probes_recovery_and_success_closes_circuit(self) -> None:
        # Pause mechanics: confirm the outage on the first failure.
        self.enterContext(patch.object(resilience, "FAILURES_TO_PAUSE", 1))
        now = [100.0]
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def respond(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise httpx.ConnectError("offline", request=request)
            if calls == 2:
                entered.set()
                await release.wait()
            return httpx.Response(200, json={"ok": True})

        client = await self.make_client(respond)
        with patch.object(resilience, "time", SimpleNamespace(monotonic=lambda: now[0])):
            with resilience.web_read_budget():
                with self.assertRaises(resilience.PgReadUnavailable):
                    await client.request("GET", "/offline")
            now[0] += resilience.READ_COOLDOWN_SEC + 1
            with resilience.web_read_budget():
                probe = asyncio.create_task(client.request("GET", "/recovery"))
                await asyncio.wait_for(entered.wait(), timeout=1)
                with self.assertRaises(resilience.PgReadUnavailable):
                    await client.request("GET", "/duplicate-probe")
                release.set()
                await probe
                await client.request("GET", "/after-recovery")
        self.assertEqual(calls, 3)

    async def test_mutation_is_not_cancelled_or_retried_by_read_deadline(self) -> None:
        calls = []

        async def slow(request: httpx.Request) -> httpx.Response:
            calls.append(request.method)
            await asyncio.sleep(0.03)
            return httpx.Response(200, json={"id": 7})

        client = await self.make_client(slow)
        with resilience.web_read_budget(seconds=0):
            created = await client.create_user({"username": "customer"})
        self.assertEqual(created["id"], 7)
        self.assertEqual(calls, ["POST"])

    async def test_unknown_mutation_outcome_still_propagates_without_retry(self) -> None:
        calls = []

        def unknown(request: httpx.Request) -> httpx.Response:
            calls.append(request.method)
            raise httpx.ReadTimeout("outcome unknown", request=request)

        client = await self.make_client(unknown)
        with resilience.web_read_budget():
            with self.assertRaisesRegex(httpx.ReadTimeout, "outcome unknown"):
                await client.create_user({"username": "customer"})
        self.assertEqual(calls, ["POST"])

    async def test_token_acquisition_uses_read_budget(self) -> None:
        client = await self.make_client(lambda request: httpx.Response(200))
        client._token = None
        client._login_password = "test-password"
        await client._ensure_safe_target()

        async def slow(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(1)
            return httpx.Response(200, json={"access_token": "new-token"})

        send = AsyncMock(side_effect=slow)
        await client._client.aclose()
        client._client = httpx.AsyncClient(base_url=client.base_url, transport=httpx.MockTransport(send))
        with patch("app.services.pasarguard.new_panel_http_client", side_effect=AssertionError("new connection")):
            with resilience.web_read_budget(seconds=0.02):
                with self.assertRaises(resilience.PgReadUnavailable):
                    await client.ensure_token()
        self.assertEqual(send.await_count, 1)
        self.assertIsNone(client._token)

    async def test_bot_and_job_reads_keep_their_existing_timeout(self) -> None:
        async def respond(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0.02)
            return httpx.Response(200, json={"username": "shop_a"})

        client = await self.make_client(respond)
        with resilience.web_read_budget(seconds=0):
            with self.assertRaises(resilience.PgReadUnavailable):
                await client.get_current_admin()
        self.assertEqual((await client.get_current_admin())["username"], "shop_a")

    async def test_saved_token_does_not_report_disconnected_panel_as_connected(self) -> None:
        def offline(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("offline", request=request)

        client = await self.make_client(offline)
        with patch("app.services.pasarguard.get_pg", return_value=client), resilience.web_read_budget():
            health = await check_pg_connection()
            self.assertIsNone(health["ok"])
            self.assertTrue(health["unchecked"])

    async def test_limited_role_health_uses_live_self_read(self) -> None:
        def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/system":
                return httpx.Response(403, json={})
            return httpx.Response(200, json={"username": "shop_a"})

        client = await self.make_client(respond)
        with patch("app.services.pasarguard.get_pg", return_value=client), resilience.web_read_budget():
            self.assertTrue((await check_pg_connection())["ok"])


    async def test_web_response_is_bounded_and_contains_safe_connection_status(self) -> None:
        async def slow(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(1)
            return httpx.Response(200, json={"username": "shop_a"})

        pg = await self.make_client(slow)
        app = FastAPI()
        app.add_middleware(PanelConnectionMiddleware)

        @app.get("/home")
        async def home(request: Request) -> HTMLResponse:
            request.state.pg_read_budget.deadline = time.monotonic() + 0.02
            features, _role, _admin = await resolve_acl_from_client(pg, username="shop_a")
            return HTMLResponse("local dashboard" if not features else "unexpected grant")

        started = time.monotonic()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as browser:
            response = await browser.get("/home")
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertEqual(response.text, "local dashboard")
        self.assertEqual(response.headers["x-pg-read-status"], "timeout")

    async def test_api_requests_do_not_inherit_web_deadlines(self) -> None:
        app = FastAPI()
        app.add_middleware(PanelConnectionMiddleware)

        @app.get("/api/mini/health")
        async def health(request: Request) -> dict[str, bool]:
            return {"has_web_budget": hasattr(request.state, "pg_read_budget")}

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as browser:
            self.assertEqual((await browser.get("/api/mini/health")).json(), {"has_web_budget": False})

    async def test_slow_dns_does_not_block_the_loop_or_send_credentials(self) -> None:
        self.settings.pg_base_url = "https://panel.test"
        calls = []
        dns_threads = []

        def slow_dns(*args: Any, **kwargs: Any) -> list[tuple[int, int, int, str, tuple[str, int]]]:
            dns_threads.append(threading.current_thread())
            time.sleep(0.06)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]

        with patch("socket.getaddrinfo", side_effect=slow_dns):
            client = await self.make_client(lambda request: calls.append(request) or httpx.Response(200, json={}))
            started = time.monotonic()
            with resilience.web_read_budget(seconds=0.02):
                read = asyncio.create_task(client.get_current_admin())
                await asyncio.sleep(0.005)
                self.assertFalse(read.done())
                with self.assertRaises(resilience.PgReadUnavailable):
                    await read
            self.assertLess(time.monotonic() - started, 0.05)
            await asyncio.sleep(0.07)
        self.assertEqual(calls, [])
        self.assertEqual(len(dns_threads), 1)
        self.assertIsNot(dns_threads[0], threading.main_thread())

    async def test_dns_validation_rejects_metadata_before_any_request(self) -> None:
        self.settings.pg_base_url = "https://panel.test"
        calls = []
        with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))]):
            client = await self.make_client(lambda request: calls.append(request) or httpx.Response(200, json={}))
            with resilience.web_read_budget():
                with self.assertRaises(resilience.PgReadUnavailable):
                    await client.get_current_admin()
        self.assertEqual(calls, [])

    async def test_shared_dns_validation_preserves_each_api_base_path(self) -> None:
        with resilience.web_read_budget():
            self.assertEqual(await resilience.validate_panel_base("https://127.0.0.1/dashboard"), "https://127.0.0.1/dashboard")
            self.assertEqual(await resilience.validate_panel_base("https://127.0.0.1"), "https://127.0.0.1")

    async def test_outage_gate_retries_after_cooldown_instead_of_caching_denial_for_45_seconds(self) -> None:
        from app.services import pg_staff_access

        now = [100.0]
        gate = AsyncMock(side_effect=[("unreachable", None), ("ok", {"username": "shop_a"})])
        with (
            patch.object(pg_staff_access, "time", SimpleNamespace(monotonic=lambda: now[0])),
            patch.object(pg_staff_access, "_PG_GATE_CACHE", {}),
            patch.object(pg_staff_access, "fetch_pg_admin_gate", gate),
        ):
            self.assertFalse((await enforce_pg_admin_web_gate(AsyncMock(), "shop_a"))[0])
            now[0] += resilience.READ_COOLDOWN_SEC + 0.1
            self.assertTrue((await enforce_pg_admin_web_gate(AsyncMock(), "shop_a"))[0])
        self.assertEqual(gate.await_count, 2)

    async def test_authenticated_owner_can_use_local_home_during_outage_and_pg_body_stays_denied(self) -> None:
        from app.api import app as api
        from app.services.org_principals import ensure_owner_principal
        from app.services.pg_access import clear_platform_pg_capability_cache

        calls = []

        def offline(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            raise httpx.ConnectError("offline", request=request)

        pg = await self.make_client(offline)
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            await ensure_owner_principal(session)
            await session.commit()
        clear_platform_pg_capability_cache()
        cookie = URLSafeTimedSerializer("test-web-secret", salt="pgclock-session").dumps(
            {"role": "admin", "username": "owner", "sv": "test-version", "pg_permissions": ["pg_overview"]}
        )
        try:
            with (
                TemporaryDirectory() as directory,
                patch.object(api, "DATA_DIR", Path(directory)),
                patch.object(api, "SessionLocal", sessions),
                patch.object(api, "is_setup_complete", return_value=True),
                patch.object(api, "ensure_web_secret", return_value="test-web-secret"),
                patch.object(api, "admin_session_version", return_value="test-version"),
                patch("app.config.get_settings", return_value=self.settings),
                patch("app.services.pasarguard.get_pg", return_value=pg),
                patch("app.services.ssl_certs.ensure_dirs"),
            ):
                app = api.create_api_app()
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as browser:
                    browser.cookies.set("session", cookie)
                    home = await browser.get("/home")
                    self.assertEqual(home.status_code, 200)
                    self.assertIn('id="home-dash"', home.text)
                    self.assertIn("وضعیت اتصال", home.text)
                    self.assertIn("اطلاعات پاسارگارد در دسترس نیست", home.text)
                    self.assertIn('id="panel-widget-status"', home.text)
                    self.assertNotRegex(home.text, r'<div[^>]*id="home-dash"[^>]*>\s*</div>')
                    denied = await browser.get("/pg/body")
                    self.assertEqual(denied.status_code, 303)
                    self.assertEqual(denied.headers["location"], "/home")
                    browser.cookies.clear()
                    self.assertEqual((await browser.get("/home/body")).status_code, 303)
                    password_threads = []

                    def verify(username: str, password: str) -> bool:
                        password_threads.append(threading.current_thread())
                        return True

                    with (
                        patch.object(api, "verify_web_admin", side_effect=verify),
                        patch.object(api, "load_web_admin", return_value={"username": "owner"}),
                    ):
                        login = await browser.post("/login", data={"username": "owner", "password": "fake-password"})
                    self.assertEqual(login.status_code, 303)
                    self.assertEqual(len(password_threads), 1)
                    self.assertIsNot(password_threads[0], threading.main_thread())
                    browser.cookies.clear()

                    def rotated_verify(username: str, password: str) -> bool:
                        api.admin_session_version.return_value = "rotated-version"
                        return True

                    with (
                        patch.object(api, "verify_web_admin", side_effect=rotated_verify),
                        patch.object(api, "load_web_admin", return_value={"username": "owner"}),
                    ):
                        rotated = await browser.post("/login", data={"username": "owner", "password": "old-password"})
                    self.assertEqual(rotated.status_code, 303)
                    decoded = URLSafeTimedSerializer("test-web-secret", salt="pgclock-session").loads(rotated.cookies["session"])
                    self.assertEqual(decoded["sv"], "test-version")
                    invalid = await browser.get("/home")
                    self.assertEqual(invalid.status_code, 303)
                    self.assertEqual(invalid.headers["location"], "/login")
            self.assertEqual(calls, ["/api/admin"])
        finally:
            clear_platform_pg_capability_cache()
            await engine.dispose()

    async def test_principal_password_check_runs_off_the_loop_and_still_denies_disabled_identity(self) -> None:
        from app.services import principal_web_identity as identities

        password_threads = []

        def verify(password: str, password_hash: str) -> bool:
            password_threads.append(threading.current_thread())
            return True

        with (
            patch.object(identities, "get_web_identity_by_username", new=AsyncMock(
                return_value=SimpleNamespace(web_password_hash="fake-hash", is_active=False)
            )),
            patch.object(identities, "verify_password_hash", side_effect=verify),
        ):
            with self.assertRaises(identities.PrincipalWebIdentityError) as denied:
                await identities.authenticate_level1_web(AsyncMock(), username="shop_a", password="fake-password")
        self.assertEqual(denied.exception.code, "identity_disabled")
        self.assertEqual(len(password_threads), 1)
        self.assertIsNot(password_threads[0], threading.main_thread())

    async def test_api_base_discovery_recovers_after_initial_outage(self) -> None:
        self.settings.pg_base_url = "https://127.0.0.1/dashboard"
        calls = []
        offline = [True]

        def respond(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if offline[0]:
                raise httpx.ConnectError("offline", request=request)
            if request.url.path == "/openapi.json":
                return httpx.Response(200, text="PasarGuard")
            return httpx.Response(404)

        client = await self.make_client(respond)
        client._api_base_resolved = False
        def probe_client(base_url: str, **kwargs: Any) -> httpx.AsyncClient:
            return httpx.AsyncClient(base_url=base_url, transport=httpx.MockTransport(respond))

        with patch("app.services.pasarguard.new_panel_http_client", side_effect=probe_client):
            with resilience.web_read_budget():
                await client._ensure_api_base()
            self.assertFalse(client._api_base_resolved)
            offline[0] = False
            resilience.reset_read_circuits()
            with resilience.web_read_budget():
                await client._ensure_api_base()
        self.assertTrue(client._api_base_resolved)
        self.assertEqual(client.base_url, "https://127.0.0.1")
        self.assertEqual(calls, ["/dashboard/openapi.json", "/dashboard/openapi.json", "/openapi.json"])

    async def test_older_success_does_not_resume_reads_after_another_request_fails(self) -> None:
        # Pause mechanics: confirm the outage on the first failure.
        self.enterContext(patch.object(resilience, "FAILURES_TO_PAUSE", 1))
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def respond(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path == "/older":
                entered.set()
                await release.wait()
                return httpx.Response(200, json={"ok": True})
            raise httpx.ConnectError("offline", request=request)

        client = await self.make_client(respond)
        with resilience.web_read_budget():
            older = asyncio.create_task(client.request("GET", "/older"))
            await asyncio.wait_for(entered.wait(), timeout=1)
            with self.assertRaises(resilience.PgReadUnavailable):
                await client.request("GET", "/offline")
            release.set()
            await older
        with resilience.web_read_budget():
            with self.assertRaises(resilience.PgReadUnavailable):
                await client.request("GET", "/retry")
        self.assertEqual(calls, ["/older", "/offline"])

    async def test_mutation_followup_and_spawned_work_do_not_inherit_auth_read_deadline(self) -> None:
        calls = []

        async def respond(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.method == "POST":
                await asyncio.sleep(0.04)
            return httpx.Response(200, json={"id": 7, "username": "shop_a"})

        pg = await self.make_client(respond)
        await pg._ensure_safe_target()
        app = FastAPI()
        app.add_middleware(PanelConnectionMiddleware)

        async def authorize(request: Request) -> None:
            request.state.pg_read_budget.deadline = time.monotonic() + 0.02
            await pg.get_current_admin()
            finish_panel_authentication(request)

        @app.post("/orders/approve", dependencies=[Depends(authorize)])
        async def approve() -> dict[str, bool]:
            await pg.create_user({"username": "customer"})
            verified = await asyncio.create_task(pg.request("GET", "/verification"))
            return {"verified": verified["id"] == 7}

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as browser:
            self.assertEqual((await browser.post("/orders/approve")).json(), {"verified": True})
        self.assertEqual(calls, ["/api/admin", "/api/user", "/verification"])
