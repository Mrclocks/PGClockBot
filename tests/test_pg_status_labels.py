"""PasarGuard user, admin and node statuses are shown in Persian; a status the bot does not know stays readable."""

from __future__ import annotations

import unittest


from app.services.formatting import node_status_fa, pg_user_status_fa
from app.services.node_traffic import build_nodes_overview


class PgStatusLabelTests(unittest.TestCase):
    def test_known_user_statuses_read_in_persian(self) -> None:
        expected = {
            "active": "فعال",
            "limited": "محدود",
            "expired": "منقضی",
            "disabled": "غیرفعال",
            "on_hold": "در انتظار اتصال",
        }
        for status, label in expected.items():
            self.assertEqual(pg_user_status_fa(status), label)
        self.assertEqual(pg_user_status_fa(" Active "), "فعال")

    def test_an_unknown_or_missing_user_status_is_not_invented(self) -> None:
        self.assertEqual(pg_user_status_fa("paused_by_panel"), "paused_by_panel")
        self.assertEqual(pg_user_status_fa(None), "—")

    def test_a_disconnected_node_reads_in_persian(self) -> None:
        self.assertEqual(node_status_fa("disconnected"), "قطع")
        self.assertEqual(node_status_fa("Connected"), "متصل")

    def test_the_dashboard_node_card_carries_the_persian_status(self) -> None:
        overview = build_nodes_overview([{"id": 1, "name": "de-1", "status": "connected"}], None)
        card = overview["nodes"][0]
        self.assertEqual(card["status"], "متصل")
        self.assertEqual(card["status_kind"], "ok")


if __name__ == "__main__":
    unittest.main()
