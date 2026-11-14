"""GET on POST-only delete routes must not strand operators on a bare 405 page."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("BOT_TOKEN", "1:test")


class DeletePostOnly405Tests(unittest.TestCase):
    def test_get_admin_delete_shows_persian_guidance(self):
        from starlette.testclient import TestClient

        from app.api.app import create_api_app

        app = create_api_app()
        client = TestClient(app, raise_server_exceptions=False)
        # Unauthenticated GET still hits method-matching after auth redirect in
        # some paths; force raw route via POST-only match with GET.
        res = client.get(
            "/pg/admins/bob/delete",
            headers={"referer": "https://example.test/pg/admins"},
            follow_redirects=False,
        )
        # Login gate may 303 first; follow once if so, then assert 405 page.
        if res.status_code in {302, 303, 307}:
            # Without session, auth middleware redirects — that's fine; the
            # Method Not Allowed path is what logged-in operators hit.
            # Simulate by calling the exception handler path via a POST-only
            # route match on a mounted app without auth for this assertion:
            from starlette.exceptions import HTTPException as StarletteHTTPException
            from starlette.requests import Request

            scope = {
                "type": "http",
                "asgi": {"version": "3"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "https",
                "path": "/pg/admins/bob/delete",
                "raw_path": b"/pg/admins/bob/delete",
                "query_string": b"",
                "headers": [
                    (b"host", b"example.test"),
                    (b"referer", b"https://example.test/pg/admins"),
                ],
                "client": ("127.0.0.1", 123),
                "server": ("example.test", 443),
            }
            request = Request(scope)

            async def _run():
                handler = None
                for reg in app.exception_handlers.items():
                    if reg[0] is StarletteHTTPException:
                        handler = reg[1]
                        break
                self.assertIsNotNone(handler)
                resp = await handler(
                    request, StarletteHTTPException(status_code=405)
                )
                body = resp.body.decode("utf-8")
                self.assertEqual(resp.status_code, 405)
                self.assertIn("این عملیات از این آدرس ممکن نیست", body)
                self.assertIn("حذف ادمین یا پلن", body)
                return resp

            import asyncio

            asyncio.run(_run())
            return
        self.assertEqual(res.status_code, 405)
        self.assertIn("این عملیات از این آدرس ممکن نیست", res.text)

    def test_get_plan_delete_route_is_post_only(self):
        from starlette.routing import Match

        from app.api.app import create_api_app

        app = create_api_app()
        for method, path in (
            ("GET", "/pg/admins/bob/delete"),
            ("GET", "/plans/3/delete"),
            ("POST", "/pg/admins/bob/delete"),
            ("POST", "/plans/3/delete"),
        ):
            scope = {
                "type": "http",
                "method": method,
                "path": path,
                "headers": [],
            }
            matches = []
            for route in app.router.routes:
                m, _ = route.matches(scope)
                if m != Match.NONE:
                    matches.append(
                        (m, getattr(route, "path", None), getattr(route, "methods", None))
                    )
            if method == "GET":
                self.assertTrue(
                    any(m == Match.PARTIAL for m, _, _ in matches),
                    msg=f"expected PARTIAL for GET {path}: {matches}",
                )
            else:
                self.assertTrue(
                    any(m == Match.FULL for m, _, _ in matches),
                    msg=f"expected FULL for POST {path}: {matches}",
                )

    def test_friendly_pg_maps_failed_405(self):
        from app.services.credential_policy import friendly_pg_error

        msg = friendly_pg_error("DELETE /api/admin/by-id/1 failed (405)", status_code=405)
        self.assertIn("پاسارگارد", msg)
        self.assertNotIn("405", msg)
        self.assertNotIn("failed", msg.lower())


if __name__ == "__main__":
    unittest.main()
