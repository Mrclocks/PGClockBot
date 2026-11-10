"""Owner bot settings expose the same «مدیریت PAYG» keys as the web finance modal."""

from __future__ import annotations

import unittest


class AdminPaygSettingsBotTests(unittest.TestCase):
    def test_pay_section_lists_payg_and_credit_ladder_subs(self) -> None:
        from app.bot.handlers.admin_settings import FIELDS, SECTIONS, SELECT_CYCLE_OPTIONS

        pay_subs = {sub[0]: sub for sub in SECTIONS["pay"]["subs"]}
        self.assertIn("payg", pay_subs)
        self.assertIn("payg_credit", pay_subs)
        self.assertEqual(pay_subs["payg"][1], "مدیریت PAYG")
        self.assertEqual(pay_subs["payg_credit"][1], "مهلت اعتبار PAYG")

        payg_keys = [f[0] for f in pay_subs["payg"][2]]
        credit_keys = [f[0] for f in pay_subs["payg_credit"][2]]
        self.assertEqual(
            payg_keys,
            [
                "billing_enabled",
                "billing_low_balance",
                "billing_on_empty",
                "billing_tick_minutes",
            ],
        )
        self.assertEqual(
            credit_keys,
            [
                "credit_due_days",
                "credit_block_create_days",
                "credit_block_changes_days",
                "credit_block_days",
            ],
        )
        self.assertLessEqual(len(payg_keys), 8)
        self.assertLessEqual(len(credit_keys), 8)
        self.assertNotIn("billing_price_per_gb", payg_keys)

        for key in payg_keys + credit_keys:
            self.assertIn(key, FIELDS)
        self.assertEqual(FIELDS["billing_enabled"][2], "toggle")
        self.assertEqual(FIELDS["billing_on_empty"][2], "select")
        self.assertEqual(FIELDS["billing_tick_minutes"][2], "number")
        self.assertEqual(
            SELECT_CYCLE_OPTIONS["billing_on_empty"][0][0],
            "block_provision",
        )

    def test_bot_keys_match_web_setting_group(self) -> None:
        from app.bot.handlers.admin_settings import SECTIONS
        from app.services.users import SETTING_GROUPS

        web_keys = [item[0] for item in SETTING_GROUPS["مدیریت PAYG"]]
        bot_keys: list[str] = []
        for sub in SECTIONS["pay"]["subs"]:
            if sub[0] in ("payg", "payg_credit") and isinstance(sub[2], list):
                bot_keys.extend(f[0] for f in sub[2])
        self.assertEqual(bot_keys, web_keys)

    def test_reseller_settings_do_not_expose_platform_payg(self) -> None:
        from app.bot.handlers.reseller_settings import FIELDS as RES_FIELDS
        from app.bot.handlers.reseller_settings import SECTIONS as RES_SECTIONS

        for key in (
            "billing_enabled",
            "billing_low_balance",
            "billing_on_empty",
            "billing_tick_minutes",
            "credit_due_days",
            "credit_block_create_days",
            "credit_block_changes_days",
            "credit_block_days",
        ):
            self.assertNotIn(key, RES_FIELDS)
        for sec in RES_SECTIONS.values():
            for sub in sec.get("subs") or []:
                self.assertNotIn(sub[0], ("payg", "payg_credit"))

    def test_owner_screen_routes_payg_keys_back_to_pay_subs(self) -> None:
        from app.bot.handlers.admin_settings import _owner_screen_for_key

        self.assertEqual(_owner_screen_for_key("billing_enabled"), ("pay", "payg"))
        self.assertEqual(_owner_screen_for_key("credit_due_days"), ("pay", "payg_credit"))
        self.assertEqual(_owner_screen_for_key("billing_on_empty"), ("pay", "payg"))


if __name__ == "__main__":
    unittest.main()
