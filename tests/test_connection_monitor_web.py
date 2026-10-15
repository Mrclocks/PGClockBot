"""The «وضعیت اتصال» settings tab: Owner-only access, safe errors, host management and the idle scheduler job."""

from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import ConnectionMonitorState, ConnectionMonitorTarget, OrgPrincipal
from app.jobs import connection_status as monitor_job
from app.services.connection_monitor_panel import MonitorCatalog
from app.services.users import clear_settings_cache, get_all_settings
from app.services.xray_probe_resources import XrayProbeError
from tests.test_update_owner_only import OWNER_SESSION, PRINCIPAL_SESSION, SHOP_SESSION, RoutesHarness

BASE = "/connection-monitor"
SECRET = "super-secret-token-value"
FIXTURE = Path(__file__).parent / "fixtures/connection_monitor_catalog.json"
ROUTES = (
    ("GET", BASE + "/status"), ("POST", BASE + "/enabled"), ("POST", BASE + "/account"), ("GET", BASE + "/hosts"),
    ("POST", BASE + "/targets"), ("POST", BASE + "/targets/1/edit"), ("POST", BASE + "/targets/1/check"),
    ("POST", BASE + "/targets/1/pause"),
)


class MonitorWebHarness(RoutesHarness):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        clear_settings_cache()
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([OrgPrincipal(id=1, depth=0, status="active"), OrgPrincipal(id=2, parent_id=1, depth=1, status="active")])
            session.add(ConnectionMonitorState(pg_user_id=900, pg_username="monitor-only", account_revision="one",
                                               subscription_token_enc="encrypted"))
            await session.commit()
        catalog = MonitorCatalog(**json.loads(FIXTURE.read_text()), checked_at=datetime.now(timezone.utc))
        reader = AsyncMock(return_value=catalog)
        for patcher in (
            patch("app.services.connection_monitor_preferences.read_monitor_catalog", reader),
            patch("app.services.connection_monitor_admin.read_monitor_catalog", reader),
            patch("app.services.connection_monitor_admin.reveal_monitor_token", AsyncMock(return_value="synthetic")),
            patch("app.services.connection_monitor_admin.XrayBinary.verify", return_value=Path("/synthetic/xray")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def send(self, session_payload: dict, method: str, path: str, data: dict | None = None) -> httpx.Response:
        headers = {"cookie": f"session={self.signer.dumps(session_payload)}; csrf=token",
                   "x-csrf-token": "token", "origin": "http://panel.test"}
        return await self.client.request(method, path, headers=headers, data=data if method == "POST" else None)

    async def select_host(self, host_id: int = 41) -> int:
        response = await self.send(OWNER_SESSION, "POST", BASE + "/targets", {"host_id": str(host_id)})
        self.assertEqual(response.status_code, 200, response.text)
        async with self.Session() as session:
            return await session.scalar(select(ConnectionMonitorTarget.id).where(ConnectionMonitorTarget.pg_host_id == host_id))


class AccessTests(MonitorWebHarness):
    async def test_only_the_owner_can_use_any_monitor_route(self) -> None:
        for label, session_payload in (("shop", SHOP_SESSION), ("sub-principal", PRINCIPAL_SESSION)):
            for method, path in ROUTES:
                with self.subTest(who=label, method=method, path=path):
                    response = await self.send(session_payload, method, path, {"enabled": "1", "host_id": "41"})
                    self.assertNotEqual(response.status_code, 200)
        async with self.Session() as session:
            state = await session.scalar(select(ConnectionMonitorState))
            self.assertFalse(state.enabled)
            self.assertIsNone(await session.scalar(select(ConnectionMonitorTarget.id)))

    async def test_anonymous_requests_are_refused(self) -> None:
        response = await self.client.get(BASE + "/status")
        self.assertNotEqual(response.status_code, 200)

    async def test_the_settings_tab_renders_for_the_owner_and_is_denied_to_a_shop(self) -> None:
        owner = await self.send(OWNER_SESSION, "GET", "/settings?tab=connection_status")
        self.assertEqual(owner.status_code, 200)
        self.assertIn('id="cm-panel"', owner.text)
        self.assertIn("connection-monitor-admin.js", owner.text)
        shop = await self.send(SHOP_SESSION, "GET", "/settings?tab=connection_status")
        self.assertNotIn('id="cm-panel"', shop.text)


class StatusTests(MonitorWebHarness):
    async def test_status_reports_runtime_core_and_targets_without_secrets(self) -> None:
        await self.select_host()
        response = await self.send(OWNER_SESSION, "GET", BASE + "/status")
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertFalse(data["enabled"])
        self.assertTrue(data["has_account"])
        self.assertEqual(data["account_username"], "monitor-only")
        self.assertEqual(data["core"], "not_configured")
        self.assertEqual(len(data["targets"]), 1)
        self.assertFalse(data["targets"][0]["is_public"])
        for forbidden in ("encrypted", "subscription_token", "xray_path", "sha256"):
            self.assertNotIn(forbidden, response.text)

    async def test_core_check_never_returns_the_path_or_checksum(self) -> None:
        deployment = type("Deployment", (), {"connection_monitor_xray_path": "/opt/secret-dir/xray",
                                             "connection_monitor_xray_sha256": "a" * 64})()
        with patch("app.services.connection_monitor_overview.get_settings", return_value=deployment):
            plain = await self.send(OWNER_SESSION, "GET", BASE + "/status")
            ready = await self.send(OWNER_SESSION, "GET", BASE + "/status?verify=1")
            with patch("app.services.connection_monitor_overview.XrayBinary.verify", side_effect=XrayProbeError("core_binary")):
                invalid = await self.send(OWNER_SESSION, "GET", BASE + "/status?verify=1")
        self.assertEqual([item.json()["core"] for item in (plain, ready, invalid)], ["configured", "ready", "invalid"])
        for response in (plain, ready, invalid):
            self.assertNotIn("secret-dir", response.text)
            self.assertNotIn("a" * 64, response.text)

    async def test_invalid_cursor_is_rejected(self) -> None:
        for value in ("-1", "abc", "9" * 11, "1.5"):
            with self.subTest(value=value):
                response = await self.send(OWNER_SESSION, "GET", BASE + "/status?after_id=" + value)
                self.assertEqual(response.status_code, 409)

    async def test_unexpected_failures_never_echo_their_text(self) -> None:
        with patch("app.api.connection_monitor_pages.monitor_overview", AsyncMock(side_effect=RuntimeError(SECRET))):
            response = await self.send(OWNER_SESSION, "GET", BASE + "/status")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(SECRET, response.text)


class ManagementTests(MonitorWebHarness):
    async def test_owner_selects_edits_publishes_checks_and_pauses_a_host(self) -> None:
        target_id = await self.select_host()
        path = f"{BASE}/targets/{target_id}"
        self.assertEqual((await self.send(OWNER_SESSION, "POST", BASE + "/enabled", {"enabled": "1"})).status_code, 200)
        for field, value in (("public_name", "هاست عمومی"), ("display_order", "7"), ("is_public", "1")):
            response = await self.send(OWNER_SESSION, "POST", path + "/edit", {"field": field, "value": value})
            self.assertEqual(response.status_code, 200, response.text)
        row = (await self.send(OWNER_SESSION, "GET", BASE + "/status")).json()["targets"][0]
        self.assertEqual((row["public_name"], row["display_order"], row["is_public"]), ("هاست عمومی", 7, True))
        pause = await self.send(OWNER_SESSION, "POST", path + "/pause")
        self.assertEqual(pause.status_code, 200)
        row = (await self.send(OWNER_SESSION, "GET", BASE + "/status")).json()["targets"][0]
        self.assertFalse(row["enabled"])
        self.assertFalse(row["is_public"])

    async def test_hostile_edit_values_are_rejected_with_a_fixed_message(self) -> None:
        target_id = await self.select_host()
        path = f"{BASE}/targets/{target_id}/edit"
        for field, value in (("is_public", "yes"), ("is_public", "1 "), ("display_order", "-1"), ("display_order", "1e3"),
                             ("display_order", "٣"), ("display_order", "2147483648"), ("public_name", "line\nbreak"),
                             ("public_name", "x" * 65), ("nonsense", "1"), ("", "1")):
            with self.subTest(field=field, value=value):
                response = await self.send(OWNER_SESSION, "POST", path, {"field": field, "value": value})
                self.assertEqual(response.status_code, 409)
                self.assertFalse(response.json()["ok"])
        async with self.Session() as session:
            target = await session.get(ConnectionMonitorTarget, target_id)
            self.assertEqual((target.public_name, target.display_order, target.is_public), ("", 0, False))

    async def test_publishing_requires_a_public_name(self) -> None:
        target_id = await self.select_host()
        response = await self.send(OWNER_SESSION, "POST", f"{BASE}/targets/{target_id}/edit",
                                   {"field": "is_public", "value": "1"})
        self.assertEqual(response.status_code, 409)

    async def test_unknown_action_and_unknown_target_are_refused(self) -> None:
        self.assertEqual((await self.send(OWNER_SESSION, "POST", BASE + "/targets/1/delete")).status_code, 409)
        self.assertEqual((await self.send(OWNER_SESSION, "POST", BASE + "/targets/999/pause")).status_code, 409)

    async def test_enabling_without_an_account_is_refused(self) -> None:
        async with self.Session() as session:
            state = await session.scalar(select(ConnectionMonitorState))
            state.pg_user_id = state.pg_username = state.subscription_token_enc = None
            await session.commit()
        response = await self.send(OWNER_SESSION, "POST", BASE + "/enabled", {"enabled": "1"})
        self.assertEqual(response.status_code, 409)
        for value in ("", "2", "true"):
            with self.subTest(value=value):
                self.assertEqual((await self.send(OWNER_SESSION, "POST", BASE + "/enabled", {"enabled": value})).status_code, 409)

    async def test_changing_the_account_turns_monitoring_off_and_unpublishes(self) -> None:
        target_id = await self.select_host()
        await self.send(OWNER_SESSION, "POST", BASE + "/enabled", {"enabled": "1"})
        await self.send(OWNER_SESSION, "POST", f"{BASE}/targets/{target_id}/edit", {"field": "public_name", "value": "نام"})
        await self.send(OWNER_SESSION, "POST", f"{BASE}/targets/{target_id}/edit", {"field": "is_public", "value": "1"})
        with patch("app.services.connection_monitor_settings._read_account", AsyncMock(return_value=(901, "tok"))), \
                patch("app.services.connection_monitor_settings.encrypt_secret", return_value="enc"):
            response = await self.send(OWNER_SESSION, "POST", BASE + "/account", {"username": "another-probe"})
        self.assertEqual(response.status_code, 200, response.text)
        data = (await self.send(OWNER_SESSION, "GET", BASE + "/status")).json()
        self.assertFalse(data["enabled"])
        self.assertEqual(data["account_username"], "another-probe")
        self.assertFalse(data["targets"][0]["is_public"])

    async def test_host_choices_are_bounded_and_paged(self) -> None:
        data = (await self.send(OWNER_SESSION, "GET", BASE + "/hosts")).json()
        self.assertTrue(data["ok"])
        self.assertLessEqual(len(data["hosts"]), 10)
        self.assertEqual(set(data["hosts"][0]), {"id", "name"})


class TextSettingsTests(MonitorWebHarness):
    async def test_customer_texts_are_saved_from_their_own_tab_only(self) -> None:
        response = await self.send(OWNER_SESSION, "POST", "/settings?tab=connection_status",
                                   {"s_connection_status_title": "عنوان تازه", "s_btn_connection_status": "📡 وضعیت"})
        self.assertIn(response.status_code, (200, 303))
        async with self.Session() as session:
            values = await get_all_settings(session)
        self.assertEqual(values["connection_status_title"], "عنوان تازه")
        self.assertEqual(values["btn_connection_status"], "📡 وضعیت")
        self.assertTrue(values["connection_status_empty"])
        clear_settings_cache()
        other = await self.send(OWNER_SESSION, "POST", "/settings?tab=messages", {"s_shop_empty_text": "x"})
        self.assertIn(other.status_code, (200, 303))
        async with self.Session() as session:
            values = await get_all_settings(session)
        self.assertEqual(values["connection_status_title"], "عنوان تازه")


class IdleJobTests(unittest.IsolatedAsyncioTestCase):
    async def run_job(self, enabled: bool | None) -> AsyncMock:
        class FakeSession:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *args):
                return False

        cycle = AsyncMock()
        with patch.object(monitor_job, "SessionLocal", FakeSession), \
                patch.object(monitor_job, "monitor_runtime_enabled", AsyncMock(return_value=enabled)), \
                patch.object(monitor_job, "run_monitor_cycle", cycle):
            await monitor_job.run_connection_status()
        return cycle

    async def test_disabled_monitor_does_not_start_a_cycle(self) -> None:
        for enabled in (False, None):
            with self.subTest(enabled=enabled):
                (await self.run_job(enabled)).assert_not_awaited()

    async def test_enabled_monitor_starts_a_cycle(self) -> None:
        (await self.run_job(True)).assert_awaited_once()
