"""Regression tests for 2.7.0 broadcast / upload contrast / danger deletes."""

from __future__ import annotations

import unittest
from pathlib import Path


class BroadcastDirectDeleteTests(unittest.TestCase):
    def test_no_kebab_direct_delete_and_red_clear(self):
        html = Path("app/web/templates/broadcast.html").read_text(encoding="utf-8")
        self.assertNotIn("row_actions", html)
        self.assertIn('class="btn btn-danger btn-sm" type="submit">حذف تاریخچه<', html)
        self.assertIn('action="/broadcast/history/{{ row.id }}/delete"', html)
        self.assertIn('class="btn btn-danger btn-sm" type="submit">حذف<', html)


class UploadContrastTests(unittest.TestCase):
    def test_large_body_small_contrast(self):
        css = Path("app/web/static/panel.css").read_text(encoding="utf-8")
        # Large chrome around image uploads matches body
        block = css.split(".image-setting {\n  display: flex; flex-direction: column; gap: 8px;", 1)[1]
        block = block.split("}", 1)[0]
        self.assertIn("background: var(--background);", block)
        self.assertNotIn("background: var(--muted);", block)
        # Inner dashed box (inside image-setting) gets a mild contrast mix
        self.assertIn(".image-setting .upload-box,", css)
        self.assertIn(
            "background: color-mix(in srgb, var(--muted) 42%, var(--background));",
            css,
        )


class UploadDeleteDangerTests(unittest.TestCase):
    def test_clear_buttons_are_danger(self):
        field = Path("app/web/templates/_settings_field.html").read_text(encoding="utf-8")
        self.assertIn("حذف فایل آپلودشده", field)
        self.assertIn("btn-danger", field)
        self.assertNotIn("حذف عکس فعلی", field)
        ap = Path("app/web/templates/_settings_appearance.html").read_text(encoding="utf-8")
        self.assertIn("btn-danger", ap)
        self.assertIn("حذف فایل آپلودشده", ap)
        pwa = Path("app/web/templates/_settings_pwa.html").read_text(encoding="utf-8")
        self.assertIn("btn-danger", pwa)
        self.assertIn("حذف فایل آپلودشده", pwa)


if __name__ == "__main__":
    unittest.main()
