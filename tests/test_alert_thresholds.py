"""Low-volume and low-time alert thresholds: one number plus the unit it is measured in."""
import unittest

from app.services.alert_thresholds import (
    BYTES_PER_GB,
    AlertThreshold,
    describe_threshold,
    normalize_threshold_form,
    parse_threshold,
    should_rearm_time,
    should_rearm_traffic,
    time_is_low,
    time_threshold,
    traffic_is_low,
    traffic_threshold,
)
from app.services.panel_user_note import build_panel_user_note
from app.services.users import keys_for_tab

DAY = 86400
GB = BYTES_PER_GB


class ThresholdParsingTests(unittest.TestCase):
    def test_legacy_settings_without_a_unit_stay_percent(self):
        self.assertEqual(traffic_threshold({"user_alert_low_traffic_pct": "30"}), AlertThreshold(30, "pct"))

    def test_unit_not_offered_for_that_alert_falls_back_to_percent(self):
        self.assertEqual(parse_threshold("5", "day", ("pct", "gb")).unit, "pct")
        self.assertEqual(parse_threshold("5", "gb", ("pct", "day")).unit, "pct")

    def test_value_is_clamped_to_the_unit_range_and_hostile_text_is_ignored(self):
        self.assertEqual(parse_threshold("500", "pct", ("pct", "gb")).value, 99)
        self.assertEqual(parse_threshold("-3", "gb", ("pct", "gb")).value, 1)
        self.assertEqual(parse_threshold("<script>", "gb", ("pct", "gb")).value, 20)
        self.assertEqual(parse_threshold("nan", "gb", ("pct", "gb")).value, 20)
        self.assertEqual(parse_threshold("inf", "day", ("pct", "day")).value, 20)

    def test_form_payload_is_normalized_in_place(self):
        payload = {"user_alert_low_traffic_pct": "0", "user_alert_low_traffic_unit": "GB",
                   "user_alert_low_time_pct": "9999999", "user_alert_low_time_unit": "years"}
        normalize_threshold_form(payload)
        self.assertEqual(payload["user_alert_low_traffic_pct"], "1")
        self.assertEqual(payload["user_alert_low_traffic_unit"], "gb")
        self.assertEqual(payload["user_alert_low_time_unit"], "pct")
        self.assertEqual(payload["user_alert_low_time_pct"], "99")

    def test_other_tabs_are_left_alone(self):
        payload = {"shop_title": "x"}
        normalize_threshold_form(payload)
        self.assertEqual(payload, {"shop_title": "x"})

    def test_unit_keys_belong_to_the_notifications_tab(self):
        keys = keys_for_tab("notifications")
        self.assertIn("user_alert_low_traffic_unit", keys)
        self.assertIn("user_alert_low_time_unit", keys)

    def test_customer_text_names_the_unit(self):
        self.assertIn("٪", describe_threshold(AlertThreshold(20, "pct")))
        self.assertIn("گیگ", describe_threshold(AlertThreshold(5, "gb")))
        self.assertIn("روز", describe_threshold(AlertThreshold(3, "day")))


class TriggerTests(unittest.TestCase):
    def test_gigabyte_threshold_ignores_the_percentage(self):
        limit = 1000 * GB
        threshold = AlertThreshold(5, "gb")
        self.assertFalse(traffic_is_low(limit - 6 * GB, limit, threshold))  # 0.6 % left but 6 GB
        self.assertTrue(traffic_is_low(limit - 4 * GB, limit, threshold))

    def test_percent_threshold_ignores_the_gigabytes(self):
        limit = 10 * GB
        self.assertTrue(traffic_is_low(9 * GB, limit, AlertThreshold(20, "pct")))  # 1 GB = 10 % left
        self.assertFalse(traffic_is_low(5 * GB, limit, AlertThreshold(20, "pct")))

    def test_unlimited_volume_is_never_low(self):
        self.assertFalse(traffic_is_low(5 * GB, 0, AlertThreshold(5, "gb")))

    def test_day_threshold_ignores_the_period_length(self):
        threshold = AlertThreshold(3, "day")
        self.assertTrue(time_is_low(2 * DAY, None, threshold))
        self.assertFalse(time_is_low(4 * DAY, 5 * DAY, threshold))  # 80 % left, still 4 days

    def test_percent_time_needs_a_known_period_and_an_unfinished_service(self):
        threshold = AlertThreshold(20, "pct")
        self.assertTrue(time_is_low(5 * DAY, 30 * DAY, threshold))
        self.assertFalse(time_is_low(5 * DAY, None, threshold))
        self.assertFalse(time_is_low(-1, 30 * DAY, threshold))

    def test_rearm_needs_clear_headroom_above_the_threshold(self):
        gb = AlertThreshold(4, "gb")
        self.assertFalse(should_rearm_traffic(100 * GB - 4 * GB, 100 * GB, gb))
        self.assertFalse(should_rearm_traffic(100 * GB - 5 * GB, 100 * GB, gb))
        self.assertTrue(should_rearm_traffic(100 * GB - 20 * GB, 100 * GB, gb))
        day = AlertThreshold(4, "day")
        self.assertFalse(should_rearm_time(4 * DAY, None, day))
        self.assertTrue(should_rearm_time(10 * DAY, None, day))
        pct = AlertThreshold(20, "pct")
        self.assertFalse(should_rearm_time(24 * DAY / 10, 10 * DAY, pct))
        self.assertTrue(should_rearm_time(5 * DAY, 10 * DAY, pct))

    def test_time_threshold_reads_day_unit(self):
        self.assertEqual(time_threshold({"user_alert_low_time_pct": "3", "user_alert_low_time_unit": "day"}),
                         AlertThreshold(3, "day"))


class PanelUserNoteTests(unittest.TestCase):
    def test_note_has_order_telegram_id_and_username(self):
        self.assertEqual(build_panel_user_note(order_id=12, telegram_id=555, telegram_username="ali_x"),
                         "PGClockBot order #12 | id: 555 | @ali_x")

    def test_bulk_order_keeps_the_position(self):
        note = build_panel_user_note(order_id=12, telegram_id=555, telegram_username=None, index=2, quantity=3)
        self.assertEqual(note, "PGClockBot order #12 (2/3) | id: 555")

    def test_hostile_username_is_cleaned(self):
        note = build_panel_user_note(order_id=1, telegram_id=None, telegram_username="a b|<b>@x\n" + "z" * 80)
        self.assertNotIn("<", note)
        self.assertNotIn("\n", note)
        self.assertLessEqual(len(note.split(" | ")[-1]), 33)


if __name__ == "__main__":
    unittest.main()
