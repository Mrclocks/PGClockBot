"""CSRF middleware must not empty POST bodies for Form()/request.form() handlers.

Regression: BaseHTTPMiddleware + request.form() without body() first caused
settings to flash «ذخیره شد» with no write, and POST /plans to 422 missing name/price.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse
from starlette.testclient import TestClient

from app.services.csrf import CSRF_FORM_FIELD, CSRF_HEADER, extract_csrf_from_request

ROOT = Path(__file__).resolve().parents[1]


def _app_with_csrf_form_extract() -> FastAPI:
    """Mirror production: middleware extracts CSRF from form, then Form()/form() run."""
    app = FastAPI()

    @app.middleware("http")
    async def csrf_mw(request: Request, call_next):
        tok = await extract_csrf_from_request(request)
        if not tok:
            return JSONResponse({"ok": False, "error": "csrf"}, status_code=403)
        return await call_next(request)

    @app.post("/plans")
    async def plans_create(name: str = Form(...), price: int = Form(...)):
        return {"ok": True, "name": name, "price": price}

    @app.post("/plans/{plan_id}/edit")
    async def plans_edit(
        plan_id: int,
        request: Request,
        name: str = Form(...),
        price: int = Form(...),
    ):
        form = await request.form()
        return {
            "ok": True,
            "plan_id": plan_id,
            "name": name,
            "price": price,
            "description": form.get("description"),
        }

    @app.post("/users/{user_id}/edit")
    async def users_edit(user_id: int, request: Request):
        # Same pattern as app/api/user_pages.py — body via request.form() only.
        form = await request.form()
        return {
            "ok": True,
            "user_id": user_id,
            "note": form.get("note"),
            "color_tag": form.get("color_tag"),
        }

    @app.post("/resellers/plans")
    async def reseller_plans_create(request: Request):
        # Same pattern as reseller_pages — multipart/urlencoded form dict.
        form = await request.form()
        return {
            "ok": True,
            "name": form.get("name"),
            "price": form.get("price"),
            "plan_kind": form.get("plan_kind"),
        }

    @app.post("/settings")
    async def settings_save(request: Request):
        form = await request.form()
        return {
            "ok": True,
            "shop_title": form.get("s_shop_title"),
            "welcome_text": form.get("s_welcome_text"),
            "keys": list(form.keys()),
        }

    return app


class CsrfBodyReplayTests(unittest.TestCase):
    def test_extract_buffers_body_before_form(self):
        src = (ROOT / "app/services/csrf.py").read_text(encoding="utf-8")
        start = src.find("async def extract_csrf_from_request")
        self.assertGreater(start, 0)
        block = src[start:]
        # Must call body() before form() for BaseHTTPMiddleware replay.
        form_at = block.find("await request.form()")
        body_at = block.find("await request.body()")
        self.assertGreater(form_at, 0)
        self.assertGreater(body_at, 0)
        self.assertLess(body_at, form_at)
        self.assertIn("_CachedRequest", block)

    def test_urlencoded_form_reaches_form_deps(self):
        client = TestClient(_app_with_csrf_form_extract())
        res = client.post(
            "/plans",
            data={"name": "ویژه", "price": "150000", CSRF_FORM_FIELD: "tok"},
        )
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["name"], "ویژه")
        self.assertEqual(res.json()["price"], 150000)

    def test_urlencoded_settings_fields_persist_through_middleware(self):
        client = TestClient(_app_with_csrf_form_extract())
        res = client.post(
            "/settings",
            data={
                "s_shop_title": "فروشگاه تست",
                "s_welcome_text": "سلام جدید",
                CSRF_FORM_FIELD: "tok",
            },
        )
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        self.assertEqual(data["shop_title"], "فروشگاه تست")
        self.assertEqual(data["welcome_text"], "سلام جدید")
        self.assertIn("s_shop_title", data["keys"])

    def test_multipart_settings_fields_persist_through_middleware(self):
        client = TestClient(_app_with_csrf_form_extract())
        res = client.post(
            "/settings",
            files={
                "s_shop_title": (None, "مولتی"),
                "s_welcome_text": (None, "متن مولتی"),
                CSRF_FORM_FIELD: (None, "tok"),
            },
        )
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        self.assertEqual(data["shop_title"], "مولتی")
        self.assertEqual(data["welcome_text"], "متن مولتی")

    def test_header_csrf_skips_body_parse(self):
        client = TestClient(_app_with_csrf_form_extract())
        res = client.post(
            "/plans",
            data={"name": "hdr", "price": "1"},
            headers={CSRF_HEADER: "tok-header"},
        )
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["name"], "hdr")

    def test_plan_edit_form_deps_and_request_form(self):
        client = TestClient(_app_with_csrf_form_extract())
        res = client.post(
            "/plans/7/edit",
            data={
                "name": "ویرایش‌شده",
                "price": "200000",
                "description": "توضیح",
                CSRF_FORM_FIELD: "tok",
            },
        )
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        self.assertEqual(data["plan_id"], 7)
        self.assertEqual(data["name"], "ویرایش‌شده")
        self.assertEqual(data["price"], 200000)
        self.assertEqual(data["description"], "توضیح")

    def test_user_edit_request_form_body(self):
        client = TestClient(_app_with_csrf_form_extract())
        res = client.post(
            "/users/42/edit",
            data={"note": "یادداشت", "color_tag": "green", CSRF_FORM_FIELD: "tok"},
        )
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        self.assertEqual(data["user_id"], 42)
        self.assertEqual(data["note"], "یادداشت")
        self.assertEqual(data["color_tag"], "green")

    def test_reseller_plan_create_request_form_body(self):
        client = TestClient(_app_with_csrf_form_extract())
        res = client.post(
            "/resellers/plans",
            data={
                "name": "نقره‌ای",
                "price": "0",
                "plan_kind": "subscription",
                CSRF_FORM_FIELD: "tok",
            },
        )
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        self.assertEqual(data["name"], "نقره‌ای")
        self.assertEqual(data["price"], "0")
        self.assertEqual(data["plan_kind"], "subscription")

    def test_panel_save_routes_use_shared_csrf_guard(self):
        """All panel mutations share csrf_origin_guard → extract_csrf_from_request."""
        app_src = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        self.assertIn("extract_csrf_from_request", app_src)
        self.assertIn("csrf_origin_guard", app_src)
        # Representative save entrypoints stay form-backed (not JSON-only).
        user_src = (ROOT / "app/api/user_pages.py").read_text(encoding="utf-8")
        reseller_src = (ROOT / "app/api/reseller_pages.py").read_text(encoding="utf-8")
        self.assertIn("await request.form()", user_src)
        self.assertIn("await request.form()", reseller_src)
        plan_src = (ROOT / "app/api/plan_pages.py").read_text(encoding="utf-8")
        self.assertIn('name: str = Form(...)', plan_src)
        self.assertIn("@app.post(\"/plans/{plan_id}/edit\")", plan_src)


class CsrfExtractUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_extract_calls_body_before_form(self):
        req = MagicMock()
        req.headers = {"content-type": "application/x-www-form-urlencoded"}
        req.body = AsyncMock(return_value=b"csrf_token=abc&name=x")
        form = MagicMock()
        form.get = MagicMock(return_value="abc")
        req.form = AsyncMock(return_value=form)
        tok = await extract_csrf_from_request(req)
        self.assertEqual(tok, "abc")
        req.body.assert_awaited()
        req.form.assert_awaited()
        self.assertEqual(req.method_calls[0], call.body())


class SettingsEmptyPayloadGuardTests(unittest.TestCase):
    def test_settings_save_rejects_empty_payload_when_keys_expected(self):
        src = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        self.assertIn("elif known:", src)
        self.assertIn("ذخیره انجام نشد", src)


if __name__ == "__main__":
    unittest.main()
