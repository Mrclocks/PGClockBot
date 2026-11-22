"""The committed guide under app/web/static/guide is exactly what scripts/build_guide.py builds today.

The guide is built from docs/guide and committed so installs need no build tools. When a page's Markdown
changes and the build is not re-run, /help keeps telling customers about a panel that no longer exists.
"""

from __future__ import annotations

import filecmp
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import build_guide

COMMITTED = Path(build_guide.OUT)


def _files(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


class GuideBuildIsCurrentTests(unittest.TestCase):
    def test_the_committed_guide_matches_a_fresh_build(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            fresh = Path(folder) / "guide"
            with patch.object(build_guide, "OUT", fresh):
                self.assertEqual(build_guide.main(), 0)
            self.assertEqual(_files(fresh), _files(COMMITTED), "pages added or removed: run scripts/build_guide.py")
            stale = [name for name in sorted(_files(fresh))
                     if not filecmp.cmp(fresh / name, COMMITTED / name, shallow=False)]
            self.assertEqual(stale, [], "stale guide pages: run scripts/build_guide.py and commit the result")
