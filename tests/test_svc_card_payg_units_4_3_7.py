"""v4.3.7 — svc card redesign, reseller role UI, Persian volume units."""

from __future__ import annotations

import unittest

from app.services.formatting import format_bytes_ratio
GB = 1024**3


class VolumeUnitFaTests(unittest.TestCase):
    def test_ratio_unit_once(self):
        text = format_bytes_ratio(10 * GB, 100 * GB)
        self.assertEqual(text, "10 / 100 گیگ")
        self.assertEqual(text.count("گیگ"), 1)

    def test_ratio_az_joiner(self):
        text = format_bytes_ratio(10 * GB, 100 * GB, joiner=" از ")
        self.assertEqual(text, "10 از 100 گیگ")
        self.assertEqual(text.count("گیگ"), 1)


if __name__ == "__main__":
    unittest.main()
