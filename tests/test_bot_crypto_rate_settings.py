"""Crypto conversion settings in the owner and shop bots, validated like their readers read them.

The API address must be public https (no internal host), the asset, rate, margin and JSON path
must have the shape the rate job accepts, and a shop changes only its own copy.
"""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.bot.handlers import admin_settings, reseller_settings
from app.services.bot_setting_values import NUMBER_BOUNDS, SELECT_OPTIONS
from app.services.crypto_rates import config_from_settings, normalize_rate_setting

KEYS = (
    "crypto_rate_mode",
    "crypto_rate_asset",
    "crypto_rate_manual",
    "crypto_rate_api_url",
    "crypto_rate_api_path",
    "crypto_rate_api_divisor",
    "crypto_rate_margin_percent",
    "crypto_quote_minutes",
)
SHOP_ID = 31


class NormalizeTests(unittest.TestCase):
    def test_valid_values_are_stored_in_the_form_the_reader_uses(self) -> None:
        stored = {
            "crypto_rate_mode": "api",
            "crypto_rate_asset": normalize_rate_setting("crypto_rate_asset", " usdt "),
            "crypto_rate_manual": normalize_rate_setting("crypto_rate_manual", "۹۸۵۰۰"),
            "crypto_rate_margin_percent": normalize_rate_setting("crypto_rate_margin_percent", "1.5"),
            "crypto_rate_api_url": normalize_rate_setting("crypto_rate_api_url", "https://api.example.com/rate"),
            "crypto_rate_api_path": normalize_rate_setting("crypto_rate_api_path", "data.0.price"),
        }
        config = config_from_settings(stored)
        self.assertIsNotNone(config)
        self.assertEqual(config.asset, "USDT")
        self.assertEqual(str(config.manual_rate), "98500")
        self.assertEqual(config.margin_bps, 150)
        self.assertEqual(config.api_path, "data.0.price")

    def test_hostile_values_are_refused(self) -> None:
        cases = {
            "crypto_rate_api_url": (
                "http://api.example.com", "https://127.0.0.1/x", "https://10.0.0.2/x", "https://user:pw@example.com/",
                "https://example.com:8443/", "javascript:alert(1)", "",
            ),
            "crypto_rate_asset": ("U", "US DT", "<b>", "A" * 17),
            "crypto_rate_manual": ("nan", "-5", "0", "1e5", "1.2.3", "99999999999999"),
            "crypto_rate_margin_percent": ("21", "-1", "nan", "abc"),
            "crypto_rate_api_path": ("a/b", "a..b", ".a", "a." + ".".join(["x"] * 9), "<script>"),
        }
        for key, values in cases.items():
            for raw in values:
                with self.subTest(key=key, raw=raw), self.assertRaises(ValueError):
                    normalize_rate_setting(key, raw)

    def test_thousand_separators_are_accepted_in_the_rate(self) -> None:
        self.assertEqual(normalize_rate_setting("crypto_rate_manual", "۹۸٬۵۰۰"), "98500")
        self.assertEqual(normalize_rate_setting("crypto_rate_manual", "98,500"), "98500")

    def test_optional_texts_are_cleared_with_a_dash(self) -> None:
        for key in ("crypto_rate_manual", "crypto_rate_api_url", "crypto_rate_api_path"):
            self.assertEqual(normalize_rate_setting(key, "-"), "")
        with self.assertRaises(ValueError):
            normalize_rate_setting("crypto_rate_asset", "-")


class FieldTests(unittest.TestCase):
    def test_both_bots_offer_every_conversion_setting(self) -> None:
        for fields in (admin_settings.FIELDS, reseller_settings.FIELDS):
            for key in KEYS:
                self.assertIn(key, fields)

    def test_choices_and_bounds_match_the_reader(self) -> None:
        self.assertEqual([value for value, _ in SELECT_OPTIONS["crypto_rate_mode"]], ["off", "manual", "api"])
        self.assertEqual(NUMBER_BOUNDS["crypto_quote_minutes"], (1, 120))


class ShopSaveTests(unittest.IsolatedAsyncioTestCase):
    async def save(self, key: str, text: str) -> tuple[AsyncMock, MagicMock]:
        state = AsyncMock(get_data=AsyncMock(return_value={"edit_key": key}))
        message = MagicMock(text=text, entities=None, answer=AsyncMock(), delete=AsyncMock())
        with (
            patch.object(reseller_settings, "_gate", AsyncMock(return_value=(SimpleNamespace(user_id=SHOP_ID), None))),
            patch.object(reseller_settings, "set_setting", AsyncMock()) as saved,
            patch.object(reseller_settings, "_close_input_to_location", AsyncMock()),
        ):
            await reseller_settings.settings_edit_save(message, state, MagicMock(), SimpleNamespace(id=1))
        return saved, message

    async def test_valid_url_is_saved_for_the_shop(self) -> None:
        saved, _ = await self.save("crypto_rate_api_url", "https://api.example.com/rate")
        self.assertEqual(saved.await_args.args[1:], ("crypto_rate_api_url", "https://api.example.com/rate"))
        self.assertEqual(saved.await_args.kwargs["reseller_id"], SHOP_ID)

    async def test_internal_url_is_refused_with_a_persian_message(self) -> None:
        saved, message = await self.save("crypto_rate_api_url", "https://192.168.1.1/rate")
        saved.assert_not_awaited()
        self.assertIn("داخلی", message.answer.await_args.args[0])

    async def test_bad_number_is_refused(self) -> None:
        saved, _ = await self.save("crypto_quote_minutes", "500")
        saved.assert_not_awaited()


class OwnerSaveTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_asset_is_saved_upper_case(self) -> None:
        actor = patch.object(admin_settings, "_ensure_settings_actor", AsyncMock(return_value=True))
        actor.start()
        self.addCleanup(actor.stop)
        state = AsyncMock(get_data=AsyncMock(return_value={"edit_key": "crypto_rate_asset"}))
        message = MagicMock(text="trx", entities=None, answer=AsyncMock(), delete=AsyncMock())
        with (
            patch.object(admin_settings, "_is_admin", return_value=True),
            patch.object(admin_settings, "set_setting", AsyncMock()) as saved,
            patch.object(admin_settings, "_finish_settings_text_edit", AsyncMock()),
        ):
            await admin_settings.settings_edit_save(message, state, MagicMock(), SimpleNamespace(id=1))
        self.assertEqual(saved.await_args.args[1:], ("crypto_rate_asset", "TRX"))


if __name__ == "__main__":
    unittest.main()
