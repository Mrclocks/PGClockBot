"""Stop polling when the dedicated PasarGuard monitor user is gone (404)."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from connection_monitor_support import MonitorDatabaseCase

from app.db.models import ConnectionMonitorSnapshot, ConnectionMonitorState, ConnectionMonitorTarget
from app.services.connection_monitor_accounts import MonitorAccountError
from app.services.connection_monitor_panel import MonitorAccount, mark_monitor_user_missing, read_monitor_catalog
from app.services.connection_monitor_worker import run_monitor_cycle
from app.services.connection_status_types import MonitorReason
from app.services.pasarguard import PasarGuardError
from app.services.xray_probe_resources import XrayBinary
from pathlib import Path


TOKEN = "synthetic-monitor-token-123456"
REVISION = hashlib.sha256(f"900:{TOKEN}".encode()).hexdigest()


class MissingUserPollTests(MonitorDatabaseCase):
    async def _seed_platform(self, *, enabled: bool = True) -> int:
        async with self.Session() as session:
            state = ConnectionMonitorState(
                reseller_id=None,
                probe_id="local",
                enabled=enabled,
                pg_user_id=900,
                pg_username="monitor",
                subscription_token_enc="encrypted",
                account_revision=REVISION,
            )
            session.add(state)
            await session.flush()
            target = ConnectionMonitorTarget(
                reseller_id=None,
                subscription_key="cfg-a",
                config_name="A",
                test_enabled=True,
                is_public=False,
                generation="gen1",
                endpoint_count=1,
            )
            session.add(target)
            await session.flush()
            session.add(
                ConnectionMonitorSnapshot(
                    reseller_id=None,
                    target_id=target.id,
                    endpoint_key="ep1",
                    probe_id="local",
                    generation="gen1",
                    status="healthy",
                )
            )
            # Another shop's row must stay enabled.
            other = ConnectionMonitorState(
                reseller_id=2,
                probe_id="local",
                enabled=True,
                pg_user_id=901,
                pg_username="shop-monitor",
                subscription_token_enc="enc2",
                account_revision="other",
            )
            session.add(other)
            await session.commit()
            return int(state.id)

    async def test_get_user_by_id_404_raises_user_missing(self) -> None:
        account = MonitorAccount(900, REVISION, "encrypted", "monitor")
        pg = SimpleNamespace(
            get_user_by_id=AsyncMock(side_effect=PasarGuardError("gone", 404)),
            subscription_xray=AsyncMock(),
        )
        with patch("app.services.connection_monitor_panel.get_pg", return_value=pg), \
             patch("app.services.connection_monitor_panel.reveal_monitor_token", AsyncMock(return_value=TOKEN)):
            with self.assertRaises(MonitorAccountError) as caught:
                await read_monitor_catalog(account)
        self.assertEqual(caught.exception.code, "user_missing")
        pg.subscription_xray.assert_not_awaited()

    async def test_mark_missing_disables_platform_only_and_is_idempotent(self) -> None:
        state_id = await self._seed_platform()
        async with self.Session() as session:
            first = await mark_monitor_user_missing(session, state_id=state_id)
            second = await mark_monitor_user_missing(session, state_id=state_id)
        self.assertTrue(first)
        self.assertTrue(second)
        async with self.Session() as session:
            platform = await session.get(ConnectionMonitorState, state_id)
            shop = await session.scalar(
                select(ConnectionMonitorState).where(ConnectionMonitorState.reseller_id == 2).limit(1)
            )
            target = await session.scalar(
                select(ConnectionMonitorTarget).where(ConnectionMonitorTarget.reseller_id.is_(None)).limit(1)
            )
            snap = await session.scalar(
                select(ConnectionMonitorSnapshot).where(ConnectionMonitorSnapshot.reseller_id.is_(None)).limit(1)
            )
        self.assertFalse(platform.enabled)
        self.assertTrue(shop.enabled)
        self.assertFalse(target.test_enabled)
        self.assertEqual(snap.reason, MonitorReason.USER_MISSING.value)
        self.assertEqual(snap.status, "paused")

    async def test_worker_stops_polling_after_missing_user(self) -> None:
        state_id = await self._seed_platform(enabled=True)
        binary = XrayBinary(Path("/synthetic/xray"), "0" * 64)

        async def missing_catalog(account: MonitorAccount):
            raise MonitorAccountError("user_missing")

        with patch("app.services.connection_monitor_worker.read_monitor_catalog", side_effect=missing_catalog), \
             patch("app.services.connection_monitor_worker.finish_cleanup", AsyncMock()):
            result = await run_monitor_cycle(self.Session, binary)
        self.assertTrue(result.claimed)
        self.assertEqual(result.reason, MonitorReason.USER_MISSING.value)
        async with self.Session() as session:
            platform = await session.get(ConnectionMonitorState, state_id)
            shop = await session.scalar(
                select(ConnectionMonitorState).where(ConnectionMonitorState.reseller_id == 2).limit(1)
            )
            # Next claim must not start while disabled.
            from app.services.connection_monitor_claims import claim_monitor_cycle

            cycle = await claim_monitor_cycle(session, now=datetime.now(timezone.utc))
        self.assertFalse(platform.enabled)
        self.assertTrue(shop.enabled)
        self.assertIsNone(cycle)

    async def test_transient_not_found_does_not_disable_platform_monitor(self) -> None:
        """A setup/blip 404 mapped to not_found must not silence all polling."""
        state_id = await self._seed_platform(enabled=True)
        binary = XrayBinary(Path("/synthetic/xray"), "0" * 64)

        async def not_found_catalog(account: MonitorAccount):
            raise MonitorAccountError("not_found")

        with patch("app.services.connection_monitor_worker.read_monitor_catalog", side_effect=not_found_catalog), \
             patch("app.services.connection_monitor_worker.finish_cleanup", AsyncMock()), \
             patch("app.services.connection_monitor_worker.mark_monitor_user_missing", AsyncMock()) as mark:
            result = await run_monitor_cycle(self.Session, binary)
        self.assertTrue(result.claimed)
        self.assertEqual(result.reason, "account_unavailable")
        self.assertNotEqual(result.reason, MonitorReason.USER_MISSING.value)
        mark.assert_not_awaited()
        async with self.Session() as session:
            platform = await session.get(ConnectionMonitorState, state_id)
        self.assertTrue(platform.enabled)

    async def test_restored_user_can_be_re_enabled(self) -> None:
        from sqlalchemy import update

        state_id = await self._seed_platform(enabled=True)
        async with self.Session() as session:
            await mark_monitor_user_missing(session, state_id=state_id)
        async with self.Session() as session:
            result = await session.execute(
                update(ConnectionMonitorState).where(
                    ConnectionMonitorState.id == state_id,
                    ConnectionMonitorState.reseller_id.is_(None),
                    ConnectionMonitorState.enabled.is_(False),
                ).values(enabled=True)
            )
            await session.commit()
            self.assertEqual(result.rowcount, 1)
            platform = await session.get(ConnectionMonitorState, state_id)
        self.assertTrue(platform.enabled)
