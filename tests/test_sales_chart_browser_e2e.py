"""Dashboard sales chart in a real browser: axis labels, hover dot, guide and tooltip line up with the data.

The chart card is rendered from the shipped template and drawn by Chromium with the shipped
panel.css and sales-chart.js. Skipped when Playwright is not installed.
"""

from __future__ import annotations

import json
import re
import unittest


from app.api.app import templates
from app.services.dashboard_trend import empty_trend
from tests.test_ui_polish_browser_e2e import _launch, _Pages

DESKTOP = {"width": 900, "height": 700}
REVENUE_7 = [3_200_000, 4_100_000, 2_600_000, 5_400_000, 3_900_000, 6_100_000, 4_820_000]
PERSIAN_DIGITS = re.compile("[۰-۹]")


def _trend(days: int) -> dict:
    trend = empty_trend(days)
    for index, day in enumerate(trend["days"]):
        day["revenue"] = REVENUE_7[index % 7] + index * 1000
        day["orders"] = index + 1
    trend.update(total=sum(d["revenue"] for d in trend["days"]), previous_total=1, change_pct=5, has_data=True)
    return trend


def _chart_page() -> str:
    card = templates.get_template("_home_sales_chart.html").render(trend=_trend(7))
    return (
        '<!doctype html><html lang="fa" dir="rtl" data-theme="dark"><head><meta charset="utf-8">'
        '<link rel="stylesheet" href="/static/panel.css"></head>'
        f'<body><div style="width:760px;padding:24px">{card}</div>'
        '<script src="/static/sales-chart.js"></script></body></html>'
    )


class SalesChartBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise unittest.SkipTest(f"playwright not installed: {exc}") from exc

    def _with_chart(self, check) -> None:
        from playwright.sync_api import sync_playwright

        server = _Pages({"/chart": _chart_page()})
        server.start()
        try:
            with sync_playwright() as p:
                browser = _launch(p)
                page = browser.new_page(viewport=DESKTOP)
                body = json.dumps(_trend(30))
                page.route("**/home/sales-trend*", lambda route: route.fulfill(content_type="application/json", body=body))
                page.goto(f"http://127.0.0.1:{server.port}/chart")
                page.wait_for_selector(".sc-dot")
                check(page)
                browser.close()
        finally:
            server.stop()

    @staticmethod
    def _ticks(page) -> list[tuple[str, float]]:
        """Axis labels with their centre, measured from the plot's left edge."""
        return page.evaluate(
            """() => {
              const plot = document.querySelector('[data-plot]').getBoundingClientRect();
              return Array.from(document.querySelectorAll('.sc-xtick')).map(t => {
                const r = t.getBoundingClientRect();
                return [t.textContent, r.left + r.width / 2 - plot.left];
              });
            }"""
        )

    def test_axis_labels_run_from_the_oldest_day_on_the_right_to_today_on_the_left(self) -> None:
        expected = [day["weekday"] for day in _trend(7)["days"]][:-1] + ["امروز"]

        def check(page) -> None:
            right_to_left = [text for text, _ in sorted(self._ticks(page), key=lambda tick: -tick[1])]
            self.assertEqual(right_to_left, expected)

        self._with_chart(check)

    def test_hovering_marks_the_day_under_the_pointer(self) -> None:
        days = _trend(7)["days"]

        def check(page) -> None:
            box = page.locator("[data-plot]").bounding_box()
            step = box["width"] / (len(days) - 1)
            for index, day in enumerate(days):
                # Oldest day at the right edge, today at the left edge.
                pointer_x = box["width"] - index * step
                page.mouse.move(box["x"] + min(max(pointer_x, 1), box["width"] - 1), box["y"] + box["height"] / 2)
                spot = page.evaluate(
                    """() => {
                      const plot = document.querySelector('[data-plot]').getBoundingClientRect();
                      const centre = el => { const r = el.getBoundingClientRect(); return r.left + r.width / 2 - plot.left; };
                      const tip = document.querySelector('.sc-tip');
                      return { dot: centre(document.querySelector('.sc-dot')),
                               guide: document.querySelector('.sc-guide').getBoundingClientRect().left - plot.left,
                               tip: tip.textContent };
                    }"""
                )
                with self.subTest(day=day["weekday"]):
                    self.assertAlmostEqual(spot["dot"], pointer_x, delta=2)
                    self.assertAlmostEqual(spot["guide"], pointer_x, delta=2)
                    self.assertIn(day["weekday"], spot["tip"])

        self._with_chart(check)

    def test_month_view_labels_use_latin_digits_in_date_order(self) -> None:
        def check(page) -> None:
            page.click('[data-range="30"]')
            # The week view already has seven labels; wait for the month labels (they carry day numbers).
            page.wait_for_function(
                "Array.from(document.querySelectorAll('.sc-xtick')).some(t => /\\d/.test(t.textContent))"
            )
            ticks = sorted(self._ticks(page), key=lambda tick: -tick[1])
            labels = [text for text, _ in ticks]
            self.assertEqual(labels[-1], "امروز")
            for text in labels:
                self.assertIsNone(PERSIAN_DIGITS.search(text), text)
            day_numbers = [int(re.match(r"\d+", text).group()) for text in labels[1:-1]]
            # Within one month the day of the month grows from right to left; a month change resets it.
            drops = sum(1 for a, b in zip(day_numbers, day_numbers[1:]) if b < a)
            self.assertLessEqual(drops, 1, labels)

        self._with_chart(check)


if __name__ == "__main__":
    unittest.main()
