"""The phone header shows how many action-center items are waiting, read once and cached briefly."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.services import panel_inbox
from app.services.panel_sidebar_cache import invalidate_sidebar_counts, peek_sidebar_counts, store_sidebar_counts

STAFF = {"role": "reseller", "bot_user_id": 7}


class SidebarCacheCountTests(unittest.TestCase):
    def tearDown(self) -> None:
        invalidate_sidebar_counts()

    def test_cache_keeps_the_inbox_count_not_just_a_flag(self) -> None:
        store_sidebar_counts("reseller:7", 2, 5, True)
        self.assertEqual(peek_sidebar_counts("reseller:7"), (2, 5, True))

    def test_a_negative_count_is_stored_as_zero(self) -> None:
        store_sidebar_counts("reseller:7", 0, -3)
        self.assertEqual(peek_sidebar_counts("reseller:7"), (0, 0, False))


class SidebarInboxCountTests(unittest.TestCase):
    def setUp(self) -> None:
        panel_inbox.invalidate_inbox_sidebar_cache()

    def tearDown(self) -> None:
        panel_inbox.invalidate_inbox_sidebar_cache()

    def count(self, built: AsyncMock) -> int:
        with patch.object(panel_inbox, "build_inbox_context", built):
            return asyncio.run(panel_inbox.sidebar_inbox_count(None, None, STAFF))

    def test_returns_the_visible_item_count_and_reuses_it(self) -> None:
        built = AsyncMock(return_value={"inbox_count": 4})
        self.assertEqual(self.count(built), 4)
        self.assertEqual(self.count(built), 4)
        built.assert_awaited_once()

    def test_a_failed_build_shows_no_count(self) -> None:
        self.assertEqual(self.count(AsyncMock(side_effect=RuntimeError("db down"))), 0)


if __name__ == "__main__":
    unittest.main()
