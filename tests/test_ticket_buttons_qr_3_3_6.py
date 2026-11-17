"""3.3.6 — ticket notify reply/close buttons + QR background shrink/round."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class TicketNotifyButtonsTests(unittest.TestCase):
    def test_markup_has_reply_and_close(self):
        from app.services.notifications import ticket_action_markup

        markup = ticket_action_markup(42)
        flat = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        self.assertEqual(flat, ["tkt:reply:42", "tkt:close:42"])
        labels = [btn.text for row in markup.inline_keyboard for btn in row]
        self.assertTrue(any("پاسخ" in t for t in labels))
        self.assertTrue(any("بستن" in t for t in labels))


class QrBackgroundPolishTests(unittest.TestCase):

    def test_make_qr_with_background_is_smaller_plate_than_canvas(self):
        try:
            from PIL import Image
            import qrcode  # noqa: F401
        except ImportError:
            self.skipTest("Pillow/qrcode not installed")

        from app.services.qrcode_gen import make_subscription_qr

        with tempfile.TemporaryDirectory() as td:
            bg_path = Path(td) / "bg.png"
            Image.new("RGB", (400, 400), (20, 80, 160)).save(bg_path)
            with patch("app.services.qrcode_gen.resolve_media_path", return_value=bg_path):
                buf = make_subscription_qr("https://example.com/sub/abc", background="uploads/bg.png")
            img = Image.open(buf)
            # Canvas should exist (not bare tiny QR) and be square
            self.assertEqual(img.size[0], img.size[1])
            self.assertGreaterEqual(img.size[0], 200)
            # Corners of canvas should be background-ish (not pure white plate)
            # Sample a corner pixel — background was blue-ish after cover-fit
            corner = img.getpixel((2, 2))
            # Not the near-white plate color
            self.assertLess(sum(corner[:3]) / 3, 240)


if __name__ == "__main__":
    unittest.main()
