"""Reworded customer texts: untouched old copies follow the new wording, edited ones stay."""
from __future__ import annotations

import unittest

from app.services.receipt_review import DEFAULT_REJECT_TEXT
from app.services.retired_text_defaults import RETIRED_TEXT_DEFAULTS, refresh_retired_defaults
from app.services.users import DEFAULT_SETTINGS

REWORDED = ("payment_reject_text", "faq_text")


class ReworkedTextTests(unittest.TestCase):
    def test_an_untouched_old_copy_is_shown_with_the_new_wording(self) -> None:
        for key in REWORDED:
            stored = {key: RETIRED_TEXT_DEFAULTS[key][0]}
            refreshed = refresh_retired_defaults(stored, DEFAULT_SETTINGS)
            self.assertEqual(refreshed[key], DEFAULT_SETTINGS[key])
            self.assertNotEqual(refreshed[key], RETIRED_TEXT_DEFAULTS[key][0])

    def test_a_text_edited_by_the_shop_is_kept(self) -> None:
        stored = {key: f"custom {key}" for key in REWORDED}
        self.assertEqual(refresh_retired_defaults(dict(stored), DEFAULT_SETTINGS), stored)

    def test_every_rejection_path_uses_the_same_default(self) -> None:
        self.assertEqual(DEFAULT_REJECT_TEXT, DEFAULT_SETTINGS["payment_reject_text"])

    def test_new_defaults_are_not_listed_as_retired(self) -> None:
        for key in REWORDED:
            self.assertNotIn(DEFAULT_SETTINGS[key], RETIRED_TEXT_DEFAULTS[key])


if __name__ == "__main__":
    unittest.main()
