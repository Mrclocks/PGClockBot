"""PSP checkout copy: VPN off for gateway + bot-primary return after pay."""

from __future__ import annotations

import unittest

from app.services.payment_settlement import (
    PSP_BOT_RETURN_NOTICE,
    PSP_VPN_NOTICE,
    with_psp_checkout_guidance,
    with_psp_vpn_notice,
)


class PspCheckoutGuidanceTests(unittest.TestCase):
    def test_appends_vpn_and_bot_return_once(self) -> None:
        out = with_psp_checkout_guidance("مبلغ قابل پرداخت: ۱۰۰۰")
        self.assertTrue(out.startswith("مبلغ قابل پرداخت: ۱۰۰۰"))
        self.assertIn(PSP_VPN_NOTICE, out)
        self.assertIn(PSP_BOT_RETURN_NOTICE, out)
        self.assertIn("VPN", out)
        self.assertIn("ربات", out)
        self.assertIn("سایت", out)
        self.assertEqual(out.count(PSP_VPN_NOTICE), 1)
        self.assertEqual(out.count(PSP_BOT_RETURN_NOTICE), 1)

    def test_does_not_duplicate_either_notice(self) -> None:
        once = with_psp_checkout_guidance("متن")
        again = with_psp_checkout_guidance(once)
        self.assertEqual(once, again)
        self.assertEqual(again.count(PSP_VPN_NOTICE), 1)
        self.assertEqual(again.count(PSP_BOT_RETURN_NOTICE), 1)

    def test_empty_body_is_just_guidance(self) -> None:
        out = with_psp_checkout_guidance("")
        self.assertEqual(out, f"{PSP_VPN_NOTICE}\n\n{PSP_BOT_RETURN_NOTICE}")

    def test_legacy_alias_matches_guidance(self) -> None:
        self.assertEqual(
            with_psp_vpn_notice("hello"),
            with_psp_checkout_guidance("hello"),
        )

    def test_partial_notices_are_completed(self) -> None:
        only_vpn = f"متن\n\n{PSP_VPN_NOTICE}"
        out = with_psp_checkout_guidance(only_vpn)
        self.assertEqual(out.count(PSP_VPN_NOTICE), 1)
        self.assertIn(PSP_BOT_RETURN_NOTICE, out)


if __name__ == "__main__":
    unittest.main()
