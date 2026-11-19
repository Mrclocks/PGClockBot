"""REAL browser E2E: the bot connection settings page («اتصال ربات و پاسارگارد») runs its whole script.

A leftover listener on a «آدرس عمومی» field that moved to the general settings threw ``pubBase is not defined``
part-way through the page script. Everything after it never ran: the page did not sync its mode on load, the
restart confirmation had no handler, and the form fell back to a plain submit instead of its preview → restart
flow.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


from tests.test_delete_reason_browser_e2e import _launch
from tests.test_message_variables_entry import OWNER, _page

PROBE = """() => {
  const calls = [];
  window.fetch = (url, opts) => { calls.push(String(url)); return new Promise(() => {}); };
  const form = document.querySelector('form[data-public-base]');
  const submit = new Event('submit', { cancelable: true, bubbles: true });
  form.dispatchEvent(submit);
  return { handled: submit.defaultPrevented, fetched: calls.length };
}"""


class BotSettingsScriptTests(unittest.TestCase):
    def test_the_page_script_runs_to_the_end_and_owns_the_save(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # optional dependency in some environments
            raise unittest.SkipTest(f"playwright not installed: {exc}") from exc
        html = _page(
            "settings.html", "/settings", OWNER, query=b"tab=bot", tabs=[], tab="bot", is_panel_settings=True,
            env_values={"PUBLIC_BASE_URL": "https://panel.example"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            page_file = Path(tmp) / "page.html"
            page_file.write_text(html, encoding="utf-8")
            with sync_playwright() as playwright:
                browser = _launch(playwright)
                try:
                    page = browser.new_page()
                    errors: list[str] = []
                    page.on("pageerror", lambda exc: errors.append(str(exc)))
                    page.goto(page_file.as_uri())
                    got = page.evaluate(PROBE)
                finally:
                    browser.close()
        self.assertEqual([e for e in errors if "is not defined" in e], [], "the page script must not stop part-way")
        self.assertTrue(got["handled"], "the form's own save handler takes the submit")
        self.assertEqual(got["fetched"], 1, "saving starts with the preview request")


if __name__ == "__main__":
    unittest.main()
