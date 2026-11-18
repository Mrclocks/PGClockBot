"""Snapshot writes require complete coverage and the still-current committed lease."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import timedelta

from sqlalchemy import insert, select, text

from connection_monitor_support import MonitorDatabaseCase

from app.db.models import ConnectionMonitorSnapshot, ConnectionMonitorState, ConnectionMonitorTarget, OrgPrincipal, UserService
from app.services.connection_monitor_claims import MonitorTargetClaim, claim_due_targets, claim_monitor_cycle
from app.services.connection_monitor_snapshots import complete_monitor_target, snapshot_health
from app.services.connection_status_types import ObservationKind, ProbeObservation


class MonitorSnapshotTests(MonitorDatabaseCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        async with self.Session() as session:
            session.add(ConnectionMonitorState(enabled=True, pg_user_id=900, pg_username="monitor-only", account_revision="one", subscription_token_enc="synthetic"))
            session.add(ConnectionMonitorTarget(id=10, pg_host_id=41, subscription_key="A" * 43, test_enabled=True, generation="one", endpoint_count=2, next_check_at=self.now))
            await session.flush()
            session.add_all([ConnectionMonitorSnapshot(target_id=10, endpoint_key=key, generation="one") for key in ("a", "b")])
            await session.commit()
            self.cycle = await claim_monitor_cycle(session, now=self.now)
            self.claim = (await claim_due_targets(session, self.cycle, now=self.now))[0]
        self.observations = {key: ProbeObservation(ObservationKind.SUCCESS, self.now, "gstatic", 100) for key in ("a", "b")}

    async def complete(self, claim: MonitorTargetClaim | None = None, *, later: int = 0) -> bool:
        async with self.Session() as session:
            return await complete_monitor_target(session, claim or self.claim, self.observations, now=self.now + timedelta(seconds=later))

    async def test_repeated_or_concurrent_completion_writes_only_one_observation(self) -> None:
        winners = await asyncio.gather(self.complete(), self.complete())
        self.assertEqual(sum(winners), 1)
        self.assertFalse(await self.complete())
        async with self.Session() as session:
            rows = (await session.scalars(select(ConnectionMonitorSnapshot))).all()
            self.assertTrue(all(row.success_streak == 1 for row in rows))
            self.assertTrue(all(len(snapshot_health(row).samples) == 1 for row in rows))

    async def test_old_token_wrong_shop_wrong_generation_and_expired_cycle_cannot_write(self) -> None:
        invalid = (replace(self.claim, token="wrong"), replace(self.claim, target_id=999),
                   replace(self.claim, generation="old"), replace(self.claim, cycle=replace(self.cycle, token="wrong")))
        for claim in invalid:
            with self.subTest(claim=claim):
                self.assertFalse(await self.complete(claim))
        self.assertFalse(await self.complete(later=90))

    async def test_pause_or_owner_revocation_during_network_prevents_result_write(self) -> None:
        async with self.Session() as session:
            owner = await session.get(OrgPrincipal, 1)
            owner.status = "disabled"
            await session.commit()
        self.assertFalse(await self.complete())
        async with self.Session() as session:
            owner = await session.get(OrgPrincipal, 1)
            owner.status = "active"
            state = await session.scalar(select(ConnectionMonitorState))
            state.enabled = False
            await session.commit()
        self.assertFalse(await self.complete())

    async def test_partial_or_future_observations_do_not_complete_the_target(self) -> None:
        partial = self.observations.pop("b")
        self.assertFalse(await self.complete())
        self.observations["b"] = replace(partial, checked_at=self.now + timedelta(seconds=1))
        self.assertFalse(await self.complete())

    async def test_account_attached_to_a_legacy_customer_during_network_cannot_write_results(self) -> None:
        async with self.Session() as session:
            session.add(UserService(bot_user_id=5, pg_user_id=None, pg_username="MONITOR-ONLY"))
            await session.commit()
        self.assertFalse(await self.complete())

    async def test_malformed_sample_windows_become_unknown_without_exposing_payload(self) -> None:
        async with self.Session() as session:
            row = await session.scalar(select(ConnectionMonitorSnapshot).limit(1))
            for raw in ('{"token":"sensitive"}', '[{"delay_ms": true}]', 'not json', '"' + 'x'*9000 + '"'):
                row.samples_json = raw
                health = snapshot_health(row)
                self.assertEqual(health.status, "unknown")
                self.assertNotIn("sensitive", repr(health))

    async def test_customer_account_exclusion_can_use_both_identity_indexes(self) -> None:
        async with self.Session() as session:
            await session.execute(insert(UserService), [
                {"bot_user_id": 4, "pg_user_id": index + 1000, "pg_username": f"historical-{index}"}
                for index in range(5000)
            ])
            await session.commit()
            await session.execute(text("ANALYZE user_services"))
            query = "SELECT id FROM user_services WHERE pg_user_id = :user_id OR lower(pg_username) = :username"
            result = await session.execute(text("EXPLAIN (FORMAT JSON) " + query), {"user_id": 6000, "username": "monitor-only"})
            plan = json.dumps([tuple(row) for row in result.all()])
            self.assertIn("ix_user_services_pg_user_id", plan)
            self.assertIn("ix_user_services_pg_username_lower", plan)
