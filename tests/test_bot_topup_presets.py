"""Preset wallet top-up amounts, typed in the owner or shop bot, stay inside the top-up limits."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


from app.bot.handlers import admin_settings, reseller_settings
from app.services.orders import MAX_TOPUP_AMOUNT, MIN_TOPUP_AMOUNT
from app.services.wallet_topup import (
    DEFAULT_TOPUP_PRESETS,
    MAX_TOPUP_PRESETS,
    normalize_topup_presets,
    parse_topup_presets,
)

KEY = "wallet_topup_presets"
SHOP_ID = 41


class NormalizeTests(unittest.TestCase):
    def test_amounts_are_stored_as_the_csv_the_keyboard_reads(self) -> None:
        stored = normalize_topup_presets("۵۰٬۰۰۰، 100000\n200000 100000")
        self.assertEqual(stored, "50000,100000,200000")
        self.assertEqual(parse_topup_presets({KEY: stored}), [50000, 100000, 200000])

    def test_default_words_keep_the_built_in_amounts(self) -> None:
        for raw in ("پیش‌فرض", "-", ""):
            stored = normalize_topup_presets(raw)
            self.assertEqual(stored, "")
            self.assertEqual(parse_topup_presets({KEY: stored}), list(DEFAULT_TOPUP_PRESETS))

    def test_out_of_range_or_malformed_amounts_are_refused(self) -> None:
        too_many = ",".join(str(MIN_TOPUP_AMOUNT * (i + 1)) for i in range(MAX_TOPUP_PRESETS + 1))
        for raw in (str(MIN_TOPUP_AMOUNT - 1), str(MAX_TOPUP_AMOUNT + 1), "-5000", "1e5", "50000.5", "abc", too_many):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                normalize_topup_presets(raw)


class BotSaveTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_bots_offer_the_presets(self) -> None:
        self.assertIn(KEY, admin_settings.FIELDS)
        self.assertIn(KEY, reseller_settings.FIELDS)

    async def test_shop_save_is_normalised_and_scoped(self) -> None:
        state = AsyncMock(get_data=AsyncMock(return_value={"edit_key": KEY}))
        message = MagicMock(text="20000, 30000", entities=None, answer=AsyncMock(), delete=AsyncMock())
        with (
            patch.object(reseller_settings, "_gate", AsyncMock(return_value=(SimpleNamespace(user_id=SHOP_ID), None))),
            patch.object(reseller_settings, "set_setting", AsyncMock()) as saved,
            patch.object(reseller_settings, "_close_input_to_location", AsyncMock()),
        ):
            await reseller_settings.settings_edit_save(message, state, MagicMock(), SimpleNamespace(id=1))
        self.assertEqual(saved.await_args.args[1:], (KEY, "20000,30000"))
        self.assertEqual(saved.await_args.kwargs["reseller_id"], SHOP_ID)

    async def test_owner_save_refuses_a_bad_amount(self) -> None:
        actor = patch.object(admin_settings, "_ensure_settings_actor", AsyncMock(return_value=True))
        actor.start()
        self.addCleanup(actor.stop)
        state = AsyncMock(get_data=AsyncMock(return_value={"edit_key": KEY}))
        message = MagicMock(text="10", entities=None, answer=AsyncMock(), delete=AsyncMock())
        with (
            patch.object(admin_settings, "_is_admin", return_value=True),
            patch.object(admin_settings, "set_setting", AsyncMock()) as saved,
            patch.object(admin_settings, "_finish_settings_text_edit", AsyncMock()),
        ):
            await admin_settings.settings_edit_save(message, state, MagicMock(), SimpleNamespace(id=1))
        saved.assert_not_awaited()
        message.answer.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
