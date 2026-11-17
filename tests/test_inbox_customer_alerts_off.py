"""The «customer alerts are off» notice lives in the inbox and can be dismissed."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from app.services.inbox_dismissals import MODE_FOREVER, _alert_active_map, filter_inbox_context
from app.services.panel_inbox import _customer_alerts_off_page, inbox_alert_count

ALERTS_OFF = {"user_alert_low_enabled": "0"}
ALERTS_ON = {"user_alert_low_enabled": "1"}


def _dismissal(mode: str = MODE_FOREVER) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        alert_key="customer_alerts_off",
        entity_id="",
        mode=mode,
        seen_count=1,
        dismissed_at=datetime.now(timezone.utc),
    )


class CustomerAlertsOffInboxTests(unittest.TestCase):
    def test_owner_sees_notice_while_alerts_are_off(self):
        page = _customer_alerts_off_page({"role": "admin"}, ALERTS_OFF, is_owner=True)
        self.assertEqual(page, "/settings?tab=notifications")
        self.assertEqual(inbox_alert_count({"customer_alerts_off": page}), 1)

    def test_no_notice_when_alerts_are_on_or_settings_unreadable(self):
        self.assertIsNone(_customer_alerts_off_page({"role": "admin"}, ALERTS_ON, is_owner=True))
        self.assertIsNone(_customer_alerts_off_page({"role": "admin"}, {}, is_owner=True))

    def test_shop_staff_without_settings_permission_get_no_notice(self):
        staff = {"role": "reseller_staff", "permissions": []}
        self.assertIsNone(_customer_alerts_off_page(staff, ALERTS_OFF, is_owner=False))

    def test_forever_dismiss_hides_notice_while_alerts_stay_off(self):
        ctx = {"customer_alerts_off": "/settings?tab=notifications"}
        filtered = filter_inbox_context(ctx, [_dismissal()])
        self.assertFalse(filtered["customer_alerts_off"])
        self.assertEqual(filtered["inbox_count"], 0)

    def test_dismissal_is_released_once_alerts_are_switched_on(self):
        self.assertFalse(_alert_active_map({"customer_alerts_off": None})["customer_alerts_off"])
        self.assertTrue(_alert_active_map({"customer_alerts_off": "/x"})["customer_alerts_off"])


if __name__ == "__main__":
    unittest.main()
