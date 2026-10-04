"""Release notes / changelog for the update page."""

from __future__ import annotations

import unittest
from pathlib import Path


class ReleaseNotesTests(unittest.TestCase):
    def test_current_version_has_single_block(self):
        from app.services.release_notes import changelog_for_update_page
        from app.version import __version__

        cl = changelog_for_update_page(local=__version__, remote=__version__)
        self.assertTrue(cl["has_notes"])
        self.assertEqual(len(cl["blocks"]), 1)
        self.assertTrue(cl["title"])

    def test_upgrade_shows_all_versions_in_range(self):
        from app.services.release_notes import changelog_for_update_page

        remote_notes = {
            "0.1.2": ["پچ ۲"],
            "0.1.1": ["پچ ۱"],
            "0.1.0": ["پایه"],
        }
        cl = changelog_for_update_page(
            local="0.1.0",
            remote="0.1.2",
            remote_notes=remote_notes,
        )
        self.assertTrue(cl["has_notes"])
        versions = [b["version"] for b in cl["blocks"]]
        self.assertEqual(versions, ["0.1.2", "0.1.1"])
        self.assertIn("از 0.1.0 تا 0.1.2", cl["title"])

    def test_upgrade_example_3_0_1_to_3_0_4(self):
        from app.services.release_notes import changelog_for_update_page

        remote_notes = {
            "3.0.4": ["هدف"],
            "3.0.3": ["میانی ۳"],
            "3.0.2": ["میانی ۲"],
            "3.0.1": ["نصب‌شده — نباید بیاید"],
        }
        cl = changelog_for_update_page(
            local="3.0.1",
            remote="3.0.4",
            remote_notes=remote_notes,
        )
        versions = [b["version"] for b in cl["blocks"]]
        self.assertEqual(versions, ["3.0.4", "3.0.3", "3.0.2"])
        self.assertNotIn("3.0.1", versions)

    def test_upgrade_uses_remote_notes_not_local_fallback(self):
        """Installed panel may not yet contain notes for the target version."""
        from app.services.release_notes import changelog_for_update_page

        remote_notes = {
            "1.9.0": ["ویژگی جدید از ریموت"],
            "1.8.3": ["نسخه قدیمی‌تر"],
        }
        cl = changelog_for_update_page(
            local="1.8.3",
            remote="1.9.0",
            remote_notes=remote_notes,
        )
        self.assertEqual(cl["blocks"][0]["version"], "1.9.0")
        self.assertIn("ویژگی جدید از ریموت", cl["blocks"][0]["notes"])

    def test_parse_release_notes_source(self):
        from app.services.release_notes import parse_release_notes_source

        src = Path("app/services/release_notes.py").read_text(encoding="utf-8")
        parsed = parse_release_notes_source(src)
        self.assertEqual(list(parsed.keys())[0], "0.1.7")
        self.assertIn("0.1.0", parsed)
        self.assertTrue(parsed["0.1.7"])
        self.assertTrue(any("نمایندگی" in n or "باشگاه" in n or "گردونه" in n for n in parsed["0.1.7"]))
        self.assertTrue(parsed["0.1.4"])
        self.assertTrue(any("مودال" in n or "گوشه" in n for n in parsed["0.1.4"]))
        self.assertTrue(parsed["0.1.3"])
        self.assertTrue(any("CSRF" in n or "ذخیره" in n or "SSL" in n for n in parsed["0.1.3"]))
        self.assertTrue(parsed["0.1.2"])
        self.assertTrue(parsed["0.1.1"])

    def test_unknown_local_falls_back_to_newest(self):
        from app.services.release_notes import changelog_for_update_page, RELEASE_NOTES_FA

        cl = changelog_for_update_page(local="9.9.9", remote=None)
        self.assertTrue(cl["has_notes"])
        self.assertEqual(len(cl["blocks"]), 1)
        newest = next(iter(RELEASE_NOTES_FA))
        self.assertEqual(cl["blocks"][0]["version"], newest)

    def test_update_template_simplified(self):
        src = Path("app/web/templates/_settings_update.html").read_text(encoding="utf-8")
        self.assertIn("update-changelog", src)
        self.assertIn("rollback-version", src)
        self.assertIn("settings-card", src)
        self.assertIn("upd-start", src)
        self.assertNotIn("update-details", src)
        self.assertNotIn("جزئیات عملیات", src)
        self.assertNotIn("upd-log", src)
        self.assertNotIn("upd-steps", src)
        self.assertNotIn("روش جایگزین", src)
        self.assertNotIn("get.sh", src)

    def test_parse_includes_latest_notes(self):
        from app.services.release_notes import parse_release_notes_source

        src = Path("app/services/release_notes.py").read_text(encoding="utf-8")
        parsed = parse_release_notes_source(src)
        self.assertEqual(
            set(parsed), {"0.1.7", "0.1.6", "0.1.5", "0.1.4", "0.1.3", "0.1.2", "0.1.1", "0.1.0"}
        )
        self.assertTrue(any("نمایندگی" in n or "باشگاه" in n or "گردونه" in n for n in parsed["0.1.7"]))
        self.assertTrue(any("کیف پول" in n or "امنیت" in n for n in parsed["0.1.2"]))
        self.assertTrue(any("راه‌اندازی" in n or "۰٫۱٫۱" in n for n in parsed["0.1.1"]))

    def test_home_no_manual_refresh(self):
        src = Path("app/web/templates/home.html").read_text(encoding="utf-8")
        self.assertNotIn(">بروزرسانی</a>", src)

    def test_pg_home_no_update_button(self):
        src = Path("app/web/templates/pg_home.html").read_text(encoding="utf-8")
        self.assertNotIn(">بروزرسانی</a>", src)
        self.assertNotIn('href="/pg">بروزرسانی', src)

    def test_pg_home_stats_uses_home_panel(self):
        src = Path("app/web/templates/_pg_dash_body.html").read_text(encoding="utf-8")
        self.assertIn("home-panel home-panel-pg", src)
        self.assertIn("home-panel-grid", src)
        self.assertIn("آمار پنل", src)

    def test_sidebar_web_panel_label(self):
        src = Path("app/web/templates/base.html").read_text(encoding="utf-8")
        self.assertIn("وب پنل", src)
        self.assertIn("nav-label-home", src)

    def test_update_context_fetches_remote_notes(self):
        src = Path("app/services/panel_update.py").read_text(encoding="utf-8")
        self.assertIn("fetch_remote_release_notes", src)
        self.assertIn("remote_notes=", src)


if __name__ == "__main__":
    unittest.main()
