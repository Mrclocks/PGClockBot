"""Panel↔bot setting parity fixes (3.3.7 audit)."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.notifications import build_qr_caption
from app.services.users import (
    format_force_join_for_edit,
    normalize_force_join_channel_value,
    parse_force_join_channels,
    parse_force_join_entries,
)


class QrCaptionLinkToggleTests(unittest.TestCase):
    def test_link_included_by_default(self):
        cap, _kw = build_qr_caption(sub_url="https://ex.com/s/1", ui={})
        self.assertIn("https://ex.com/s/1", cap)
        self.assertIn("لینک اشتراک", cap)

    def test_link_hidden_when_toggle_off(self):
        cap, _kw = build_qr_caption(
            sub_url="https://ex.com/s/1",
            ui={"show_sub_link_in_text": "0"},
        )
        self.assertNotIn("https://ex.com/s/1", cap)
        self.assertNotIn("لینک اشتراک", cap)

    def test_custom_caption_still_works_without_auto_link(self):
        cap, _kw = build_qr_caption(
            sub_url="https://ex.com/s/1",
            ui={"qr_caption": "کد من", "show_sub_link_in_text": "0"},
        )
        self.assertIn("کد من", cap)
        self.assertNotIn("https://ex.com/s/1", cap)


class ForceJoinOptionalEditTests(unittest.TestCase):
    def test_format_keeps_optional_marker(self):
        raw = '[{"id":"@must","required":true},{"id":"@opt","required":false}]'
        text = format_force_join_for_edit(raw)
        self.assertIn("@must", text)
        self.assertIn("@opt !optional", text)

    def test_roundtrip_preserves_optional(self):
        text = "@must\n@opt !optional"
        norm = normalize_force_join_channel_value(text)
        self.assertEqual(parse_force_join_channels(norm), ["@must"])
        entries = parse_force_join_entries(norm)
        by_id = {e["id"]: e["required"] for e in entries}
        self.assertTrue(by_id["@must"])
        self.assertFalse(by_id["@opt"])

    def test_optional_paren_marker(self):
        entries = parse_force_join_entries("@a\n@b (اختیاری)")
        self.assertEqual(entries[0], {"id": "@a", "required": True})
        self.assertEqual(entries[1], {"id": "@b", "required": False})


class ResellerApplyMenuOrderTests(unittest.TestCase):
    def test_main_menu_shows_reseller_apply_from_order_even_if_show_flag_off(self):
        from app.bot.keyboards import main_menu

        ui = {
            "menu_order": "shop,reseller_apply,wallet",
            "menu_layout": "classic",
            "show_reseller_apply": "0",
            "btn_shop": "خرید",
            "btn_reseller_apply": "نمایندگی",
            "btn_wallet": "کیف",
            "support_contacts": "[]",
        }
        with patch("app.bot.keyboards.get_settings") as gs:
            gs.return_value.miniapp_enabled = False
            gs.return_value.miniapp_url = ""
            markup = main_menu("user", has_services=False, ui=ui)
        labels = [b.text for row in markup.inline_keyboard for b in row]
        self.assertIn("نمایندگی", labels)


class ReferralBonusHookTests(unittest.TestCase):
    def test_deliver_order_calls_referral_hook(self):
        src = (
            __import__("pathlib").Path("app/services/orders.py").read_text(encoding="utf-8")
        )
        self.assertIn("async def _maybe_pay_referral_bonus", src)
        self.assertIn("await _maybe_pay_referral_bonus(session, order)", src)
        self.assertIn('reason = f"referral:{int(buyer.id)}"', src)
        self.assertIn('get_setting(\n        session, "referral_bonus"', src)

    def test_skips_renew_and_reseller_app(self):
        import asyncio
        from app.services.orders import _maybe_pay_referral_bonus

        async def _run():
            session = AsyncMock()
            order = MagicMock()
            order.note = "renew:12"
            order.user = MagicMock(referred_by_id=9, id=3)
            order.user_id = 3
            order.reseller_id = None
            await _maybe_pay_referral_bonus(session, order)
            session.get.assert_not_called()

            order.note = "reseller_app:1"
            await _maybe_pay_referral_bonus(session, order)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
