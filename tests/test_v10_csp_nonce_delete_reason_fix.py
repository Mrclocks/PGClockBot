"""Regression: CSP nonce injection + delete-reason extraction (v10 panel fixes)."""

from __future__ import annotations

import unittest
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from app.services.csp_nonce import buffer_and_inject_nonce, inject_script_nonces
from app.services.delete_reason import delete_reason_too_short, extract_delete_reason

ROOT = Path(__file__).resolve().parents[1]


class CspNonceInjectionTests(unittest.TestCase):
    def test_injects_nonce_on_scripts_without_one(self):
        html = (
            '<html><script>var a=1</script>'
            '<script src="/static/panel.js"></script>'
            '<script nonce="keep">var b=2</script>'
            '<script type="application/json">{"x":1}</script></html>'
        )
        out = inject_script_nonces(html, "abc123")
        self.assertIn('<script nonce="abc123">var a=1</script>', out)
        self.assertIn('src="/static/panel.js" nonce="abc123"', out)
        self.assertIn('<script nonce="keep">', out)
        self.assertIn('type="application/json" nonce="abc123"', out)

    def test_middleware_streaming_response_gets_nonce(self):
        app = FastAPI()

        @app.middleware("http")
        async def stamp(request: Request, call_next):
            response = await call_next(request)
            return await buffer_and_inject_nonce(response, "mid-nonce")

        @app.get("/page")
        async def page():
            return HTMLResponse(
                "<html><head></head><body><script>window.X=1</script></body></html>"
            )

        client = TestClient(app)
        r = client.get("/page")
        self.assertEqual(r.status_code, 200)
        self.assertIn('nonce="mid-nonce"', r.text)
        self.assertIn("<script nonce=\"mid-nonce\">window.X=1</script>", r.text)

    def test_app_middleware_imports_buffer_helper(self):
        src = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        self.assertIn("buffer_and_inject_nonce", src)
        self.assertIn("csp_nonce", src)

    def test_buffer_preserves_multiple_set_cookie_headers(self):
        """dict(headers) would keep only the first Set-Cookie — must not drop csrf/session."""
        from starlette.responses import HTMLResponse

        app = FastAPI()

        @app.middleware("http")
        async def stamp(request: Request, call_next):
            response = await call_next(request)
            return await buffer_and_inject_nonce(response, "cookie-nonce")

        @app.get("/page")
        async def page():
            resp = HTMLResponse("<html><script>1</script></html>")
            resp.set_cookie("csrf", "tok-a", path="/")
            resp.set_cookie("setup_gate", "tok-b", path="/")
            return resp

        client = TestClient(app)
        r = client.get("/page")
        self.assertEqual(r.status_code, 200)
        self.assertIn('nonce="cookie-nonce"', r.text)
        # TestClient stores cookies; both must survive the rebuild.
        self.assertIn("csrf", r.cookies)
        self.assertIn("setup_gate", r.cookies)
        self.assertEqual(r.cookies["csrf"], "tok-a")
        self.assertEqual(r.cookies["setup_gate"], "tok-b")

    def test_no_inline_onclick_in_resellers_template(self):
        html = (ROOT / "app/web/templates/resellers.html").read_text(encoding="utf-8")
        self.assertNotIn("onclick=", html)
        self.assertIn("data-clear-and-submit", html)
        self.assertIn("data-clear-fields", html)
        js = (ROOT / "app/web/static/panel.js").read_text(encoding="utf-8")
        self.assertIn("data-clear-and-submit", js)



class DeleteReasonExtractionTests(unittest.TestCase):
    def test_prefers_reason_then_confirm_reason(self):
        self.assertEqual(extract_delete_reason({"reason": "abc"}), "abc")
        self.assertEqual(
            extract_delete_reason({"reason": "", "confirm_reason": "حذف تست"}),
            "حذف تست",
        )
        self.assertEqual(extract_delete_reason({}), "")

    def test_filled_then_empty_reason_does_not_shadow(self):
        """Starlette FormData.get is last-wins — empty trailing reason must not win."""
        from starlette.datastructures import FormData

        form = FormData(
            [
                ("reason", "حذف به دلیل تست کاربر"),
                ("reason", ""),
                ("confirm_reason", ""),
            ]
        )
        self.assertEqual(extract_delete_reason(form), "حذف به دلیل تست کاربر")
        self.assertFalse(delete_reason_too_short(extract_delete_reason(form)))

        form2 = FormData([("reason", ""), ("reason", "abc")])
        self.assertEqual(extract_delete_reason(form2), "abc")

        form3 = FormData([("reason", ""), ("confirm_reason", "حذف نماینده")])
        self.assertEqual(extract_delete_reason(form3), "حذف نماینده")

    def test_too_short(self):
        self.assertTrue(delete_reason_too_short("ab"))
        self.assertFalse(delete_reason_too_short("abc"))
        self.assertFalse(delete_reason_too_short("حذف"))

    def test_users_delete_uses_helper(self):
        src = (ROOT / "app/api/user_list_pages.py").read_text(encoding="utf-8")
        self.assertIn("extract_delete_reason", src)
        self.assertIn("delete_reason_too_short", src)

    def test_panel_js_root_fixes_delete_reason(self):
        js = (ROOT / "app/web/static/panel.js").read_text(encoding="utf-8")
        # v9-compatible in-place update + IME-safe validation + native submit after confirm
        self.assertIn("update existing hidden reason in place", js)
        self.assertIn("form.id === 'confirm-form'", js)
        # The too-short error names the modal's own reason label («علت تمدید», «علت حذف», ...).
        self.assertIn("activeReasonLabel + ' باید حداقل '", js)
        self.assertNotIn('minlength="', js[js.find("setupPanelConfirm") : js.find("Normalize Persian")])
        self.assertIn("compositionend", js)
        self.assertIn("confirm_reason", js)
        self.assertIn("stack: true", js)
        self.assertIn("novalidate", js)

    def test_templates_may_have_empty_reason_placeholder_updated_in_place(self):
        """v9 style: empty hidden reason is OK because applyReason updates it in place."""
        js = (ROOT / "app/web/static/panel.js").read_text(encoding="utf-8")
        self.assertIn("update existing hidden reason in place", js)
        users = (ROOT / "app/web/templates/users.html").read_text(encoding="utf-8")
        delete = users.split("/users/{{ u.id }}/delete", 1)[1].split("</form>", 1)[0]
        self.assertIn('name="reason"', delete)
        self.assertIn('data-confirm-reason="1"', delete)


if __name__ == "__main__":
    unittest.main()
