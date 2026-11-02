"""The Mini App service screen shows how much traffic is left."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.api.miniapp_pages import _serialize_service, _unavailable_service_row
from app.services.formatting import UNLIMITED_TRAFFIC_LABEL, format_bytes_left

GB = 1024**3


class TrafficLeftTests(unittest.TestCase):
    def test_left_is_limit_minus_used_and_never_negative(self) -> None:
        self.assertEqual(format_bytes_left(13.6 * GB, 20 * GB), "6.4 گیگ")
        self.assertEqual(format_bytes_left(25 * GB, 20 * GB), "0 بایت")
        self.assertEqual(format_bytes_left(None, 20 * GB), "20 گیگ")

    def test_no_limit_reads_as_unlimited(self) -> None:
        for limit in (None, 0, -1):
            with self.subTest(limit=limit):
                self.assertEqual(format_bytes_left(5 * GB, limit), UNLIMITED_TRAFFIC_LABEL)

    def test_bad_values_read_as_unknown(self) -> None:
        self.assertEqual(format_bytes_left("x", 20 * GB), "—")

    def test_service_payload_carries_traffic_left_only_when_usage_is_known(self) -> None:
        svc = SimpleNamespace(id=1, pg_username="s", subscription_url="", plan_id=None,
                              is_cancelled=False, cancellation_pending=False)
        row = _serialize_service(svc, {"used_traffic": 15 * GB, "data_limit": 20 * GB, "status": "active"})
        self.assertEqual(row["traffic_left"], "5 گیگ")
        self.assertEqual(_serialize_service(svc, {"status": "active"})["traffic_left"], "—")
        self.assertEqual(_unavailable_service_row(svc)["traffic_left"], "—")


if __name__ == "__main__":
    unittest.main()
