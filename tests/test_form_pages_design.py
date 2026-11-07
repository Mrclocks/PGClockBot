"""Form pages (broadcast, action centre, message variables) render inside the design-language scope."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from tests.test_message_variables_entry import OWNER, _page


class FormPagesDesignTests(unittest.TestCase):
    def assert_scoped(self, html: str) -> None:
        self.assertIn('<div class="ds ds-settings">', html)
        inner = html.split('<div class="ds ds-settings">', 1)[1]
        self.assertIn("page-head", inner)

    def test_broadcast_page(self) -> None:
        self.assert_scoped(_page("broadcast.html", "/broadcast", OWNER, history=[], marketing_sections=()))

    def test_action_centre_page(self) -> None:
        self.assert_scoped(_page("inbox.html", "/inbox", OWNER, ac={"entries": []}, entries=[]))


if __name__ == "__main__":
    unittest.main()
