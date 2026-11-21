"""The check interval is the owner's choice, and the status rules mean the same at every interval."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from connection_monitor_support import MonitorDatabaseCase, monitor_access

from app.db.models import Setting
from app.services.connection_monitor_timing import (
    DEFAULT_TIMING,
    INTERVAL_CHOICES,
    INTERVAL_SETTING_KEY,
    monitor_timing,
    parse_interval,
    set_monitor_interval,
    timing_for,
)
from app.services.connection_status_policy import apply_observation, is_fresh
from app.services.connection_status_types import (
    FRESHNESS,
    ConnectionStatus,
    EndpointIdentity,
    ObservationKind,
    ProbeObservation,
)

START = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
IDENTITY = EndpointIdentity("endpoint", "generation")


def failures(*, spacing: timedelta, freshness: timedelta) -> ConnectionStatus:
    state = None
    for round_no in range(3):
        observation = ProbeObservation(ObservationKind.FAILURE, START + spacing * round_no, "gstatic", reason="timeout")
        state = apply_observation(IDENTITY, observation, previous=state, freshness=freshness)
    return state.status


class TimingTests(unittest.TestCase):
    def test_default_keeps_the_original_contract(self) -> None:
        self.assertEqual(DEFAULT_TIMING.interval, timedelta(seconds=60))
        self.assertEqual(DEFAULT_TIMING.freshness, FRESHNESS)

    def test_a_result_stays_fresh_for_three_rounds_at_any_interval(self) -> None:
        for seconds in INTERVAL_CHOICES:
            with self.subTest(seconds=seconds):
                timing = timing_for(seconds)
                self.assertGreaterEqual(timing.freshness, timing.interval * 3)
                self.assertTrue(is_fresh(START, now=START + timing.interval * 2, freshness=timing.freshness))

    def test_three_failed_rounds_are_down_at_a_five_minute_interval(self) -> None:
        timing = timing_for(300)
        self.assertEqual(failures(spacing=timing.interval, freshness=timing.freshness), ConnectionStatus.DOWN)

    def test_without_the_wider_window_spaced_rounds_would_never_reach_down(self) -> None:
        self.assertEqual(failures(spacing=timedelta(seconds=300), freshness=FRESHNESS), ConnectionStatus.DEGRADED)

    def test_unknown_or_hostile_values_read_as_the_default(self) -> None:
        for raw in (None, "", "abc", "59", "-60", "1e9", "300.5", True):
            with self.subTest(raw=raw):
                self.assertEqual(parse_interval(raw), 60)
        self.assertEqual(parse_interval(" 600 "), 600)


class IntervalSettingTests(MonitorDatabaseCase):
    async def test_owner_saves_a_choice_and_the_timing_follows_it(self) -> None:
        async with self.Session() as session:
            await set_monitor_interval(session, self.access, seconds=600)
            await set_monitor_interval(session, self.access, seconds=300)
        async with self.Session() as session:
            self.assertEqual((await monitor_timing(session)).interval, timedelta(seconds=300))
            rows = (await session.scalars(select(Setting).where(Setting.key == INTERVAL_SETTING_KEY))).all()
            self.assertEqual(len(rows), 1)

    async def test_values_outside_the_choices_are_refused(self) -> None:
        for bad in (0, 30, 61, 86400, -60, True):
            with self.subTest(bad=bad):
                async with self.Session() as session:
                    with self.assertRaises(ValueError):
                        await set_monitor_interval(session, self.access, seconds=bad)
        async with self.Session() as session:
            self.assertEqual(await monitor_timing(session), DEFAULT_TIMING)

    async def test_shop_staff_and_customers_cannot_change_the_interval(self) -> None:
        others = (
            monitor_access(org_principal_id=2, org_depth=1, org_parent_id=1, role="reseller", bot_user_id=2),
            monitor_access(org_principal_id=None, org_depth=None, role="user", bot_user_id=4),
        )
        for access in others:
            async with self.Session() as session:
                with self.assertRaises(ValueError):
                    await set_monitor_interval(session, access, seconds=900)
        async with self.Session() as session:
            self.assertEqual(await monitor_timing(session), DEFAULT_TIMING)


if __name__ == "__main__":
    unittest.main()
