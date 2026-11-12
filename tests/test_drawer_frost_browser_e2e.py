"""REAL browser E2E: the open mobile drawer and the topbar stay bright frosted glass, not a grey film over the dim."""

from __future__ import annotations

import base64
import threading
import time
import unittest
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from tests.test_delete_reason_browser_e2e import _free_port, _launch

ROOT = Path(__file__).resolve().parents[1]
PAGE = """<!doctype html>
<html lang="fa" dir="rtl" data-theme="{theme}"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<link rel="stylesheet" href="/static/panel.css" /></head>
<body class="nav-open"><div class="app">
<header class="topbar mobile-only"><a class="topbar-brand" href="#">MrClock</a>
<button type="button" class="menu-toggle" aria-label="منو"></button></header>
<aside class="side open" id="sidebar"><div class="side-panel"><p>منو</p></div></aside>
<main class="main ds"><div class="card"><p>صفحه</p></div></main>
<div class="side-backdrop show" id="side-backdrop" aria-hidden="true"></div>
</div></body></html>"""

# Reads one pixel of a PNG screenshot through a canvas, so the test needs no image library.
PIXEL_JS = """async ([src, x, y]) => {
  const img = new Image();
  img.src = src;
  await img.decode();
  const canvas = document.createElement('canvas');
  canvas.width = img.width;
  canvas.height = img.height;
  const ctx = canvas.getContext('2d');
  ctx.drawImage(img, 0, 0);
  return Array.from(ctx.getImageData(x, y, 1, 1).data.slice(0, 3));
}"""


class _Server:
    def __init__(self) -> None:
        self.port = _free_port()
        app = FastAPI()
        app.mount("/static", StaticFiles(directory=str(ROOT / "app" / "web" / "static")), name="static")

        @app.get("/closed")
        async def closed_page() -> HTMLResponse:
            html = PAGE.replace("{theme}", "light").replace('class="nav-open"', "")
            return HTMLResponse(html.replace("side open", "side").replace("side-backdrop show", "side-backdrop"))

        @app.get("/{theme}")
        async def page(theme: str) -> HTMLResponse:
            return HTMLResponse(PAGE.replace("{theme}", "light" if theme == "light" else "dark"))

        self._server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started and time.monotonic() < deadline:
            time.sleep(0.05)

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)


class DrawerFrostE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise unittest.SkipTest(f"playwright not installed: {exc}") from exc

    def test_light_theme_drawer_and_topbar_are_not_greyed_by_the_dim(self) -> None:
        from playwright.sync_api import sync_playwright

        server = _Server()
        server.start()
        try:
            with sync_playwright() as playwright:
                browser = _launch(playwright)
                page = browser.new_page(viewport={"width": 390, "height": 800})
                page.goto(f"http://127.0.0.1:{server.port}/light", wait_until="networkidle")
                page.wait_for_timeout(400)
                src = "data:image/png;base64," + base64.b64encode(page.screenshot()).decode()
                drawer = page.locator(".side-panel").bounding_box()
                topbar = page.locator(".topbar").bounding_box()

                def brightness(x: float, y: float) -> int:
                    return min(page.evaluate(PIXEL_JS, [src, int(x), int(y)]))

                inside_drawer = brightness(drawer["x"] + drawer["width"] / 2, drawer["y"] + drawer["height"] / 2)
                inside_topbar = brightness(topbar["x"] + topbar["width"] / 2, topbar["y"] + topbar["height"] / 2)
                dimmed_page = brightness(drawer["x"] / 2, drawer["y"] + drawer["height"] / 2)
                # Frosted glass over the light page stays near white (~253); over the dim it turned grey (~220).
                self.assertGreater(inside_drawer, 240)
                self.assertGreater(inside_topbar, 240)
                # The page beside the drawer is still dimmed.
                self.assertLess(dimmed_page, 200)
                browser.close()
        finally:
            server.stop()

    def test_the_dim_edge_slides_in_with_the_drawer_edge(self) -> None:
        from playwright.sync_api import sync_playwright

        server = _Server()
        server.start()
        try:
            with sync_playwright() as playwright:
                browser = _launch(playwright)
                page = browser.new_page(viewport={"width": 390, "height": 800})
                page.goto(f"http://127.0.0.1:{server.port}/closed", wait_until="networkidle")
                for fraction in (0.25, 0.5, 0.75):
                    with self.subTest(fraction=fraction):
                        edges = page.evaluate(FREEZE_OPEN_JS, fraction)
                        # Both edges have moved, and the dim ends exactly where the menu begins.
                        self.assertLess(edges["drawer"], 390)
                        self.assertAlmostEqual(edges["dim"], edges["drawer"], delta=1.0)
                browser.close()
        finally:
            server.stop()


# Opens the drawer, freezes every running transition at the same point, and returns the two edges in px.
FREEZE_OPEN_JS = """(fraction) => {
  const side = document.getElementById('sidebar');
  const back = document.getElementById('side-backdrop');
  side.classList.remove('open');
  back.classList.remove('show');
  document.getAnimations().forEach((a) => a.cancel());
  void side.offsetWidth;
  side.classList.add('open');
  back.classList.add('show');
  document.body.classList.add('nav-open');
  for (const a of document.getAnimations()) {
    a.pause();
    a.currentTime = a.effect.getComputedTiming().duration * fraction;
  }
  const panel = side.querySelector('.side-panel').getBoundingClientRect();
  const dim = getComputedStyle(back, '::before');
  const shift = new DOMMatrix(dim.transform).m41;
  const dimRight = back.getBoundingClientRect().right - parseFloat(dim.right) + shift;
  return { drawer: panel.left, dim: dimRight };
}"""


if __name__ == "__main__":
    unittest.main()
