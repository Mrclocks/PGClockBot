"""Payment settings: every setting of one method sits in that method's section, switch first."""

from __future__ import annotations

import unittest

from app.services.users import SETTING_GROUPS, TAB_SETTING_GROUPS, keys_for_tab

# Every key the payment tab saved before the regroup, minus the referral bonus that moved to loyalty.
PAYMENT_KEYS_BEFORE = {
    "pay_wallet_enabled", "pay_card_enabled", "auto_approve_payments", "auto_approve_after_minutes",
    "auto_approve_after_max_amount", "auto_approve_after_returning_only", "pay_card_auto_enabled",
    "pay_bbc_enabled", "pay_gateway_enabled", "pay_psp_enabled", "pay_crypto_enabled", "pay_stars_enabled",
    "pay_discount_enabled", "referral_bonus", "payment_reject_text", "payment_cards", "payment_cards_random",
    "card_pay_text", "btn_pay_card", "card_auto_provider", "card_auto_webhook_secret", "card_auto_hint_text",
    "payment_gateways", "gateway_pay_text", "btn_pay_gateway", "psp_gateways", "psp_checkout_link_mode",
    "psp_pay_text", "btn_pay_psp", "bbc_api_key", "bbc_pay_text", "btn_pay_bbc", "payment_crypto_wallets",
    "crypto_pay_text", "crypto_rate_mode", "crypto_rate_asset", "crypto_rate_manual", "crypto_rate_api_url",
    "crypto_rate_api_path", "crypto_rate_api_divisor", "crypto_rate_margin_percent", "crypto_quote_minutes",
    "btn_pay_crypto", "stars_toman_per_star", "stars_title", "stars_description", "btn_pay_stars",
    "btn_pay_wallet", "btn_pay_discount", "shop_maintenance_enabled", "shop_maintenance_text",
    "receipt_auto_match_enabled", "receipt_match_window_minutes",
}

METHOD_SWITCH = {
    "کیف پول": "pay_wallet_enabled",
    "کارت به کارت": "pay_card_enabled",
    "تأیید خودکار کارت به کارت": "pay_card_auto_enabled",
    "درگاه پرداخت": "pay_gateway_enabled",
    "درگاه آنلاین API": "pay_psp_enabled",
    "بزن به کارت": "pay_bbc_enabled",
    "رمزارز": "pay_crypto_enabled",
    "استارز تلگرام": "pay_stars_enabled",
    "کد تخفیف": "pay_discount_enabled",
}


class PaymentSettingsLayoutTests(unittest.TestCase):
    def test_no_payment_setting_is_lost_or_duplicated(self) -> None:
        self.assertEqual(keys_for_tab("payment"), PAYMENT_KEYS_BEFORE - {"referral_bonus"})
        keys = [row[0] for name in TAB_SETTING_GROUPS["payment"] for row in SETTING_GROUPS[name]]
        self.assertEqual(len(keys), len(set(keys)))

    def test_each_method_section_starts_with_its_switch(self) -> None:
        for section, switch in METHOD_SWITCH.items():
            self.assertIn(section, TAB_SETTING_GROUPS["payment"])
            self.assertEqual(SETTING_GROUPS[section][0][0], switch, section)

    def test_button_texts_live_with_their_method(self) -> None:
        for section, button in (("کیف پول", "btn_pay_wallet"), ("کارت به کارت", "btn_pay_card"), ("کد تخفیف", "btn_pay_discount")):
            self.assertIn(button, [row[0] for row in SETTING_GROUPS[section]])

    def test_receipt_rules_are_one_section(self) -> None:
        receipts = [row[0] for row in SETTING_GROUPS["رسیدها"]]
        for key in ("auto_approve_payments", "receipt_auto_match_enabled", "receipt_match_window_minutes", "payment_reject_text"):
            self.assertIn(key, receipts)

    def test_referral_bonus_saves_with_the_referral_text(self) -> None:
        self.assertIn("referral_bonus", keys_for_tab("loyalty"))
        self.assertNotIn("referral_bonus", keys_for_tab("payment"))


if __name__ == "__main__":
    unittest.main()
