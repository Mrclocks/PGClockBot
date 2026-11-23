"""Renew-ahead window, reminder stages and the shortfall top-up of automatic renewal."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services import auto_renew_timing as timing
from app.services.bot_setting_values import NUMBER_BOUNDS
from app.services.users import DEFAULT_SETTINGS, SETTING_GROUPS, TAB_SETTING_GROUPS

NOW = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)


def plan(*, time: bool = True, volume: bool = True) -> SimpleNamespace:
    return SimpleNamespace(carry_remaining_time=time, carry_remaining_volume=volume)


class RenewTimingTests(unittest.TestCase):
    def test_defaults_keep_renewal_at_expiry_and_remind_at_24_and_6_hours(self) -> None:
        schedule = timing.renew_timing({})
        self.assertEqual((schedule.ahead_hours, schedule.reminder_hours), (0, (6, 24)))

    def test_out_of_range_and_garbage_values_are_clamped(self) -> None:
        schedule = timing.renew_timing(
            {timing.AHEAD_HOURS_KEY: "999", timing.FIRST_REMINDER_KEY: "-5", timing.LAST_REMINDER_KEY: "abc"}
        )
        self.assertEqual(schedule.ahead_hours, timing.MAX_AHEAD_HOURS)
        self.assertEqual(schedule.reminder_hours, (6,))

    def test_due_ahead_only_inside_the_window_and_without_loss(self) -> None:
        schedule = timing.RenewTiming(ahead_hours=6, reminder_hours=())
        soon, later = NOW + timedelta(hours=5), NOW + timedelta(hours=7)
        self.assertTrue(timing.is_due_ahead(action="renew", choice=plan(), expire=soon, now=NOW, timing=schedule))
        self.assertFalse(timing.is_due_ahead(action="renew", choice=plan(), expire=later, now=NOW, timing=schedule))
        self.assertFalse(timing.is_due_ahead(action="renew", choice=plan(volume=False), expire=soon, now=NOW, timing=schedule))
        self.assertTrue(timing.is_due_ahead(action="duration", choice=object(), expire=soon, now=NOW, timing=schedule))
        self.assertFalse(timing.is_due_ahead(action=None, choice=None, expire=soon, now=NOW, timing=schedule))

    def test_selected_pack_decides_like_exhaustion(self) -> None:
        self.assertEqual(timing.time_action(renew_enabled=True, duration_enabled=True, choices={"renew": 1}), None)
        self.assertEqual(timing.time_action(renew_enabled=True, duration_enabled=False, choices={"renew": 1}), "renew")

    def test_reminder_stage_is_the_closest_stage_reached(self) -> None:
        schedule = timing.RenewTiming(ahead_hours=0, reminder_hours=(6, 24))
        stage = lambda hours: timing.reminder_stage(expire=NOW + timedelta(hours=hours), now=NOW, timing=schedule)
        self.assertEqual((stage(30), stage(20), stage(5)), (None, 24, 6))

    def test_top_up_covers_the_shortfall_in_whole_thousands(self) -> None:
        self.assertEqual(timing.topup_amount(price=120_500, balance=20_000), 101_000)
        self.assertEqual(timing.topup_amount(price=200, balance=100), timing.TOPUP_STEP)
        self.assertEqual(timing.topup_amount(price=5_000, balance=-3_000), 5_000)

    def test_top_up_step_is_the_smallest_amount_the_top_up_accepts(self) -> None:
        from app.services.orders import MIN_TOPUP_AMOUNT

        self.assertEqual(timing.TOPUP_STEP, MIN_TOPUP_AMOUNT)

    def test_settings_exist_on_web_and_bot_with_bounds(self) -> None:
        keys = [field[0] for field in SETTING_GROUPS[timing.GROUP_TITLE]]
        self.assertEqual(keys, list(timing.TIMING_DEFAULTS))
        self.assertIn(timing.GROUP_TITLE, TAB_SETTING_GROUPS["notifications"])
        for key, bounds in timing.TIMING_BOUNDS.items():
            self.assertEqual(NUMBER_BOUNDS[key], bounds)
            self.assertEqual(DEFAULT_SETTINGS[key], timing.TIMING_DEFAULTS[key])


if __name__ == "__main__":
    unittest.main()
