"""Low-time alert re-arming by days, alert texts with the expiry date, and the dashboard reminder."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from aiogram.exceptions import TelegramBadRequest

from app.services.alert_rearm import _rearm_one
from app.services.alert_thresholds import AlertThreshold, time_threshold
from app.services.users import apply_legacy_alert_days
from app.services.usage_alert_messages import (
    alerts_off_settings_page,
    send_alert_with_default_fallback,
    time_alert_text,
    traffic_alert_text,
)

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
GB = 1024**3


class DueTests(unittest.TestCase):
    def test_a_days_alert_is_not_rearmed_by_the_sweep_while_days_are_short(self) -> None:
        service = SimpleNamespace(notified_expire=True, notified_traffic=False, created_at=NOW - timedelta(days=363))
        info = {"expire": (NOW + timedelta(days=2)).timestamp()}
        self.assertFalse(
            _rearm_one(service, info, now=NOW, plan_days=365, time_limit=AlertThreshold(3, "day"), traffic_limit=AlertThreshold(20))
        )
        self.assertTrue(service.notified_expire)

    def test_a_days_alert_is_rearmed_once_the_days_are_clearly_above_the_threshold(self) -> None:
        service = SimpleNamespace(notified_expire=True, notified_traffic=False, created_at=NOW - timedelta(days=363))
        info = {"expire": (NOW + timedelta(days=30)).timestamp()}
        self.assertTrue(
            _rearm_one(service, info, now=NOW, plan_days=365, time_limit=AlertThreshold(3, "day"), traffic_limit=AlertThreshold(20))
        )
        self.assertFalse(service.notified_expire)

    def test_a_days_value_saved_before_the_unit_choice_becomes_the_time_threshold(self) -> None:
        data = {"user_alert_low_time_unit": "", "user_alert_low_days": "3", "user_alert_low_time_pct": "20"}
        apply_legacy_alert_days(data)
        self.assertEqual(time_threshold(data), AlertThreshold(3, "day"))

    def test_a_saved_unit_wins_over_the_old_days_value(self) -> None:
        data = {"user_alert_low_time_unit": "pct", "user_alert_low_days": "3", "user_alert_low_time_pct": "20"}
        apply_legacy_alert_days(data)
        self.assertEqual(time_threshold(data), AlertThreshold(20, "pct"))

    def test_no_old_days_value_keeps_percent(self) -> None:
        data = {"user_alert_low_time_unit": "", "user_alert_low_days": "0", "user_alert_low_time_pct": "20"}
        apply_legacy_alert_days(data)
        self.assertEqual(time_threshold(data), AlertThreshold(20, "pct"))


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
