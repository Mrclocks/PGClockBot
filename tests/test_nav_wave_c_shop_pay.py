"""Wave C: wallet top-up presets, shop/pay chrome removal, hub polish."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch


class TopupPresetParseTests(unittest.TestCase):
    def test_defaults_when_empty(self):
        from app.bot.nav_inline import DEFAULT_TOPUP_PRESETS, parse_topup_presets

        self.assertEqual(parse_topup_presets({}), list(DEFAULT_TOPUP_PRESETS))
        self.assertEqual(parse_topup_presets(None), list(DEFAULT_TOPUP_PRESETS))

    def test_csv_and_persian_separators(self):
        from app.bot.nav_inline import parse_topup_presets

        self.assertEqual(
            parse_topup_presets({"wallet_topup_presets": "25000،50000, 100000"}),
            [25_000, 50_000, 100_000],
        )

    def test_rejects_below_minimum(self):
        from app.bot.nav_inline import DEFAULT_TOPUP_PRESETS, parse_topup_presets

        self.assertEqual(
            parse_topup_presets({"wallet_topup_presets": "500,999"}),
            list(DEFAULT_TOPUP_PRESETS),
        )

    def test_default_settings_key(self):
        from app.services.users import DEFAULT_SETTINGS

        self.assertIn("wallet_topup_presets", DEFAULT_SETTINGS)
        self.assertEqual(DEFAULT_SETTINGS.get("nav_mode"), "inline")


class WalletTopupPresetsKeyboardTests(unittest.TestCase):
    def test_callbacks_and_custom(self):
        from app.bot.nav_inline import wallet_topup_presets_keyboard

        kb = wallet_topup_presets_keyboard(
            {"wallet_topup_presets": "50000,100000"}
        )
        data = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertIn("nv:w:amt:50000", data)
        self.assertIn("nv:w:amt:100000", data)
        self.assertIn("nv:w:amt:custom", data)
        self.assertIn("nv:w:home", data)
        self.assertTrue(all(d.startswith("nv:w:") for d in data))


class TopupMethodsKeyboardTests(unittest.TestCase):
    def test_reuses_wtop_callbacks(self):
        from app.bot.nav_inline import topup_methods_keyboard

        ui = {
            "pay_card_enabled": "1",
            "pay_gateway_enabled": "1",
            "pay_psp_enabled": "0",
            "pay_crypto_enabled": "0",
            "btn_pay_card": "کارت",
            "btn_pay_gateway": "درگاه",
        }
        kb = topup_methods_keyboard(ui)
        data = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertIn("wtop:card", data)
        self.assertIn("wtop:gateway", data)
        self.assertIn("nv:w:topup", data)
        self.assertNotIn("wtop:psp", data)


class PresentOrderPayInlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_inline_uses_pay_methods_no_reply_chrome(self):
        from app.bot.menu_nav import present_order_pay

        message = AsyncMock()
        message.answer = AsyncMock(return_value=MagicMock())
        session = AsyncMock()
        db_user = MagicMock()
        state = AsyncMock()
        state.update_data = AsyncMock()
        pay_kb = MagicMock()
        pay_kb.inline_keyboard = [[]]

        with (
            patch(
                "app.bot.menu_nav.get_all_settings",
                AsyncMock(return_value={"nav_mode": "inline", "pay_wallet_enabled": "1"}),
            ),
            patch(
                "app.bot.menu_nav.set_nav_level",
                new_callable=AsyncMock,
            ),
            patch(
                "app.bot.menu_nav.kb.pay_methods",
                return_value=pay_kb,
            ) as pay_methods,
            patch(
                "app.bot.menu_nav.show_nav_keyboard",
                new_callable=AsyncMock,
            ) as show_nav,
        ):
            await present_order_pay(
                message,
                session,
                db_user,
                99,
                state=state,
                text="💳 روش پرداخت را از کیبورد پایین انتخاب کنید:",
            )

        message.answer.assert_awaited()
        self.assertEqual(message.answer.await_count, 1)
        args, kwargs = message.answer.await_args
        self.assertNotIn("کیبورد پایین", args[0])
        self.assertIs(kwargs.get("reply_markup"), pay_kb)
        pay_methods.assert_called_once_with(99, unittest.mock.ANY)
        show_nav.assert_not_awaited()

    async def test_classic_still_uses_show_nav(self):
        from app.bot.menu_nav import present_order_pay

        message = AsyncMock()
        session = AsyncMock()
        db_user = MagicMock()
        state = AsyncMock()

        with (
            patch(
                "app.bot.menu_nav.get_all_settings",
                AsyncMock(return_value={"nav_mode": "classic"}),
            ),
            patch(
                "app.bot.menu_nav.set_nav_level",
                new_callable=AsyncMock,
            ),
            patch(
                "app.bot.menu_nav.show_nav_keyboard",
                new_callable=AsyncMock,
            ) as show_nav,
        ):
            await present_order_pay(
                message, session, db_user, 7, state=state
            )

        show_nav.assert_awaited()


class OpenWalletTopupPresetTests(unittest.IsolatedAsyncioTestCase):
    async def test_inline_shows_presets_not_free_text(self):
        from app.bot.handlers.reply_nav import open_wallet_topup

        message = AsyncMock()
        message.answer = AsyncMock(return_value=MagicMock())
        session = AsyncMock()
        state = AsyncMock()
        ui = {
            "nav_mode": "inline",
            "pay_card_enabled": "1",
            "wallet_topup_presets": "50000,100000",
        }

        with patch(
            "app.bot.handlers.reply_nav.get_all_settings",
            AsyncMock(return_value=ui),
        ):
            await open_wallet_topup(message, session, state)

        message.answer.assert_awaited()
        self.assertEqual(message.answer.await_count, 1)
        markup = message.answer.await_args.kwargs.get("reply_markup")
        self.assertTrue(hasattr(markup, "inline_keyboard"))
        data = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("nv:w:amt:50000", data)
        self.assertIn("nv:w:amt:custom", data)

    async def test_classic_still_prompts_amount(self):
        from app.bot.handlers.reply_nav import open_wallet_topup

        message = AsyncMock()
        message.answer = AsyncMock(return_value=MagicMock())
        session = AsyncMock()
        state = AsyncMock()
        ui = {
            "nav_mode": "classic",
            "pay_card_enabled": "1",
        }

        with (
            patch(
                "app.bot.handlers.reply_nav.get_all_settings",
                AsyncMock(return_value=ui),
            ),
            patch(
                "app.bot.handlers.reply_nav.kb.cancel_reply",
                return_value="CANCEL",
            ),
        ):
            await open_wallet_topup(message, session, state)

        self.assertEqual(
            message.answer.await_args.kwargs.get("reply_markup"),
            "CANCEL",
        )


class NvWalletAmountTests(unittest.IsolatedAsyncioTestCase):
    async def test_preset_routes_to_methods(self):
        from app.bot.handlers.nav_hubs import nv_wallet_amount

        callback = AsyncMock()
        callback.data = "nv:w:amt:50000"
        callback.message = AsyncMock()
        callback.answer = AsyncMock()
        session = AsyncMock()
        db_user = MagicMock()
        state = AsyncMock()

        with (
            patch(
                "app.bot.handlers.nav_hubs.get_all_settings",
                AsyncMock(return_value={"nav_mode": "inline"}),
            ),
            patch(
                "app.bot.handlers.wallet.present_topup_methods",
                new_callable=AsyncMock,
            ) as present,
        ):
            await nv_wallet_amount(callback, session, db_user, state)

        present.assert_awaited()
        self.assertEqual(present.await_args.args[4], 50_000)

    async def test_custom_prompts_with_cancel_reply(self):
        from app.bot.handlers.nav_hubs import nv_wallet_amount

        callback = AsyncMock()
        callback.data = "nv:w:amt:custom"
        callback.message = AsyncMock()
        callback.message.answer = AsyncMock()
        callback.answer = AsyncMock()
        session = AsyncMock()
        db_user = MagicMock()
        state = AsyncMock()

        with (
            patch(
                "app.bot.handlers.nav_hubs.get_all_settings",
                AsyncMock(return_value={"nav_mode": "inline"}),
            ),
            patch(
                "app.bot.keyboards.cancel_reply",
                return_value="CANCEL",
            ),
        ):
            # cancel_reply imported as kb.cancel_reply inside handler
            with patch(
                "app.bot.handlers.wallet.present_topup_methods",
                new_callable=AsyncMock,
            ) as present:
                await nv_wallet_amount(callback, session, db_user, state)

        present.assert_not_awaited()
        callback.message.answer.assert_awaited()


class ShowNavKeyboardCustomerLevelsTests(unittest.IsolatedAsyncioTestCase):
    async def test_inline_shop_uses_main_keyboard(self):
        from app.bot import menu_nav as nav

        message = AsyncMock()
        message.answer = AsyncMock(return_value=MagicMock())
        session = AsyncMock()
        db_user = MagicMock()
        state = AsyncMock()
        state.get_data = AsyncMock(return_value={})
        state.update_data = AsyncMock()
        main_kb = MagicMock(name="MAIN_KB")

        with (
            patch(
                "app.bot.menu_nav.get_all_settings",
                AsyncMock(return_value={"nav_mode": "inline"}),
            ),
            patch(
                "app.bot.menu_nav.build_main_reply_keyboard",
                AsyncMock(return_value=(main_kb, {}, "user")),
            ),
            patch(
                "app.bot.menu_nav.kb.shop_reply_keyboard",
                return_value="SHOP_CHROME",
            ) as shop_chrome,
        ):
            await nav.show_nav_keyboard(
                message,
                session,
                db_user,
                nav.NAV_SHOP,
                text="فروشگاه",
                state=state,
            )

        shop_chrome.assert_not_called()
        self.assertIs(
            message.answer.await_args.kwargs.get("reply_markup"),
            main_kb,
        )


class PresentTopupMethodsHealTests(unittest.IsolatedAsyncioTestCase):
    async def test_heal_main_sends_methods_then_main_kb(self):
        from app.bot.handlers.wallet import present_topup_methods

        message = AsyncMock()
        message.answer = AsyncMock(return_value=MagicMock())
        session = AsyncMock()
        db_user = MagicMock()
        state = AsyncMock()
        state.get_data = AsyncMock(return_value={})
        state.update_data = AsyncMock()
        state.set_state = AsyncMock()
        main_kb = MagicMock(name="MAIN")

        with (
            patch(
                "app.bot.handlers.wallet.get_all_settings",
                AsyncMock(
                    return_value={
                        "nav_mode": "inline",
                        "pay_card_enabled": "1",
                    }
                ),
            ),
            patch(
                "app.bot.menu_nav.build_main_reply_keyboard",
                AsyncMock(return_value=(main_kb, {}, "user")),
            ),
            patch(
                "app.bot.handlers.wallet.get_settings",
                return_value=MagicMock(currency="IRT"),
            ),
            patch(
                "app.bot.handlers.wallet.format_toman",
                return_value="۵۰٬۰۰۰",
            ),
        ):
            await present_topup_methods(
                message, session, db_user, state, 50_000, heal_main=True
            )

        # methods bubble + heal main KB
        self.assertGreaterEqual(message.answer.await_count, 2)
        last = message.answer.await_args_list[-1]
        self.assertIs(last.kwargs.get("reply_markup"), main_kb)


if __name__ == "__main__":
    unittest.main()
