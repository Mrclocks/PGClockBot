"""Panel usernames never carry two separators in a row, which PasarGuard refuses with HTTP 422."""

from __future__ import annotations

import random
import unittest

from app.services.credential_policy import validate_pg_username
from app.services.orders import PG_USERNAME_MAX_LENGTH, _fit_panel_username, _random_username


def assert_panel_accepts(case: unittest.TestCase, name: str) -> None:
    _normalized, error = validate_pg_username(name)
    case.assertIsNone(error, name)
    case.assertNotIn("__", name)
    case.assertLessEqual(len(name), PG_USERNAME_MAX_LENGTH)


class SeparatorTests(unittest.TestCase):
    def test_a_telegram_username_with_underscores_next_to_the_pattern_separator(self) -> None:
        name = _random_username("shop", pattern="{prefix}_{username}_{id}", user_id=12, username="_ali__x_")
        self.assertEqual(name, "shop_ali_x_12")
        assert_panel_accepts(self, name)

    def test_replaced_characters_next_to_a_separator(self) -> None:
        name = _random_username("vpn-", suffix=".x", pattern="{prefix}_{random}_{suffix}", chosen_name="my__name")
        self.assertEqual(name, "vpn_my_name_x")
        assert_panel_accepts(self, name)

    def test_cutting_a_long_name_does_not_join_two_separators(self) -> None:
        built = "a" * 23 + "_" + "_" + "b" * 7 + "_" + "c" * 8
        assert_panel_accepts(self, _fit_panel_username(built, keep_unique=False))

    def test_any_pattern_output_is_accepted_by_the_panel(self) -> None:
        rng = random.Random(1499)  # fixed seed: a reproducible sample, nothing secret
        alphabet = "ab12_-. @"
        for _ in range(2000):
            built = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 60)))
            assert_panel_accepts(self, _fit_panel_username(built, keep_unique=rng.random() < 0.5))


if __name__ == "__main__":
    unittest.main()
