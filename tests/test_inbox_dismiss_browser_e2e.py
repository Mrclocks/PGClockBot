"""REAL browser E2E: the inbox «حذف اعلان» dialog keeps exactly one option selected and posts it.

The dialog markup comes from the production ``base.html``; panel.js and panel.css are the shipped files.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "app" / "web"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _dismiss_modal_html() -> str:
    base = (WEB / "templates" / "base.html").read_text(encoding="utf-8")
    # Cut by id: the modals' classes follow the design system and change with it.
    start = base.rindex("<div", 0, base.index('id="modal-inbox-dismiss"'))
    end = base.rindex("<div", 0, base.index('id="modal-confirm"'))
    # The same macro imports base.html makes before the modal.
    imports = "".join(line + "\n" for line in base.splitlines() if line.strip().startswith("{% from "))
    source = imports + base[start:end]
    env = Environment(loader=FileSystemLoader(str(WEB / "templates")), autoescape=True)
    return env.from_string(source).render(csrf_token="e2e-token")


def _page() -> str:
    return f"""<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8" />
<meta name="csrf-token" content="e2e-token" /><link rel="stylesheet" href="/static/panel.css" /></head>
<body><div id="flash"></div>
<button type="button" id="open-a" data-inbox-dismiss="ac:delivery">حذف اعلان</button>
{_dismiss_modal_html()}
<script src="/static/panel.js"></script></body></html>"""


class _Server:
    def __init__(self) -> None:
        self.port = _free_port()
        self.received: list[dict[str, list[str]]] = []
        app = FastAPI()
        app.mount("/static", StaticFiles(directory=str(WEB / "static")), name="static")

        @app.get("/")
        async def home() -> HTMLResponse:
            return HTMLResponse(_page())

        @app.post("/inbox/dismiss")
        async def dismiss(request: Request) -> RedirectResponse:
            body = (await request.body()).decode("utf-8")
            self.received.append(parse_qs(body, keep_blank_values=True))
            return RedirectResponse("/?ok=1", status_code=303)

        self._server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self._thread = threading.Thread(target=lambda: asyncio.run(self._server.serve()), daemon=True)

    def start(self) -> None:
        self._thread.start()
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.1):
                    return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("e2e server failed to start")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)


def _launch(playwright):
    """Default Chromium, or a pre-installed build when Playwright's pinned one is not downloaded."""
    try:
        return playwright.chromium.launch(headless=True)
    except Exception:  # noqa: BLE001 - playwright raises a generic Error for a missing browser
        builds = sorted(Path("/opt/pw-browsers").glob("chromium-*/chrome-linux/chrome"))
        if not builds:
            raise
        return playwright.chromium.launch(headless=True, executable_path=str(builds[-1]))


CHECKED = "() => Array.from(document.querySelectorAll('#form-inbox-dismiss input[type=radio]')).filter(r => r.checked).map(r => r.value)"
HIGHLIGHTED = "() => Array.from(document.querySelectorAll('.inbox-dismiss-option.is-selected input')).map(r => r.value)"


class InboxDismissBrowserE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise unittest.SkipTest(f"playwright not installed: {exc}") from exc

    def setUp(self) -> None:
        self.server = _Server()
        self.server.start()
        self.addCleanup(self.server.stop)

    def _open(self, page) -> None:
        page.goto(f"http://127.0.0.1:{self.server.port}/", wait_until="networkidle")
        page.click("#open-a")
        page.wait_for_selector("#modal-inbox-dismiss.open", timeout=5000)

    def test_choosing_an_option_leaves_exactly_one_selected(self) -> None:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = _launch(p)
            page = browser.new_page()
            self._open(page)
            self.assertEqual(page.evaluate(CHECKED), ["24h"])
            page.click("label.inbox-dismiss-option:has(input[value=forever])")
            self.assertEqual(page.evaluate(CHECKED), ["forever"])
            self.assertEqual(page.evaluate(HIGHLIGHTED), ["forever"])
            page.click("label.inbox-dismiss-option:has(input[value='24h'])")
            self.assertEqual(page.evaluate(CHECKED), ["24h"])
            self.assertEqual(page.evaluate(HIGHLIGHTED), ["24h"])
            browser.close()

    def test_forever_is_what_gets_posted(self) -> None:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = _launch(p)
            page = browser.new_page()
            self._open(page)
            page.click("label.inbox-dismiss-option:has(input[value=forever])")
            page.click("#inbox-dismiss-submit")
            page.wait_for_url("**/?ok=1*", timeout=10000)
            browser.close()
        self.assertEqual(len(self.server.received), 1)
        sent = self.server.received[0]
        self.assertEqual(sent["mode"], ["forever"])
        self.assertEqual(sent["alert_key"], ["ac:delivery"])

    def test_reopening_starts_from_the_24h_option_again(self) -> None:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = _launch(p)
            page = browser.new_page()
            self._open(page)
            page.click("label.inbox-dismiss-option:has(input[value=forever])")
            page.click("[data-modal-close].btn")
            page.wait_for_selector("#modal-inbox-dismiss.open", state="detached", timeout=5000)
            page.click("#open-a")
            page.wait_for_selector("#modal-inbox-dismiss.open", timeout=5000)
            self.assertEqual(page.evaluate(CHECKED), ["24h"])
            self.assertEqual(page.evaluate(HIGHLIGHTED), ["24h"])
            browser.close()


if __name__ == "__main__":
    unittest.main()
