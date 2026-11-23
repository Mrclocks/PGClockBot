"""Volume column: unit on visual left (RTL) + no row-separator overlap."""

from __future__ import annotations

import unittest
from pathlib import Path

from app.services.formatting import format_bytes, format_bytes_ratio

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "app/web/static/panel.css").read_text(encoding="utf-8")
GB = 1024**3


class VolumeFormatterContract(unittest.TestCase):
    def test_logical_order_number_then_unit(self):
        """String order stays number→unit (Telegram/bot); CSS places unit left."""
        self.assertEqual(format_bytes(10 * GB), "10 گیگ")
        self.assertEqual(format_bytes_ratio(int(1.12 * GB), 10 * GB), "1.12 / 10 گیگ")
        self.assertEqual(format_bytes_ratio(int(1.12 * GB), 0), "1.12 / ∞ گیگ")


class VolumeCssRtlUnitLeft(unittest.TestCase):
    def test_num_ratio_is_rtl_not_ltr(self):
        block = CSS.split(".num-ratio,", 1)[1].split("span.num-ratio", 1)[0]
        self.assertIn("direction: rtl;", block)
        self.assertNotIn("direction: ltr;", block)

    def test_td_num_ratio_stays_table_cell(self):
        """Regression: display:inline-block on <td.num-ratio> cut through row borders."""
        self.assertIn("td.num-ratio,", CSS)
        self.assertIn("display: table-cell;", CSS[CSS.find("td.num-ratio") : CSS.find("td.num-ratio") + 200])
        bare = CSS.split(".num-ratio,\n.byte-size {", 1)[1].split("}", 1)[0]
        self.assertNotIn("display: inline-block", bare)

    def test_reseller_quota_rtl(self):
        cell = CSS.split(".reseller-quota-cell {", 1)[1].split("}", 1)[0]
        self.assertIn("direction: rtl;", cell)
        line = CSS.split(".reseller-quota-line {", 1)[1].split("}", 1)[0]
        self.assertIn("direction: rtl;", line)
        self.assertIn("display: block;", line)


if __name__ == "__main__":
    unittest.main()
