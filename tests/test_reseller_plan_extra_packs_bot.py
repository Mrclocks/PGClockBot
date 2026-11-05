"""Bot callback parsing for per-plan extra pack purchase confirmations."""

from __future__ import annotations

import unittest

from app.bot.handlers.reseller_capacity_buy import GB_OK, USERS_OK, _ok_uses_pack_id, _parse_ok


class ExtraPackOkParseTests(unittest.TestCase):
    def test_legacy_amount_generation(self) -> None:
        self.assertEqual(_parse_ok(f"{GB_OK}10:3", GB_OK), (10, 3))
        self.assertFalse(_ok_uses_pack_id(f"{GB_OK}10:3", GB_OK))

    def test_pack_id_generation(self) -> None:
        self.assertEqual(_parse_ok(f"{USERS_OK}id:42:7", USERS_OK), (42, 7))
        self.assertTrue(_ok_uses_pack_id(f"{USERS_OK}id:42:7", USERS_OK))

    def test_rejects_malformed(self) -> None:
        self.assertIsNone(_parse_ok(f"{GB_OK}id:x:1", GB_OK))
        self.assertIsNone(_parse_ok(f"{GB_OK}0:1", GB_OK))


if __name__ == "__main__":
    unittest.main()
