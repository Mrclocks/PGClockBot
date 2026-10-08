"""Update channel (main/dev): normalize, URLs, migration preflight, version check."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.update_channel import (
    badge_context,
    channel_label_fa,
    evaluate_migration_preflight,
    github_release_notes_url,
    github_version_url,
    normalize_channel,
    parse_revision_assignment,
)
from app.services.updates import check_github_update, clear_update_cache


class NormalizeChannelTests(unittest.TestCase):
    def test_aliases(self):
        self.assertEqual(normalize_channel("main"), "main")
        self.assertEqual(normalize_channel("stable"), "main")
        self.assertEqual(normalize_channel("DEV"), "dev")
        self.assertEqual(normalize_channel("develop"), "dev")
        self.assertEqual(normalize_channel(""), "main")
        self.assertEqual(normalize_channel(None), "main")
        self.assertEqual(normalize_channel("nope"), "main")

    def test_labels(self):
        self.assertEqual(channel_label_fa("main"), "پایدار")
        self.assertEqual(channel_label_fa("dev"), "توسعه")

    def test_badge_follows_deployed_not_update_selection(self):
        with patch("app.services.update_channel.get_deployed_channel", return_value="main"):
            badge = badge_context()
            self.assertEqual(badge["deployed_channel"], "main")
            self.assertEqual(badge["deployed_channel_label"], "پایدار")
        # Explicit deployed=dev still wins even if preferred update channel differs.
        badge_dev = badge_context("dev")
        self.assertEqual(badge_dev["deployed_channel"], "dev")
        self.assertEqual(badge_dev["deployed_channel_label"], "توسعه")
        self.assertNotEqual(badge_dev["deployed_channel_label"], channel_label_fa("main"))

    def test_badge_dev_when_deployed_dev(self):
        badge = badge_context("dev")
        self.assertEqual(badge["deployed_channel"], "dev")
        self.assertEqual(badge["deployed_channel_label"], "توسعه")


class ChannelUrlTests(unittest.TestCase):
    def test_version_and_notes_urls(self):
        self.assertIn("/main/VERSION", github_version_url(channel="main"))
        self.assertIn("/dev/VERSION", github_version_url(channel="dev"))
        self.assertIn(
            "/dev/app/services/release_notes.py",
            github_release_notes_url(channel="dev"),
        )


class MigrationPreflightTests(unittest.TestCase):
    def test_ok_when_db_rev_on_remote(self):
        out = evaluate_migration_preflight(
            db_revision="0035_payment_review_messages",
            remote_revision_ids={"0034_service_automations", "0035_payment_review_messages"},
        )
        self.assertTrue(out["checked"])
        self.assertFalse(out["blocked"])
        self.assertEqual(out["tone"], "ok")

    def test_blocked_when_db_ahead_of_channel(self):
        out = evaluate_migration_preflight(
            db_revision="0035_payment_review_messages",
            remote_revision_ids={"0034_service_automations"},
        )
        self.assertTrue(out["checked"])
        self.assertTrue(out["blocked"])
        self.assertEqual(out["tone"], "err")
        self.assertIn("0035_payment_review_messages", out["message"])

    def test_warn_when_remote_unknown(self):
        out = evaluate_migration_preflight(
            db_revision="0035_payment_review_messages",
            remote_revision_ids=None,
        )
        self.assertFalse(out["checked"])
        self.assertFalse(out["blocked"])
        self.assertEqual(out["tone"], "warn")

    def test_parse_revision_assignment(self):
        src = 'revision = "0035_payment_review_messages"\ndown_revision = "0034"\n'
        self.assertEqual(
            parse_revision_assignment(src), "0035_payment_review_messages"
        )


class _FakeResp:
    def __init__(self, status_code: int, *, text: str = "", json_data=None):
        self.status_code = status_code
        self.text = text
        self._json = json_data or {}

    def json(self):
        return self._json


class ChannelAwareUpdateCheckTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clear_update_cache()

    async def test_dev_uses_dev_version_url_not_releases(self):
        calls: list[str] = []

        async def fake_get(url, **kwargs):
            u = str(url)
            calls.append(u)
            if u.rstrip("/").endswith("/VERSION") or "/VERSION?" in u or "/dev/VERSION" in u:
                return _FakeResp(200, text="9.9.9\n")
            if "/contents/VERSION" in u:
                return _FakeResp(404)
            if u.rstrip("/").endswith("/releases") or "/releases?" in u:
                return _FakeResp(200, json_data=[])
            return _FakeResp(404)

        client = MagicMock()
        client.get = AsyncMock(side_effect=fake_get)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)

        with patch("app.services.updates.httpx.AsyncClient", return_value=client), patch(
            "app.services.updates.local_version", return_value="0.2.0"
        ):
            info = await check_github_update(force=True, channel="dev")

        self.assertTrue(info["checked"])
        self.assertEqual(info["channel"], "dev")
        self.assertEqual(info["remote_version"], "9.9.9")
        self.assertTrue(info["update_available"])
        self.assertIn("توسعه", info["label"])
        self.assertIn("آپدیت", info["label"])
        self.assertTrue(any("/dev/VERSION" in u for u in calls))
        self.assertFalse(any("/releases/latest" in u for u in calls))

    async def test_dev_falls_back_to_contents_api_and_prerelease(self):
        import base64

        calls: list[str] = []

        async def fake_get(url, **kwargs):
            u = str(url)
            calls.append(u)
            if "raw.githubusercontent" in u:
                raise RuntimeError("cdn blocked")
            if "/contents/VERSION" in u:
                body = base64.b64encode(b"9.9.8\n").decode()
                return _FakeResp(200, json_data={"content": body})
            if "/releases" in u and "/latest" not in u:
                return _FakeResp(
                    200,
                    json_data=[
                        {
                            "tag_name": "v9.9.7",
                            "prerelease": True,
                            "target_commitish": "abc1234",
                        }
                    ],
                )
            return _FakeResp(404)

        client = MagicMock()
        client.get = AsyncMock(side_effect=fake_get)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)

        with patch("app.services.updates.httpx.AsyncClient", return_value=client), patch(
            "app.services.updates.local_version", return_value="0.2.0"
        ):
            info = await check_github_update(force=True, channel="dev")

        self.assertTrue(info["checked"])
        self.assertEqual(info["remote_version"], "9.9.8")
        self.assertTrue(any("/contents/VERSION" in u for u in calls))

    async def test_dev_reports_no_update_when_already_latest(self):
        async def fake_get(url, **kwargs):
            if "VERSION" in str(url):
                return _FakeResp(200, text="0.2.21\n")
            return _FakeResp(404)

        client = MagicMock()
        client.get = AsyncMock(side_effect=fake_get)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)

        with patch("app.services.updates.httpx.AsyncClient", return_value=client), patch(
            "app.services.updates.local_version", return_value="0.2.21"
        ):
            info = await check_github_update(force=True, channel="dev")

        self.assertTrue(info["checked"])
        self.assertFalse(info["update_available"])
        self.assertIn("توسعه", info["label"])
        self.assertIn("نیست", info["label"])

    async def test_main_still_falls_back_to_releases(self):
        async def fake_get(url, **kwargs):
            u = str(url)
            if "VERSION" in u:
                raise RuntimeError("blocked")
            if u.endswith("/releases/latest"):
                return _FakeResp(200, json_data={"tag_name": "v3.2.2"})
            return _FakeResp(404)

        client = MagicMock()
        client.get = AsyncMock(side_effect=fake_get)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)

        with patch("app.services.updates.httpx.AsyncClient", return_value=client), patch(
            "app.services.updates.local_version", return_value="3.2.1"
        ):
            info = await check_github_update(force=True, channel="main")

        self.assertTrue(info["checked"])
        self.assertEqual(info["remote_version"], "3.2.2")
        self.assertEqual(info["channel"], "main")


class MigrationFlashNoDuplicateTests(unittest.TestCase):
    """Migration error must not also fill the version flash (channel switch)."""

    def test_payload_keeps_migration_text_out_of_channel_check_message(self):
        src = open("app/services/panel_update.py", encoding="utf-8").read()
        # Blocked preflight only flips tone — message stays version-status copy.
        self.assertIn("Keep update-status copy separate from migration_preflight", src)
        self.assertNotIn(
            'check_message = str(preflight.get("message") or preflight.get("label") or "")',
            src,
        )

    def test_js_hides_version_flash_when_migration_blocked(self):
        js = open("app/web/static/panel.js", encoding="utf-8").read()
        self.assertIn("Migration error already lives in #upd-migration-flash", js)
        self.assertIn("if (migrationBlocked)", js)

    def test_ssr_hides_version_flash_when_migration_blocked(self):
        html = open(
            "app/web/templates/_settings_update.html", encoding="utf-8"
        ).read()
        self.assertIn("migration_blocked or show_ops", html)
        self.assertIn('id="upd-version-flash"', html)
        self.assertIn('id="upd-migration-flash"', html)


if __name__ == "__main__":
    unittest.main()
