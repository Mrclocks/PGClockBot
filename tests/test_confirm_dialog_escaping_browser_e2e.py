"""REAL browser: a confirm dialog shows its labels and phrase exactly as written, and never as markup.

The reason label escaped only «<» and the phrase placeholder only «"». That stopped injected elements,
but a label that contains an entity («&lt;», «&amp;») showed the decoded character instead of what was
written. panel.js now builds both with its one escapeHtml. Skipped when Playwright is not installed.
"""

from __future__ import annotations

import html
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PANEL_JS = ROOT / "app" / "web" / "static" / "panel.js"

REASON_LABEL = 'علت &lt;حذف&gt; & <img src=x onerror="window.pwned=1"> "نقل‌قول"'
PHRASE = 'حذف" autofocus onfocus="window.pwned=2" x="'
PHRASE_LABEL = "<b>بنویسید</b> &amp; تأیید"


def _page() -> str:
    attr = html.escape
    return f"""<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8" />
<meta name="csrf-token" content="e2e" /></head><body>
<form id="with-reason" method="post" action="/x" data-confirm="حذف شود؟" data-confirm-danger="1"
      data-confirm-reason="1" data-confirm-reason-label="{attr(REASON_LABEL)}">
  <input type="hidden" name="csrf_token" value="e2e" /><button type="submit" id="go-reason">حذف</button>
</form>
<form id="with-phrase" method="post" action="/x" data-confirm="حذف شود؟" data-confirm-danger="1"
      data-confirm-phrase="{attr(PHRASE)}" data-confirm-phrase-label="{attr(PHRASE_LABEL)}">
  <input type="hidden" name="csrf_token" value="e2e" /><button type="submit" id="go-phrase">حذف</button>
</form>
<div class="ui-modal" id="modal-confirm" hidden><div class="ui-modal-panel" role="dialog">
  <h2 id="confirm-title">تأیید</h2>
  <form id="confirm-form" action="#" method="post" data-panel-validate="0" novalidate>
    <p id="confirm-message"></p><div id="confirm-reason-slot" hidden></div>
    <button type="submit" id="confirm-submit">تأیید</button>
  </form></div></div>
<script src="/static/panel.js"></script></body></html>"""


@unittest.skipUnless(PANEL_JS.is_file(), "panel.js missing")
class ConfirmDialogEscapingE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise unittest.SkipTest(f"playwright not installed: {exc}") from exc

    def _open(self, check, button: str, field: str) -> None:
        from playwright.sync_api import sync_playwright

        from tests.test_delete_reason_browser_e2e import _launch

        with sync_playwright() as playwright:
            browser = _launch(playwright)
            page = browser.new_page()
            # Served through the browser's request routing: no server and no network.
            page.route("http://panel.test/", lambda route: route.fulfill(content_type="text/html", body=_page()))
            page.route("http://panel.test/static/panel.js", lambda route: route.fulfill(
                content_type="application/javascript", body=PANEL_JS.read_text(encoding="utf-8")))
            page.goto("http://panel.test/")
            page.click(button)
            page.wait_for_selector(field, state="attached")
            check(page)
            self.assertEqual(page.locator("#confirm-reason-slot img, #confirm-reason-slot b").count(), 0,
                             "no element came from the text")
            self.assertIsNone(page.evaluate("window.pwned"), "no injected handler ran")
            browser.close()

    def test_the_reason_label_reads_exactly_as_written(self) -> None:
        def check(page) -> None:
            self.assertEqual(page.text_content("#confirm-reason-label"), REASON_LABEL)

        self._open(check, "#go-reason", "#confirm-reason")

    def test_the_phrase_and_its_label_read_exactly_as_written(self) -> None:
        def check(page) -> None:
            self.assertEqual(page.text_content("#confirm-phrase-label"), PHRASE_LABEL)
            self.assertEqual(page.get_attribute("#confirm-phrase", "placeholder"), PHRASE)
            self.assertIsNone(page.get_attribute("#confirm-phrase", "onfocus"))
            page.focus("#confirm-phrase")

        self._open(check, "#go-phrase", "#confirm-phrase")


if __name__ == "__main__":
    unittest.main()
