"""Main pages show their title without a caption line under it."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from starlette.requests import Request

from app.api import app as app_module

OWNER = {
    "role": "admin", "permissions": [], "org_principal_id": 1, "org_depth": 0,
    "org_parent_id": None, "org_status": "active",
}


def _request(path: str) -> Request:
    return Request(
        {
            "type": "http", "asgi": {"version": "3"}, "http_version": "1.1", "method": "GET", "scheme": "https",
            "path": path, "raw_path": path.encode(), "query_string": b"",
            "headers": [(b"host", b"example.test")], "client": ("127.0.0.1", 123),
            "server": ("example.test", 443), "state": {},
        }
    )


class InboxTitleTests(unittest.TestCase):
    def test_the_inbox_title_has_no_caption_under_it(self):
        ctx = {
            "staff": OWNER, "update": None, "ticket_alert": None, "action_center": {}, "shop_maintenance": False,
            "capacity_warn": False, "payg_risk": {}, "inbox_count": 0, "inbox_has": False,
        }
        html = app_module.render(_request("/inbox"), "inbox.html", ctx).body.decode("utf-8")
        self.assertIn("<h1>مرکز اقدام</h1>", html)
        self.assertNotIn("page-sub", html)


if __name__ == "__main__":
    unittest.main()
