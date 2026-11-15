"""Guide open/list callback_data keeps audience within Telegram's 64-byte limit."""

from __future__ import annotations

import unittest

from app.bot.handlers.guides import (
    _open_callback,
    _parse_open_callback,
    guides_list_keyboard,
)


class GuideCallbackAudienceTests(unittest.TestCase):
    def test_reseller_suffix_survives_long_gid(self):
        prefix = "guide:open:0:"
        long_gid = "g" * 80
        cb = _open_callback(prefix, long_gid, audience="reseller")
        self.assertLessEqual(len(cb), 64)
        self.assertTrue(cb.endswith(":reseller"), cb)
        # gid is truncated; suffix is not chopped mid-token
        self.assertNotEqual(cb[-9:], ":reselle")

    def test_list_keyboard_reseller_callbacks_keep_suffix(self):
        items = [{"id": "x" * 80, "title": "long", "enabled": True}]
        kb = guides_list_keyboard(items, {}, audience="reseller")
        open_cbs = [
            btn.callback_data
            for row in kb.inline_keyboard
            for btn in row
            if (btn.callback_data or "").startswith("guide:open:")
        ]
        self.assertEqual(len(open_cbs), 1)
        self.assertLessEqual(len(open_cbs[0]), 64)
        self.assertTrue(open_cbs[0].endswith(":reseller"), open_cbs[0])

    def test_parse_prefers_full_id_ending_with_reseller(self):
        known = {"panel:reseller", "panel"}
        svc, gid, aud = _parse_open_callback(
            ["guide", "open", "0", "panel", "reseller"], known
        )
        self.assertEqual(svc, 0)
        self.assertEqual(gid, "panel:reseller")
        self.assertEqual(aud, "user")

    def test_parse_strips_audience_when_full_id_unknown(self):
        known = {"panel"}
        svc, gid, aud = _parse_open_callback(
            ["guide", "open", "3", "panel", "reseller"], known
        )
        self.assertEqual(svc, 3)
        self.assertEqual(gid, "panel")
        self.assertEqual(aud, "reseller")

    def test_parse_double_reseller_token_for_id_that_ends_with_reseller(self):
        known = {"panel:reseller"}
        _svc, gid, aud = _parse_open_callback(
            ["guide", "open", "0", "panel", "reseller", "reseller"], known
        )
        self.assertEqual(gid, "panel:reseller")
        self.assertEqual(aud, "reseller")


if __name__ == "__main__":
    unittest.main()
