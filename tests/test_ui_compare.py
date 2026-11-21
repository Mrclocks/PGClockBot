"""The panel comparison reports real changes and ignores rendering noise nobody can see."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from scripts.ui_compare import PIXEL_TOLERANCE, compare_images, compare_inventory

VIEW = {"status": 200, "controls": ["button | ذخیره تنظیمات عمومی | submit | "],
        "forms": [{"method": "post", "action": "/settings/general", "fields": ["s_site_title"]}]}


class _Captures(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.before, self.after = Path(self.tmp.name, "before"), Path(self.tmp.name, "after")
        for side in (self.before, self.after):
            (side / "png").mkdir(parents=True)

    def shot(self, side: Path, color: tuple[int, int, int], size: tuple[int, int] = (40, 30), dot=None) -> None:
        image = Image.new("RGB", size, color)
        if dot:
            image.putpixel(dot[0], dot[1])
        image.save(side / "png" / "page@390-light.png")

    def views(self, side: Path, view: dict | None) -> None:
        (side / "inventory.json").write_text(json.dumps({"390": {"/settings?tab=general": view} if view else {}}))


class ImageTests(_Captures):
    def test_identical_screenshots_pass(self) -> None:
        self.shot(self.before, (250, 250, 250))
        self.shot(self.after, (250, 250, 250))
        self.assertEqual(compare_images(self.before, self.after, self.after / "diff"), [])

    def test_dithering_within_the_tolerance_is_ignored(self) -> None:
        self.shot(self.before, (250, 250, 250))
        self.shot(self.after, (250 - PIXEL_TOLERANCE, 250, 250))
        self.assertEqual(compare_images(self.before, self.after, self.after / "diff"), [])

    def test_one_changed_pixel_and_a_changed_size_are_reported(self) -> None:
        self.shot(self.before, (250, 250, 250))
        self.shot(self.after, (250, 250, 250), dot=((3, 4), (20, 20, 20)))
        (problem,) = compare_images(self.before, self.after, self.after / "diff")
        self.assertIn("1 px", problem)
        self.assertTrue((self.after / "diff" / "page@390-light.png").exists())
        self.shot(self.after, (250, 250, 250), size=(40, 31))
        self.assertIn("size changed", compare_images(self.before, self.after, self.after / "diff")[0])

    def test_a_missing_screenshot_is_reported(self) -> None:
        self.shot(self.before, (250, 250, 250))
        self.assertIn("missing after", compare_images(self.before, self.after, self.after / "diff")[0])


class InventoryTests(_Captures):
    def test_same_controls_pass(self) -> None:
        self.views(self.before, VIEW)
        self.views(self.after, VIEW)
        self.assertEqual(compare_inventory(self.before, self.after), [])

    def test_a_renamed_button_a_lost_field_and_a_missing_view_are_reported(self) -> None:
        self.views(self.before, VIEW)
        for changed in (
            {**VIEW, "controls": ["button | ذخیرهٔ تنظیمات عمومی | submit | "]},
            {**VIEW, "forms": [{"method": "post", "action": "/settings/general", "fields": []}]},
            None,
        ):
            with self.subTest(changed=changed):
                self.views(self.after, changed)
                self.assertTrue(compare_inventory(self.before, self.after))


if __name__ == "__main__":
    unittest.main()
