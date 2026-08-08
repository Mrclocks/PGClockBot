"""Update tab polish + title gap parity + footer baseline (3.1.13)."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "app/web/static/panel.css"
UPD = ROOT / "app/web/templates/_settings_update.html"


class UpdateTabUiTests(unittest.TestCase):
    def test_changelog_is_inside_update_card_under_flash(self):
        src = UPD.read_text(encoding="utf-8")
        flash_at = src.find("نسخه جدید آماده است")
        cl_at = src.find('class="update-changelog"')
        rollback_at = src.find("راه برگشت")
        # changelog block appears after the update-available flash copy
        self.assertGreater(flash_at, 0)
        self.assertGreater(cl_at, flash_at)
        self.assertGreater(rollback_at, cl_at)
        self.assertIn("update-changelog-title", src)
        self.assertNotIn('class="card settings-card update-changelog"', src)

    def test_version_boxes_use_ok_and_err_states(self):
        src = UPD.read_text(encoding="utf-8")
        self.assertIn("update-meta-ok", src)
        self.assertIn("update-meta-err", src)
        self.assertNotIn("update-meta-warn", src)
        # current: danger/err when update available, else ok
        self.assertIn(
            'class="update-meta-box {% if available %}update-meta-err{% else %}update-meta-ok{% endif %}"',
            src,
        )
        # latest: ok when update available
        self.assertIn(
            'class="update-meta-box {% if available %}update-meta-ok{% endif %}"',
            src,
        )
        css = CSS.read_text(encoding="utf-8")
        self.assertIn(".update-meta-box.update-meta-ok", css)
        self.assertIn(".update-meta-box.update-meta-err", css)
        err = css.split(".update-meta-box.update-meta-err {", 1)[1].split("}", 1)[0]
        self.assertIn("239, 68, 68", err)
        self.assertNotIn("234, 179, 8", err)

    def test_rollback_select_is_rtl_right_aligned(self):
        src = UPD.read_text(encoding="utf-8")
        # no forced LTR on the select — value sits opposite the left chevron
        sel = src.split('id="rollback-version"', 1)[1][:48]
        self.assertNotIn('dir="ltr"', sel)
        css = CSS.read_text(encoding="utf-8")
        field = css.split(".rollback-field select {", 1)[1].split("}", 1)[0]
        self.assertIn("direction: rtl", field)
        self.assertIn("text-align: right", field)
        self.assertIn("padding-left: var(--space-4)", field)
        self.assertIn("background-position: left var(--space-2) center", field)


class TitleGapParityTests(unittest.TestCase):
    def test_bot_title_above_and_below_use_same_token(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertIn(
            "padding: var(--page-title-gap) var(--space-4) 0;",
            css,
        )
        head = css.split(".page-head {\n", 1)[1].split("}", 1)[0]
        self.assertIn("margin-bottom: var(--page-title-gap);", head)
        # PG pages share this same .page-head token (no separate inflated stack)
        self.assertNotIn(
            "margin-top: calc(-1 * (var(--btn-h) + var(--space-1)));",
            css,
        )
        foot = css.split(".site-footer {\n", 1)[1].split("}", 1)[0]
        self.assertIn("padding-bottom: calc(var(--page-title-gap) + var(--safe-bottom));", foot)


class FooterBaselineTests(unittest.TestCase):
    def test_site_footer_matches_side_foot_min_height(self):
        css = CSS.read_text(encoding="utf-8")
        site = css.split(".site-footer {\n", 1)[1].split("}", 1)[0]
        side = css.split(".side-foot {\n", 1)[1].split("}", 1)[0]
        shared = "min-height: calc(var(--page-title-gap) + 28px + var(--page-title-gap) + var(--safe-bottom));"
        self.assertIn(shared, site)
        self.assertIn(shared, side)
        self.assertIn("padding-top: var(--page-title-gap);", site)
        self.assertIn("padding-top: var(--page-title-gap);", side)
        self.assertIn("padding-bottom: calc(var(--page-title-gap) + var(--safe-bottom));", site)
        self.assertIn("padding-bottom: calc(var(--page-title-gap) + var(--safe-bottom));", side)
        logout = css.split(".logout-link {\n", 1)[1].split("}", 1)[0]
        self.assertIn("height: 28px;", logout)
        self.assertIn("min-height: 28px;", logout)

    def test_mobile_main_footer_pad_matches_side_foot(self):
        css = CSS.read_text(encoding="utf-8")
        mobile = css.split("@media (max-width: 900px)", 1)[1]
        self.assertIn("padding: var(--page-title-gap) var(--space-2) 0;", mobile)
        self.assertIn(
            "padding-bottom: calc(var(--page-title-gap) + var(--safe-bottom));",
            mobile,
        )
        self.assertIn("overflow: hidden;", mobile.split(".main {", 1)[1].split("}", 1)[0])

    def test_main_body_footer_gap_matches_page_title_gap(self):
        css = CSS.read_text(encoding="utf-8")
        body = css.split(".main-body {\n", 1)[1].split("}", 1)[0]
        self.assertIn("padding-bottom: var(--page-title-gap);", body)


if __name__ == "__main__":
    unittest.main()
