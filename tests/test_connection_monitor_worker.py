"""Cycle orchestration uses bounded fake network work and real scoped transactions."""

from __future__ import annotations

import asyncio
import copy
import json
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import delete, event, select, update

from connection_monitor_support import MonitorDatabaseCase, subscription_fixture_key

from app.db.models import ConnectionMonitorSnapshot, ConnectionMonitorState, ConnectionMonitorTarget, Setting, UserService
from app.services.connection_monitor_snapshots import stored_utc
from app.services.connection_monitor_timing import INTERVAL_SETTING_KEY
from app.services.connection_monitor_config import MonitorBinding
from app.services.connection_monitor_subscription import plan_monitor_configs, subscription_entries
from app.services.connection_monitor_panel import MonitorAccount, MonitorCatalog
from app.services.connection_monitor_accounts import MonitorAccountError
from app.services.connection_monitor_claims import claim_monitor_cycle
from app.services.connection_monitor_sync import reconcile_monitor_plans
from app.services.connection_monitor_types import MonitorConfigError, MonitorEndpoint, MonitorConfigPlan, MonitorIssue
from app.services.connection_monitor_worker import PROXY_CONCURRENCY, run_monitor_cycle
from app.services.connection_status_types import ObservationKind, ProbeObservation
from app.services.xray_probe_resources import XrayBinary, XrayProbeError


OVERLAP_WAIT_SECONDS = 5


class MonitorWorkerTests(MonitorDatabaseCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.catalog = json.loads((Path(__file__).parent / "fixtures/connection_monitor_catalog.json").read_text())
        self.open_connections: set[object] = set()
        event.listen(self.engine.sync_engine, "checkout", lambda connection, record, proxy: self.open_connections.add(record))
        event.listen(self.engine.sync_engine, "checkin", lambda connection, record: self.open_connections.discard(record))
        self.binary = XrayBinary(Path("/synthetic/xray"), "0" * 64)
        self.started = 0
        self.closed = 0
        self.active = 0
        self.peak = 0
        # When set, each probe waits here until this many probes overlap, so the
        # concurrency assertion does not depend on how fast the machine is.
        self.overlap_target: int | None = None
        self.overlap_reached = asyncio.Event()

        async def catalog(account: MonitorAccount) -> MonitorCatalog:
            self.assertFalse(self.open_connections, "Panel I/O must not hold DB connections")
            return MonitorCatalog(copy.deepcopy(self.catalog["native_configs"]), checked_at=datetime.now(timezone.utc))

        @asynccontextmanager
        async def core(binary: XrayBinary, endpoints: tuple[MonitorEndpoint, ...]) -> AsyncIterator[SimpleNamespace]:
            self.assertFalse(self.open_connections)
            self.started += 1
            try:
                yield SimpleNamespace(is_alive=True, bindings=tuple(MonitorBinding(endpoint, 18080 + index) for index, endpoint in enumerate(endpoints)))
            finally:
                self.closed += 1

        @asynccontextmanager
        async def client(**kwargs: object) -> AsyncIterator[object]:
            yield object()

        async def sample(client: object, **kwargs: object) -> ProbeObservation:
            self.assertFalse(self.open_connections)
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                if self.overlap_target is not None:
                    if self.active >= self.overlap_target:
                        self.overlap_reached.set()
                    await asyncio.wait_for(self.overlap_reached.wait(), timeout=OVERLAP_WAIT_SECONDS)
                await asyncio.sleep(0.005)
                return ProbeObservation(ObservationKind.SUCCESS, datetime.now(timezone.utc), "gstatic", 100)
            finally:
                self.active -= 1

        for patcher in (
            patch("app.services.connection_monitor_worker.read_monitor_catalog", side_effect=catalog),
            patch("app.services.connection_monitor_worker.run_xray_batch", side_effect=core),
            patch("app.services.connection_monitor_worker.probe_control", AsyncMock(return_value=True)),
            patch("app.services.connection_monitor_worker.probe_client", side_effect=client),
            patch("app.services.connection_monitor_worker.probe_proxy", side_effect=sample),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        async with self.Session() as session:
            session.add(ConnectionMonitorState(enabled=True, pg_user_id=900, pg_username="monitor-only", account_revision="one", subscription_token_enc="synthetic"))
            session.add_all([ConnectionMonitorTarget(subscription_key=subscription_fixture_key(index), test_enabled=True, next_check_at=self.now) for index in range(2)])
            await session.commit()

    async def _next_round(self) -> None:
        """The targets are due again, as they are one interval after a completed round."""
        async with self.Session() as session:
            await session.execute(update(ConnectionMonitorTarget).values(next_check_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
            await session.commit()

    async def test_a_round_with_nothing_due_reads_neither_the_panel_nor_the_core(self) -> None:
        await run_monitor_cycle(self.Session, self.binary)
        with patch("app.services.connection_monitor_worker.read_monitor_catalog",
                   AsyncMock(side_effect=AssertionError("catalog read in an idle round"))):
            result = await run_monitor_cycle(self.Session, self.binary)
        self.assertEqual((result.claimed, result.completed), (True, 0))
        self.assertEqual(self.started, 1)
        async with self.Session() as session:
            self.assertIsNone((await session.scalar(select(ConnectionMonitorState))).cycle_token)

    async def test_a_catalog_one_interval_old_is_read_again_even_with_nothing_due(self) -> None:
        await run_monitor_cycle(self.Session, self.binary)
        async with self.Session() as session:
            await session.execute(update(ConnectionMonitorState).values(
                catalog_synced_at=datetime.now(timezone.utc) - timedelta(seconds=61)))
            await session.commit()
        reads = AsyncMock(side_effect=lambda account: MonitorCatalog(
            copy.deepcopy(self.catalog["native_configs"]), checked_at=datetime.now(timezone.utc)))
        with patch("app.services.connection_monitor_worker.read_monitor_catalog", reads):
            await run_monitor_cycle(self.Session, self.binary)
        self.assertEqual(reads.await_count, 1)
        self.assertEqual(self.started, 1)

    async def test_the_configured_interval_schedules_the_next_check(self) -> None:
        async with self.Session() as session:
            session.add(Setting(key=INTERVAL_SETTING_KEY, value="300"))
            await session.commit()
        before = datetime.now(timezone.utc)
        await run_monitor_cycle(self.Session, self.binary)
        async with self.Session() as session:
            due = [stored_utc(row) for row in (await session.scalars(select(ConnectionMonitorTarget.next_check_at))).all()]
        self.assertTrue(all(before + timedelta(seconds=300) <= at <= datetime.now(timezone.utc) + timedelta(seconds=300) for at in due))

    async def test_one_core_batch_persists_complete_coverage_and_releases_all_leases(self) -> None:
        result = await run_monitor_cycle(self.Session, self.binary)
        self.assertEqual((result.claimed, result.completed, result.reason), (True, 2, None))
        self.assertEqual((self.started, self.closed), (1, 1))
        async with self.Session() as session:
            snapshots = (await session.scalars(select(ConnectionMonitorSnapshot))).all()
            targets = (await session.scalars(select(ConnectionMonitorTarget))).all()
            state = await session.scalar(select(ConnectionMonitorState))
            self.assertEqual(len(snapshots), 2)
            self.assertTrue(all(row.status == "healthy" for row in snapshots))
            self.assertTrue(all(row.claim_token is None for row in targets))
            self.assertIsNone(state.cycle_token)
            self.assertIsNotNone(state.catalog_synced_at)
        repeated = await run_monitor_cycle(self.Session, self.binary)
        self.assertEqual(repeated.completed, 0)
        self.assertEqual(self.started, 1)

    async def test_disabled_feature_needs_no_core_or_network(self) -> None:
        async with self.Session() as session:
            state = await session.scalar(select(ConnectionMonitorState))
            state.enabled = False
            await session.commit()
        with patch("app.services.connection_monitor_worker.read_monitor_catalog", new_callable=AsyncMock) as panel:
            result = await run_monitor_cycle(self.Session, self.binary)
            panel.assert_not_called()
        self.assertFalse(result.claimed)
        self.assertEqual(self.started, 0)

    async def test_legacy_customer_username_stops_the_worker_before_panel_or_core_io(self) -> None:
        async with self.Session() as session:
            session.add(UserService(bot_user_id=5, pg_user_id=None, pg_username="MONITOR-ONLY"))
            await session.commit()
        with patch("app.services.connection_monitor_worker.read_monitor_catalog", new_callable=AsyncMock) as panel:
            result = await run_monitor_cycle(self.Session, self.binary)
            panel.assert_not_called()
        self.assertFalse(result.claimed)
        self.assertEqual(self.started, 0)

    async def test_multi_address_host_respects_four_endpoint_concurrency(self) -> None:
        self.catalog["native_configs"] = [{**self.catalog["native_configs"][0], "remarks": f"config {index}"} for index in range(12)]
        async with self.Session() as session:
            await session.execute(delete(ConnectionMonitorTarget))
            session.add_all(ConnectionMonitorTarget(subscription_key=entry.config_key, test_enabled=True, next_check_at=self.now)
                            for entry in subscription_entries(self.catalog["native_configs"]))
            await session.commit()
        self.overlap_target = PROXY_CONCURRENCY
        result = await run_monitor_cycle(self.Session, self.binary)
        self.assertEqual(result.completed, 12)
        self.assertEqual(self.peak, PROXY_CONCURRENCY)
        self.assertEqual(self.active, 0)

    async def test_removed_host_is_paused_and_unpublished_without_starting_a_route_for_it(self) -> None:
        self.catalog["native_configs"] = self.catalog["native_configs"][:1]
        result = await run_monitor_cycle(self.Session, self.binary)
        self.assertEqual(result.completed, 1)
        async with self.Session() as session:
            removed = await session.scalar(select(ConnectionMonitorTarget).where(ConnectionMonitorTarget.subscription_key == subscription_fixture_key(1)))
            self.assertFalse(removed.test_enabled)
            self.assertFalse(removed.is_public)
            self.assertEqual(removed.generation, "")

    async def test_core_failure_does_not_escape_as_an_exception_group_or_report_a_host_outage(self) -> None:
        @asynccontextmanager
        async def broken(binary: XrayBinary, endpoints: tuple[MonitorEndpoint, ...]) -> AsyncIterator[None]:
            raise XrayProbeError("core_checksum")
            yield

        with patch("app.services.connection_monitor_worker.run_xray_batch", side_effect=broken):
            result = await run_monitor_cycle(self.Session, self.binary)
        self.assertEqual(result.reason, "core_unavailable")
        async with self.Session() as session:
            snapshots = (await session.scalars(select(ConnectionMonitorSnapshot))).all()
            self.assertTrue(all(row.status == "not_ready" and row.failure_streak == 0 for row in snapshots))

    async def test_deadline_cancels_pending_work_without_counting_timeouts_or_leaking_claims(self) -> None:
        requested = asyncio.Event()

        async def slow(client: object, **kwargs: object) -> None:
            requested.set()
            await asyncio.Event().wait()

        # Allow enough time for catalog/reconcile/claim on slow PostgreSQL CI before
        # the hanging probe is interrupted by the cycle deadline.
        with patch("app.services.connection_monitor_worker.probe_proxy", side_effect=slow):
            with patch("app.services.connection_monitor_worker.WORK_SECONDS", 2.0):
                result = await run_monitor_cycle(self.Session, self.binary)
        self.assertTrue(requested.is_set())
        self.assertEqual(result.reason, "cycle_deadline")
        self.assertEqual(self.closed, 1)
        async with self.Session() as session:
            snapshots = (await session.scalars(select(ConnectionMonitorSnapshot))).all()
            targets = (await session.scalars(select(ConnectionMonitorTarget))).all()
            self.assertTrue(all(row.status == "unknown" and row.failure_streak == 0 for row in snapshots))
            self.assertTrue(all(row.claim_token is None for row in targets))

    async def test_two_workers_have_only_one_committed_global_winner(self) -> None:
        entered, released = asyncio.Event(), asyncio.Event()

        async def delayed(account: MonitorAccount) -> MonitorCatalog:
            entered.set()
            await released.wait()
            return MonitorCatalog(self.catalog["native_configs"], checked_at=datetime.now(timezone.utc))

        with patch("app.services.connection_monitor_worker.read_monitor_catalog", side_effect=delayed):
            first = asyncio.create_task(run_monitor_cycle(self.Session, self.binary))
            await entered.wait()
            second = await run_monitor_cycle(self.Session, self.binary)
            self.assertFalse(second.claimed)
            released.set()
            self.assertEqual((await first).completed, 2)
        self.assertEqual(self.started, 1)

    async def test_unexpected_endpoint_failure_records_unknown_and_other_hosts_continue(self) -> None:
        calls = 0

        async def sample(client: object, **kwargs: object) -> ProbeObservation:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("sensitive internal payload")
            return ProbeObservation(ObservationKind.SUCCESS, datetime.now(timezone.utc), "gstatic", 100)

        with patch("app.services.connection_monitor_worker.probe_proxy", side_effect=sample):
            result = await run_monitor_cycle(self.Session, self.binary)
        self.assertEqual(result.completed, 2)
        async with self.Session() as session:
            rows = (await session.scalars(select(ConnectionMonitorSnapshot))).all()
            self.assertEqual({row.status for row in rows}, {"healthy", "unknown"})
            self.assertTrue(all(row.failure_streak == 0 for row in rows))

    async def test_panel_timeout_invalidates_evidence_without_incrementing_route_failure(self) -> None:
        await run_monitor_cycle(self.Session, self.binary)
        await self._next_round()
        with patch("app.services.connection_monitor_worker.read_monitor_catalog", AsyncMock(side_effect=TimeoutError)):
            result = await run_monitor_cycle(self.Session, self.binary)
        self.assertEqual(result.reason, "catalog_unavailable")
        async with self.Session() as session:
            rows = (await session.scalars(select(ConnectionMonitorSnapshot))).all()
            self.assertTrue(all(row.status == "unknown" and row.failure_streak == 0 and row.delay_ms is None for row in rows))

    async def test_inactive_account_error_is_not_ready_not_catalog_outage(self) -> None:
        await run_monitor_cycle(self.Session, self.binary)
        await self._next_round()
        with patch("app.services.connection_monitor_worker.read_monitor_catalog",
                   AsyncMock(side_effect=MonitorAccountError("inactive"))):
            result = await run_monitor_cycle(self.Session, self.binary)
        self.assertEqual(result.reason, "account_unavailable")
        async with self.Session() as session:
            rows = (await session.scalars(select(ConnectionMonitorSnapshot))).all()
            self.assertTrue(all(row.status == "not_ready" and row.reason == "account_unavailable" for row in rows))

    async def test_panel_timeout_account_error_stays_catalog_unavailable(self) -> None:
        await run_monitor_cycle(self.Session, self.binary)
        await self._next_round()
        with patch("app.services.connection_monitor_worker.read_monitor_catalog",
                   AsyncMock(side_effect=MonitorAccountError("panel_timeout"))):
            result = await run_monitor_cycle(self.Session, self.binary)
        self.assertEqual(result.reason, "catalog_unavailable")
        async with self.Session() as session:
            rows = (await session.scalars(select(ConnectionMonitorSnapshot))).all()
            self.assertTrue(all(row.status == "unknown" and row.reason == "catalog_unavailable" for row in rows))

    async def test_monitor_config_error_stores_issue_code_as_reason(self) -> None:
        await run_monitor_cycle(self.Session, self.binary)
        await self._next_round()
        with patch("app.services.connection_monitor_worker.plan_monitor_configs",
                   side_effect=MonitorConfigError(MonitorIssue.UNSUPPORTED)):
            result = await run_monitor_cycle(self.Session, self.binary)
        self.assertEqual(result.reason, "unsupported_route")
        async with self.Session() as session:
            rows = (await session.scalars(select(ConnectionMonitorSnapshot))).all()
            self.assertTrue(all(row.status == "not_ready" and row.reason == "unsupported_route" for row in rows))

    async def test_catalog_planning_leaves_the_event_loop_responsive(self) -> None:
        entered, released = threading.Event(), threading.Event()

        def plan(**kwargs: object) -> tuple[MonitorConfigPlan, ...]:
            entered.set()
            if not released.wait(timeout=2):
                raise AssertionError("Catalog planning blocked the event loop")
            return plan_monitor_configs(**kwargs)

        with patch("app.services.connection_monitor_worker.plan_monitor_configs", side_effect=plan):
            task = asyncio.create_task(run_monitor_cycle(self.Session, self.binary))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                released.set()
                self.assertEqual((await task).completed, 2)
            finally:
                released.set()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)


class MonitorSyncReconcileTests(MonitorDatabaseCase):
    async def test_unsupported_diagnostic_snapshot_keeps_id_across_identical_reconciles(self) -> None:
        config_key = "U" * 43
        plan = MonitorConfigPlan(config_key, issue=MonitorIssue.UNSUPPORTED)
        async with self.Session() as session:
            session.add(ConnectionMonitorState(
                enabled=True, pg_user_id=900, pg_username="monitor-only",
                account_revision="one", subscription_token_enc="synthetic",
            ))
            session.add(ConnectionMonitorTarget(subscription_key=config_key, test_enabled=True, next_check_at=self.now))
            await session.commit()
            cycle = await claim_monitor_cycle(session, now=self.now)
        self.assertIsNotNone(cycle)
        async with self.Session() as session:
            self.assertTrue(await reconcile_monitor_plans(session, cycle, (plan,), now=self.now))
            first = await session.scalar(select(ConnectionMonitorSnapshot).where(
                ConnectionMonitorSnapshot.endpoint_key == "configuration",
            ))
            self.assertIsNotNone(first)
            self.assertEqual(first.reason, "unsupported_route")
            first_id = first.id
            target = await session.scalar(select(ConnectionMonitorTarget))
            self.assertTrue(target.test_enabled)
        async with self.Session() as session:
            self.assertTrue(await reconcile_monitor_plans(session, cycle, (plan,), now=self.now))
            second = await session.scalar(select(ConnectionMonitorSnapshot).where(
                ConnectionMonitorSnapshot.endpoint_key == "configuration",
            ))
            self.assertEqual(second.id, first_id)
            self.assertEqual(second.reason, "unsupported_route")
            target = await session.scalar(select(ConnectionMonitorTarget))
            self.assertTrue(target.test_enabled)
