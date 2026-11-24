"""Every guide page searches the one index at the guide root.

guide.js fetches ``search-index.json`` from the folder it was loaded from. Each page loads guide.js
from the guide root, so one index serves them all; the copy each page folder used to carry was never
read and added 4.7 MB to every install.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from posixpath import dirname, join, normpath

GUIDE = Path(__file__).resolve().parents[1] / "app" / "web" / "static" / "guide"
SCRIPT_SRC = re.compile(r'<script[^>]*\bsrc="([^"]*guide\.js)[^"]*"')


def _page_dirs() -> list[str]:
    return sorted(p.parent.relative_to(GUIDE).as_posix() for p in GUIDE.glob("**/index.html"))


class GuideSearchIndexTests(unittest.TestCase):
    def test_every_page_fetches_an_index_that_exists(self) -> None:
        for page in _page_dirs():
            with self.subTest(page=page):
                html = (GUIDE / page / "index.html").read_text(encoding="utf-8")
                (src,) = SCRIPT_SRC.findall(html)
                index = normpath(join(page, dirname(src), "search-index.json"))
                self.assertTrue((GUIDE / index).is_file(), index)

    def test_the_guide_carries_one_search_index(self) -> None:
        self.assertEqual([p.relative_to(GUIDE).as_posix() for p in GUIDE.glob("**/search-index.json")],
                         ["search-index.json"])

    def test_every_result_of_the_index_leads_to_a_page(self) -> None:
        items = json.loads((GUIDE / "search-index.json").read_text(encoding="utf-8"))
        self.assertTrue(items)
        for item in items:
            with self.subTest(item=item["id"]):
                self.assertTrue((GUIDE / item["href"]).is_file(), item["href"])


if __name__ == "__main__":
    unittest.main()
