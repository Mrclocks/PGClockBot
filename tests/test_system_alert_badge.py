"""Alerts about the panel itself carry a «سیستم» badge in the alerts settings tab."""

from __future__ import annotations

import os
import re
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.api.app import templates
from app.services.notifications import PLATFORM_NOTIFY_PREFS, SYSTEM_NOTIFY_KEYS, shop_notify_catalog

BADGE = re.compile(r'name="s_(notify_[a-z_]+)"')


def badged_keys(html: str) -> set[str]:
    """Keys whose switch row shows the system badge."""
    rows = html.split('<label class="notify-row')[1:]
    return {BADGE.search(row).group(1) for row in rows if "notify-system-badge" in row}


def render(name: str, items: list[tuple[str, str, str, str]]) -> str:
    return templates.env.get_template(name).render(
        notify_items=items, notify_prefs={}, tab_groups=[], groups={}, values={}, csrf_token="t",
    )


class SystemAlertBadgeTests(unittest.TestCase):
    def test_owner_tab_marks_only_panel_alerts(self) -> None:
        html = render("_settings_notifications.html", PLATFORM_NOTIFY_PREFS)
        self.assertEqual(badged_keys(html), {"notify_panel_update", "notify_panel_delivery_failures"})

    def test_shop_tab_marks_its_panel_alert(self) -> None:
        items = shop_notify_catalog(["orders", "payments", "tickets", "dashboard"])
        html = render("_shop_settings_notifications.html", items)
        self.assertEqual(badged_keys(html), {k for k, *_ in items} & SYSTEM_NOTIFY_KEYS)
        self.assertIn("notify_panel_delivery_failures", badged_keys(html))

    def test_system_keys_are_real_alerts(self) -> None:
        self.assertLessEqual(SYSTEM_NOTIFY_KEYS, {k for k, *_ in PLATFORM_NOTIFY_PREFS})


if __name__ == "__main__":
    unittest.main()
