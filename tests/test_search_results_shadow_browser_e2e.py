"""REAL browser E2E: the phone search results float with the header's shadow, not a clipped grey block.

The open search field in the phone header clipped everything outside its own sides, so the results' heavy
shadow was cut into a grey rectangle around them, and the line between two results was a ring that drew the
second result as a separate card. The results now carry the header capsule's own shadow, the open field lets it
spread past its sides, and results are split by a hairline.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from tests.test_delete_reason_browser_e2e import _launch
from tests.test_message_variables_entry import OWNER, _page

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "web" / "static"
OPEN = """() => {
  const results = document.getElementById('panel-search-results-top');
  results.innerHTML = ['متن دکمه‌ها', 'رنگ دکمه‌ها'].map((w) =>
    '<a class="side-search-hit" href="#"><span class="side-search-label">درخواست لغو سرویس</span>' +
    '<span class="side-search-where">' + w + '</span></a>').join('');
  results.hidden = false;
  document.getElementById('panel-search-input-top').value = 'درخواست لغو';
}"""
PROBE = """() => {
  const field = document.querySelector('.topbar .side-search.topbar-search');
  const hits = document.querySelectorAll('#panel-search-results-top .side-search-hit');
  return {
    clip: getComputedStyle(field).clipPath,
    resultsShadow: getComputedStyle(document.getElementById('panel-search-results-top')).boxShadow,
    headerShadow: getComputedStyle(document.querySelector('.topbar')).boxShadow,
    secondRing: getComputedStyle(hits[1]).boxShadow,
    divider: getComputedStyle(hits[1], '::before').height,
  };
}"""


def _side_insets(clip: str) -> tuple[float, float]:
    """Right and left insets of a computed ``inset(...)`` clip (one to four lengths, like margin)."""
    match = re.match(r"inset\(([^)]*?)(?:\s+round\b.*)?\)", clip)
    if not match:
        raise AssertionError(f"not an inset clip: {clip}")
    values = [float(v.replace("px", "")) for v in match.group(1).split()]
    right = values[1] if len(values) > 1 else values[0]
    return right, values[3] if len(values) == 4 else right


class SearchResultsShadowTests(unittest.TestCase):
    def _probe(self, theme: str) -> dict:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # optional dependency in some environments
            raise unittest.SkipTest(f"playwright not installed: {exc}") from exc
        html = _page("settings.html", "/settings", OWNER, query=b"tab=general", tabs=[], tab="general",
                     is_panel_settings=True, env_values={})
        html = html.replace('data-theme="dark"', f'data-theme="{theme}"', 1)
        # The page's own stylesheets, in its own order, read from disk; scripts stay out (they need the server).
        html = re.sub(r'href="/static/([\w.-]+\.css)[^"]*"', lambda m: f'href="{(STATIC / m.group(1)).as_uri()}"', html)
        html = re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.S)
        with tempfile.TemporaryDirectory() as tmp:
            page_file = Path(tmp) / "page.html"
            page_file.write_text(html, encoding="utf-8")
            with sync_playwright() as playwright:
                browser = _launch(playwright)
                try:
                    page = browser.new_page(viewport={"width": 390, "height": 844})
                    page.goto(page_file.as_uri())
                    page.evaluate(OPEN)
                    page.focus("#panel-search-input-top")
                    page.wait_for_timeout(700)
                    return page.evaluate(PROBE)
                finally:
                    browser.close()

    def test_results_take_the_header_shadow_and_it_is_not_cut_at_the_sides(self) -> None:
        for theme in ("dark", "light"):
            got = self._probe(theme)
            self.assertEqual(got["resultsShadow"], got["headerShadow"], f"{theme}: same lift as the header capsule")
            for side in _side_insets(got["clip"]):
                self.assertLess(side, 0, f"{theme}: the open field lets the shadow out {got['clip']}")

    def test_results_are_split_by_a_hairline_not_a_ring(self) -> None:
        got = self._probe("light")
        self.assertEqual(got["secondRing"], "none")
        self.assertEqual(got["divider"], "1px")


if __name__ == "__main__":
    unittest.main()
