"""Low-time alerts by days as well as percent, alert texts with the expiry date, and the dashboard reminder."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from aiogram.exceptions import TelegramBadRequest

from app.services.alert_rearm import _rearm_one
from app.services.usage_alert_messages import (
    alerts_off_settings_page,
    clamp_alert_setting,
    send_alert_with_default_fallback,
    should_rearm_time,
    time_alert_due,
    time_alert_text,
    traffic_alert_text,
)

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
GB = 1024**3


class DueTests(unittest.TestCase):
    def test_days_threshold_catches_a_long_service_the_percent_misses(self) -> None:
        # Percent says «plenty left» (60%), but only 2.5 days remain: the days threshold catches it.
        self.assertTrue(time_alert_due(60.0, 2.5, time_pct=20, days=3))
        self.assertFalse(time_alert_due(60.0, 2.5, time_pct=20, days=0))

    def test_percent_still_works_alone(self) -> None:
        self.assertTrue(time_alert_due(10.0, 30.0, time_pct=20, days=3))
        self.assertFalse(time_alert_due(None, None, time_pct=20, days=3))

    def test_rearm_waits_until_both_thresholds_are_clear(self) -> None:
        self.assertFalse(should_rearm_time(90.0, 2.0, time_pct=20, days=3, margin_pct=5))
        self.assertFalse(should_rearm_time(90.0, 3.5, time_pct=20, days=3, margin_pct=5))
        self.assertTrue(should_rearm_time(90.0, 30.0, time_pct=20, days=3, margin_pct=5))
        self.assertTrue(should_rearm_time(90.0, 2.0, time_pct=20, days=0, margin_pct=5))

    def test_a_days_alert_is_not_rearmed_by_the_sweep_while_days_are_short(self) -> None:
        service = SimpleNamespace(notified_expire=True, notified_traffic=False, created_at=NOW - timedelta(days=363))
        info = {"expire": (NOW + timedelta(days=2)).timestamp()}
        self.assertFalse(_rearm_one(service, info, now=NOW, plan_days=365, time_pct=20, traffic_pct=20, days=3))
        self.assertTrue(service.notified_expire)

    def test_settings_are_clamped(self) -> None:
        self.assertEqual(clamp_alert_setting("user_alert_low_days", "99"), "60")
        self.assertEqual(clamp_alert_setting("user_alert_low_days", "abc"), "0")
        self.assertEqual(clamp_alert_setting("user_alert_low_time_pct", "0"), "1")
        self.assertEqual(clamp_alert_setting("user_alert_low_traffic_pct", ""), "20")


class TextTests(unittest.TestCase):
    def test_default_time_text_has_days_and_jalali_date(self) -> None:
        text = time_alert_text({}, service_name="ali", expire=NOW + timedelta(days=2, hours=3), now=NOW, renew_button=True)
        self.assertIn("۳ روز", text)
        self.assertIn("۱۴۰۵/۸/۲۸", text)
        self.assertTrue(text.endswith("دکمهٔ زیر را بزنید."))

    def test_shop_text_is_used_and_values_are_escaped(self) -> None:
        ui = {"user_alert_time_text": "{service_name} — {days_left} — {unknown}"}
        text = time_alert_text(ui, service_name="<b>x</b>", expire=NOW + timedelta(days=1), now=NOW, renew_button=False)
        self.assertTrue(text.startswith("&lt;b&gt;x&lt;/b&gt; — ۱ — {unknown}"))
        self.assertIn("سرویس‌های من", text)

    def test_traffic_text(self) -> None:
        text = traffic_alert_text({}, service_name="ali", used=45 * GB, limit=50 * GB)
        self.assertIn("ali", text)
        self.assertIn("از", text)


class ReminderTests(unittest.TestCase):
    def test_reminder_only_while_off_and_only_for_staff_who_can_switch_it(self) -> None:
        off = {"user_alert_low_enabled": "0"}
        self.assertEqual(alerts_off_settings_page(off, is_owner=True, can_edit_shop=False), "/settings?tab=notifications")
        self.assertEqual(alerts_off_settings_page(off, is_owner=False, can_edit_shop=True), "/shop-settings?tab=notifications")
        self.assertIsNone(alerts_off_settings_page(off, is_owner=False, can_edit_shop=False))
        self.assertIsNone(alerts_off_settings_page({"user_alert_low_enabled": "1"}, is_owner=True, can_edit_shop=True))
        self.assertIsNone(alerts_off_settings_page({}, is_owner=True, can_edit_shop=True))


class CustomTextFallbackTests(unittest.IsolatedAsyncioTestCase):
    def _build(self, ui: dict) -> str:
        return traffic_alert_text(ui, service_name="ali", used=45 * GB, limit=50 * GB)

    def _refused(self) -> TelegramBadRequest:
        return TelegramBadRequest(method=MagicMock(), message="can't parse entities")

    async def test_a_custom_text_telegram_refuses_falls_back_to_the_default_text(self) -> None:
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=[self._refused(), "sent"])
        ui = {"user_alert_traffic_text": "<b>{service_name}"}
        result = await send_alert_with_default_fallback(bot, 1, ui, self._build, note="", markup=None)
        self.assertEqual(result, "sent")
        first, second = (call.args[1] for call in bot.send_message.await_args_list)
        self.assertIn("<b>ali", first)
        self.assertEqual(second, self._build({}))

    async def test_a_refusal_without_a_custom_text_is_raised(self) -> None:
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=self._refused())
        with self.assertRaises(TelegramBadRequest):
            await send_alert_with_default_fallback(bot, 1, {}, self._build, note="", markup=None)
        self.assertEqual(bot.send_message.await_count, 1)


if __name__ == "__main__":
    unittest.main()
