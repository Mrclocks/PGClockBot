"""3.3.6 — ticket notify reply/close buttons + QR background shrink/round."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class TicketNotifyButtonsTests(unittest.TestCase):
    def test_markup_has_reply_and_close(self):
        from app.services.notifications import ticket_action_markup

        markup = ticket_action_markup(42)
        flat = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        self.assertEqual(flat, ["tkt:reply:42", "tkt:close:42"])
        labels = [btn.text for row in markup.inline_keyboard for btn in row]
        self.assertTrue(any("پاسخ" in t for t in labels))
        self.assertTrue(any("بستن" in t for t in labels))

    def test_notify_new_ticket_sends_markup(self):
        src = (ROOT / "app/services/notifications.py").read_text(encoding="utf-8")
        self.assertIn("ticket_action_markup", src)
        self.assertIn("markup=ticket_action_markup(", src)
        self.assertIn("_ticket_reseller_chat_ids", src)
        self.assertIn("notify_ticket_message", src)

    def test_handlers_registered(self):
        init = (ROOT / "app/bot/__init__.py").read_text(encoding="utf-8")
        self.assertIn("ticket_actions", init)
        self.assertIn("ticket_actions.router", init)
        actions = (ROOT / "app/bot/handlers/ticket_actions.py").read_text(encoding="utf-8")
        self.assertIn('F.data.startswith("tkt:reply:")', actions)
        self.assertIn('F.data.startswith("tkt:close:")', actions)
        self.assertIn("close_ticket", actions)

    def test_close_ticket_service(self):
        src = (ROOT / "app/services/tickets.py").read_text(encoding="utf-8")
        self.assertIn("async def close_ticket", src)
        self.assertIn("TicketStatus.CLOSED.value", src)

    def test_admin_and_user_reply_use_notify_helper(self):
        admin = (ROOT / "app/bot/handlers/admin.py").read_text(encoding="utf-8")
        support = (ROOT / "app/bot/handlers/support.py").read_text(encoding="utf-8")
        self.assertIn("notify_ticket_message", admin)
        self.assertIn("notify_ticket_message", support)
        self.assertIn("ticket_user_id=db_user.id", support)
        # Old plain DM without buttons must be gone
        self.assertNotIn("پاسخ پشتیبانی برای تیکت #", admin)


class QrBackgroundPolishTests(unittest.TestCase):
    def test_source_shrinks_and_rounds_when_background(self):
        src = (ROOT / "app/services/qrcode_gen.py").read_text(encoding="utf-8")
        self.assertIn("ImageDraw", src)
        self.assertIn("rounded_rectangle", src)
        self.assertIn("shrink = 0.82", src)
        self.assertIn("+ 140", src)
        self.assertIn("radius = 18", src)

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
