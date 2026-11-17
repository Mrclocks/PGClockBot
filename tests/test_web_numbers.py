"""The web panel shows figures with Persian digits, «٬» and «٫»; Telegram texts keep theirs (DESIGN_SYSTEM.md §2)."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.api.app import templates
from app.services.formatting import format_money, format_number
from app.services.web_numbers import WEB_NUMBER_FILTERS, persian_display


def show(expr: str, **ctx: object) -> str:
    return templates.env.from_string("{{ " + expr + " }}").render(**ctx)


class WebNumberFilterTests(unittest.TestCase):
    def test_counts_and_amounts_read_in_persian(self) -> None:
        self.assertEqual(show("1234567 | num"), "۱٬۲۳۴٬۵۶۷")
        self.assertEqual(show("390000 | money"), "۳۹۰٬۰۰۰")
        self.assertEqual(show("1.5 | num"), "۱٫۵۰")

    def test_sizes_and_dates_read_in_persian(self) -> None:
        self.assertNotRegex(show("12.5 | gb"), "[0-9]")
        self.assertIn("۱۲٫۵", show("12.5 | gb"))
        self.assertNotRegex(show("1610612736 | bytes"), "[0-9]")

    def test_missing_values_keep_their_dash(self) -> None:
        self.assertEqual(show("none | num"), "—")
        self.assertEqual(show("missing | money"), "—")

    def test_every_number_filter_of_the_panel_is_the_persian_one(self) -> None:
        for name, display in WEB_NUMBER_FILTERS.items():
            self.assertIs(templates.env.filters[name], display, name)

    def test_shared_formatters_for_telegram_keep_ascii_digits(self) -> None:
        self.assertEqual(format_number(1234), "1٬234")
        self.assertEqual(format_money(390000), "390,000")

    def test_only_a_point_between_digits_becomes_the_decimal_mark(self) -> None:
        self.assertEqual(persian_display(lambda: "v1. 2.5")(), "v۱. ۲٫۵")


if __name__ == "__main__":
    unittest.main()


class TemplateFigureTests(unittest.TestCase):
    """Figures written into the page text, not only the filtered ones, read in Persian."""

    def test_the_sales_card_names_its_ranges_in_persian(self) -> None:
        html = templates.env.get_template("_home_sales_chart.html").render(sales_trend={}, periods={})
        self.assertIn(">۷ روز<", html)
        self.assertIn(">۳۰ روز<", html)
        self.assertNotRegex(html, r"[0-9] روز")

    def test_a_raw_figure_before_a_unit_is_shown_in_persian(self) -> None:
        self.assertEqual(show("(p.duration_days or '∞') | fa", p={"duration_days": 30}), "۳۰")
        self.assertEqual(show("(p.duration_days or '∞') | fa", p={"duration_days": 0}), "∞")
