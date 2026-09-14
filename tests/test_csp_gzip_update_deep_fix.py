"""Deep CSRF/CSP guards: gzip must not strip script nonces; update UI lives in panel.js."""

from __future__ import annotations

import gzip
import re
import unittest
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from starlette.middleware.gzip import GZipMiddleware

from app.services.csp_nonce import buffer_and_inject_nonce, inject_script_nonces

ROOT = Path(__file__).resolve().parents[1]


class CspGzipNonceOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_gzip_outermost_stamps_bare_scripts(self):
        html = (
            "<!doctype html><html><body>"
            + ("x" * 2000)
            + '<script src="/static/panel.js" nonce="TEMPLATE"></script>'
            '<script>console.log("inline")</script></body></html>'
        )
        app = FastAPI()

        @app.middleware("http")
        async def security_headers(request: Request, call_next):
            request.state.csp_nonce = "TESTNONCE123"
            response = await call_next(request)
            nonce = request.state.csp_nonce
            response.headers["Content-Security-Policy"] = (
                f"script-src 'self' 'nonce-{nonce}'"
            )
            return await buffer_and_inject_nonce(response, nonce)

        @app.get("/")
        async def home():
            return HTMLResponse(html)

        # Same order as create_api_app: GZip added last → outermost.
        app.add_middleware(GZipMiddleware, minimum_size=50)

        messages: list[dict] = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [
                (b"accept-encoding", b"gzip"),
                (b"host", b"test"),
            ],
            "client": ("test", 50000),
            "server": ("test", 80),
        }
        await app(scope, receive, send)
        start = next(m for m in messages if m["type"] == "http.response.start")
        headers = {k.decode(): v.decode() for k, v in start["headers"]}
        body = b"".join(
            m.get("body", b"") for m in messages if m["type"] == "http.response.body"
        )
        self.assertEqual(headers.get("content-encoding"), "gzip")
        text = gzip.decompress(body).decode("utf-8")
        tags = re.findall(r"<script\b[^>]*>", text, flags=re.I)
        self.assertEqual(len(tags), 2)
        self.assertTrue(all("nonce=" in t.lower() for t in tags))
        self.assertIn('nonce="TESTNONCE123"', text)

    def test_inject_script_nonces_helper(self):
        html = "<html><body><script>alert(1)</script></body></html>"
        out = inject_script_nonces(html, "ABC")
        self.assertIn('nonce="ABC"', out)


class UpdateUiLivesInPanelJsTests(unittest.TestCase):
    def test_update_template_has_no_inline_script(self):
        html = (ROOT / "app/web/templates/_settings_update.html").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("<script", html.lower())
        self.assertIn('id="upd-root"', html)
        self.assertIn('id="upd-start"', html)

    def test_panel_js_boots_update_tab(self):
        js = (ROOT / "app/web/static/panel.js").read_text(encoding="utf-8")
        self.assertIn("initPanelUpdateTab", js)
        self.assertIn("/update/start", js)
        self.assertIn("X-CSRF-Token", js)
        self.assertIn("csrf_token", js)
        self.assertIn("panelFetch", js)
        self.assertIn("panelReadJson", js)

    def test_app_registers_gzip_outermost(self):
        src = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        gzip_idx = src.rfind("add_middleware(GZipMiddleware")
        inject_idx = src.find("buffer_and_inject_nonce")
        self.assertGreater(gzip_idx, 0)
        self.assertGreater(inject_idx, 0)
        self.assertGreater(gzip_idx, inject_idx)
        self.assertIn("compress AFTER CSP nonce injection", src)


if __name__ == "__main__":
    unittest.main()
