"""Rollback-to-version API (3.0.5+) — UI restored to 3.0.4 snapshot rollback."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch


class RecentVersionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_recent_from_releases(self):
        from app.services.updates import clear_update_cache, fetch_recent_versions

        clear_update_cache()
        payload = [
            {"tag_name": "v3.0.5", "name": "v3.0.5", "draft": False, "published_at": "2026-07-31T00:00:00Z"},
            {"tag_name": "v3.0.4", "name": "v3.0.4", "draft": False, "published_at": "2026-07-30T00:00:00Z"},
            {"tag_name": "v3.0.3", "name": "v3.0.3", "draft": False, "published_at": "2026-07-29T00:00:00Z"},
            {"tag_name": "v3.0.2", "name": "v3.0.2", "draft": False, "published_at": "2026-07-28T00:00:00Z"},
        ]

        class FakeResp:
            status_code = 200

            def json(self):
                return payload

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, url, **kwargs):
                return FakeResp()

        with patch("app.services.updates.httpx.AsyncClient", return_value=FakeClient()):
            rows = await fetch_recent_versions(limit=3, force=True)
        self.assertEqual([r["version"] for r in rows], ["3.0.5", "3.0.4", "3.0.3"])
        self.assertTrue(all(r["tag"].startswith("v") for r in rows))


class RollbackStartTests(unittest.TestCase):
    def test_rejects_version_outside_allowlist(self):
        from app.services.panel_update import start_rollback_to_version

        out = start_rollback_to_version("1.0.0", allowed=["3.0.5", "3.0.4", "3.0.3"])
        self.assertFalse(out["ok"])
        self.assertIn("۳ نسخه", out["error"])

    def test_rejects_current_version(self):
        from app.services.panel_update import start_rollback_to_version
        from app.version import __version__

        out = start_rollback_to_version(__version__, allowed=[__version__, "3.0.0", "2.9.0"])
        self.assertFalse(out["ok"])
        self.assertIn("همین الان", out["error"])


class UpdateUiSourceTests(unittest.TestCase):
    def test_update_page_uses_304_snapshot_rollback(self):
        src = Path("app/web/templates/_settings_update.html").read_text(encoding="utf-8")
        self.assertIn("snap-rollback", src)
        self.assertIn("upd-log", src)
        self.assertIn("upd-steps", src)
        self.assertIn("get.sh", src)
        self.assertNotIn('id="rollback-version"', src)
        self.assertNotIn("rollback-row", src)

    def test_context_exposes_rollback_versions_and_snapshots(self):
        src = Path("app/services/panel_update.py").read_text(encoding="utf-8")
        self.assertIn("fetch_recent_versions", src)
        self.assertIn("rollback_versions", src)
        self.assertIn("snapshots", src)
        self.assertIn("start_rollback_to_version", src)
        self.assertIn("_do_rollback_to_version", src)

    def test_force_channels_match_304_markup(self):
        html = Path("app/web/templates/_settings_field.html").read_text(encoding="utf-8")
        js = Path("app/web/static/panel.js").read_text(encoding="utf-8")
        self.assertIn("force-channel-row", html)
        self.assertIn("force-channel-id", js)
        self.assertNotIn("force-channel-fields", js)
        self.assertNotIn("force-channel-id-wrap", js)


if __name__ == "__main__":
    unittest.main()
