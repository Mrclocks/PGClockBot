from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "release_notes_for_tag.py"
spec = importlib.util.spec_from_file_location("release_notes_for_tag", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ReleaseNotesForTagTests(unittest.TestCase):
    def test_known_version_renders_one_bullet_per_note(self) -> None:
        text = module.render("1.0.0", {"1.0.0": ["a", "b"]})
        self.assertEqual(text, "- a\n- b")

    def test_unknown_version_falls_back_to_title(self) -> None:
        self.assertEqual(module.render("9.9.9", {}), "PGClockBot 9.9.9")

    def test_real_notes_file_loads_without_importing_the_app(self) -> None:
        self.assertTrue(module.load_notes(module.NOTES_FILE))


if __name__ == "__main__":
    unittest.main()
