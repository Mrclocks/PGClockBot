"""Staff service lists stay inside Telegram's message length limit."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.services.bot_user_admin import ServiceSnapshot, snapshot_telegram_lines
from app.services.staff_cards import TELEGRAM_TEXT_LIMIT, format_services_list, format_services_list_header

LINK = "https://sub.example-panel.com:2096/sub/Y2xrX3NhcwgNMTA3LDE3MzAwMDAwMDA.MTA3.joziaNayocXGLc"


def snapshot(service_id: int) -> ServiceSnapshot:
    service = SimpleNamespace(
        id=service_id, pg_username=f"clk_customer_{service_id}", plan=SimpleNamespace(name="30 گیگ یک ماهه")
    )
    return ServiceSnapshot(
        service=service,
        pg=None,
        status_fa="فعال",
        used_text="2.63 گیگ",
        limit_text="30 گیگ",
        volume_text="2.63 / 30 گیگ",
        remain_gb_text="27.4 گیگ",
        days_left=30,
        expire_text="2026/12/03",
        subscription_url=f"{LINK}{service_id}",
    )


def telegram_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


class ServiceListLimitTests(unittest.TestCase):
    def test_a_long_list_is_cut_before_the_limit_and_counts_the_rest(self) -> None:
        entries = [snapshot_telegram_lines(snapshot(sid)) for sid in range(1, 31)]

        text = format_services_list(format_services_list_header(4), entries)

        self.assertLessEqual(telegram_length(text), TELEGRAM_TEXT_LIMIT)
        shown = sum(1 for entry in entries if entry in text)
        self.assertLess(shown, len(entries))
        self.assertIn(f"… و {len(entries) - shown} سرویس دیگر", text)

    def test_a_short_list_is_shown_whole_without_a_note(self) -> None:
        entries = [snapshot_telegram_lines(snapshot(sid), with_link=False) for sid in range(1, 4)]

        text = format_services_list(format_services_list_header(4), entries)

        for entry in entries:
            self.assertIn(entry, text)
        self.assertNotIn("سرویس دیگر", text)

    def test_fifteen_services_fit_once_links_are_left_to_the_service_screen(self) -> None:
        entries = [snapshot_telegram_lines(snapshot(sid), with_link=False) for sid in range(1, 16)]

        text = format_services_list(format_services_list_header(4), entries)

        self.assertLessEqual(telegram_length(text), TELEGRAM_TEXT_LIMIT)
        self.assertNotIn("سرویس دیگر", text)

    def test_list_entries_leave_out_the_subscription_link(self) -> None:
        snap = snapshot(7)

        self.assertIn(snap.subscription_url, snapshot_telegram_lines(snap))
        self.assertNotIn(snap.subscription_url, snapshot_telegram_lines(snap, with_link=False))
        self.assertIn("clk_customer_7", snapshot_telegram_lines(snap, with_link=False))


if __name__ == "__main__":
    unittest.main()
