"""3.3.2 — full panel + bot audit: dead code gone, hot-path skips, lighter client."""

from __future__ import annotations

import unittest


class UnreadSkipTests(unittest.TestCase):
    def test_should_skip_helper(self):
        from app.services.panel_tickets import should_skip_unread_count

        self.assertTrue(should_skip_unread_count("/home/metrics", "GET"))
        self.assertTrue(should_skip_unread_count("/tickets", "GET"))
        self.assertTrue(should_skip_unread_count("/pg/users/12/link", "GET"))
        self.assertTrue(should_skip_unread_count("/home", "POST"))
        self.assertFalse(should_skip_unread_count("/home", "GET"))


if __name__ == "__main__":
    unittest.main()
