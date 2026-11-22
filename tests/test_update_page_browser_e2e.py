"""REAL browser E2E: the update page script drives the new version card.

Tapping a channel in the capsule switches the hidden channel select and asks the server for that channel; the
answer recolours the card (green and no install button when that channel has nothing newer) and redraws the
changelog. «نمایش همه» reveals the notes past the first five.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.test_delete_reason_browser_e2e import _launch
from tests.test_update_page_design import render_update, _notes

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "web" / "static"
DEV_ANSWER = {
    "ok": True, "channel": "dev", "channel_label": "توسعه", "local_version": "0.9.0", "can_start_update": False,
    "update_info": {"update_available": False, "remote_version": "0.9.0", "checked": True},
    "migration_preflight": {}, "channel_check_tone": "ok",
    "changelog": {"title": "تغییرات نسخه 0.9.0", "has_notes": True, "blocks": [
        {"version": "0.9.0", "items": [{"topic": "<b>x</b>", "text": "<img src=x>", "refs": ["#1"], "tone": "fix"}]}]},
}
STUB = """(answer) => {
  window.__calls = [];
  window.fetch = (url, opts) => {
    window.__calls.push({ url: String(url), body: opts && opts.body });
    return Promise.resolve(new Response(JSON.stringify(answer), { status: 200 }));
  };
}"""
STATE = """() => ({
  tone: document.getElementById('upd-hero').dataset.tone,
  select: document.getElementById('upd-channel').value,
  active: document.querySelector('#upd-channel-seg .active').dataset.channel,
  startHidden: document.getElementById('upd-start').hidden,
  oldHidden: document.getElementById('upd-local-box').hidden,
  rows: document.querySelectorAll('.upd-note-row').length,
  hiddenRows: document.querySelectorAll('.upd-note-row[hidden]').length,
  moreHidden: document.getElementById('upd-changelog-more').hidden,
  injected: document.querySelectorAll('#upd-changelog-blocks img, #upd-changelog-blocks b').length,
  calls: window.__calls || [],
})"""


class UpdatePageScriptTests(unittest.TestCase):
    def _open(self, playwright, tmp: str):
        page_file = Path(tmp) / "page.html"
        page_file.write_text(render_update(available=True, notes=_notes(7)).html, encoding="utf-8")
        browser = _launch(playwright)
        page = browser.new_page()
        page.goto(page_file.as_uri())
        for sheet in ("panel.css", "panel-ds.css"):
            page.add_style_tag(path=str(STATIC / sheet))
        page.evaluate(STUB, DEV_ANSWER)
        page.add_script_tag(path=str(STATIC / "panel.js"))
        return browser, page

    def test_tapping_a_channel_checks_it_and_recolours_the_card(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # optional dependency in some environments
            raise unittest.SkipTest(f"playwright not installed: {exc}") from exc
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as playwright:
            browser, page = self._open(playwright, tmp)
            try:
                before = page.evaluate(STATE)
                page.click('#upd-channel-seg button[data-channel="dev"]')
                page.wait_for_function("document.getElementById('upd-hero').dataset.tone === 'ok'")
                after = page.evaluate(STATE)
            finally:
                browser.close()
        self.assertEqual((before["tone"], before["startHidden"], before["oldHidden"]), ("brand", False, False))
        channel_calls = [c for c in after["calls"] if "/update/channel" in c["url"]]
        self.assertEqual(len(channel_calls), 1)
        self.assertEqual(json.loads(channel_calls[0]["body"])["channel"], "dev")
        self.assertEqual((after["select"], after["active"]), ("dev", "dev"))
        self.assertTrue(after["startHidden"], "nothing newer on that channel: no install button")
        self.assertTrue(after["oldHidden"], "only the installed version is shown")
        self.assertEqual(after["rows"], 1)
        self.assertEqual(after["injected"], 0, "note text from the server is escaped")

    def test_show_all_reveals_the_rest_of_the_notes(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # optional dependency in some environments
            raise unittest.SkipTest(f"playwright not installed: {exc}") from exc
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as playwright:
            browser, page = self._open(playwright, tmp)
            try:
                before = page.evaluate(STATE)
                page.click("#upd-changelog-more")
                after = page.evaluate(STATE)
            finally:
                browser.close()
        self.assertEqual((before["rows"], before["hiddenRows"]), (7, 2))
        self.assertEqual(after["hiddenRows"], 0)
        self.assertTrue(after["moreHidden"])


if __name__ == "__main__":
    unittest.main()
