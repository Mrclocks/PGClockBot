"""Delivery UX: QR-first, WireGuard files, guides last; no mid «در حال تحویل»."""

from __future__ import annotations

import inspect
import io
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


SAMPLE_CONF = """[Interface]
PrivateKey = abc
Address = 10.66.66.2/32

[Peer]
PublicKey = xyz
AllowedIPs = 0.0.0.0/0
Endpoint = example.com:51820
"""


class WireGuardParseTests(unittest.TestCase):
    def test_plain_conf(self):
        from app.services.wireguard_delivery import parse_wireguard_payload

        files = parse_wireguard_payload(SAMPLE_CONF.encode(), default_stem="u1")
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].filename.endswith(".conf"))
        self.assertIn(b"PrivateKey", files[0].content)

    def test_zip_confs(self):
        from app.services.wireguard_delivery import parse_wireguard_payload

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("node-a.conf", SAMPLE_CONF)
            zf.writestr("readme.txt", "nope")
            zf.writestr("node-b.conf", SAMPLE_CONF.replace("10.66.66.2", "10.66.66.3"))
        files = parse_wireguard_payload(buf.getvalue())
        self.assertEqual(len(files), 2)
        names = {f.filename for f in files}
        self.assertIn("node-a.conf", names)
        self.assertIn("node-b.conf", names)

    def test_rejects_html(self):
        from app.services.wireguard_delivery import parse_wireguard_payload

        self.assertEqual(parse_wireguard_payload(b"<html>nope</html>"), [])


class CaptionComposeTests(unittest.TestCase):
    def test_compose_and_truncate(self):
        from app.services.delivery import _compose_caption

        self.assertEqual(_compose_caption("H", "B"), "H\n\nB")
        long_base = "B" * 1000
        out = _compose_caption("HEADER-" + ("X" * 200), long_base, limit=1024)
        self.assertLessEqual(len(out), 1024)
        self.assertTrue(out.endswith(long_base) or out == long_base[:1024])


class DeliveryOrderContractTests(unittest.TestCase):
    def test_guides_after_qr_and_wireguard(self):
        from app.services import delivery as delivery_mod

        src = inspect.getsource(delivery_mod.send_delivery_to_user)
        qr_i = src.index("send_subscription_qr_photo")
        wg_i = src.index("_send_wireguard_documents")
        guide_i = src.index("_send_delivery_guides")
        self.assertLess(qr_i, wg_i)
        self.assertLess(wg_i, guide_i)
        self.assertIn("caption_header", src)
        self.assertIn("_delivery_qr_header", src)

    def test_shop_no_delivering_toast(self):
        shop = Path("app/bot/handlers/shop.py").read_text(encoding="utf-8")
        self.assertNotIn("سرویس در حال تحویل است", shop)
        self.assertIn("پرداخت تأیید شد", shop)

    def test_pasarguard_wireguard_bytes_method(self):
        src = Path("app/services/pasarguard.py").read_text(encoding="utf-8")
        self.assertIn("subscription_wireguard_bytes", src)
        self.assertIn("resp.content", src)


class DeliveryFlowMockTests(unittest.IsolatedAsyncioTestCase):
    async def test_qr_success_skips_ready_text_then_wg_then_guides(self):
        from app.services.delivery import send_delivery_to_user
        from app.services.wireguard_delivery import WireGuardFile

        bot = AsyncMock()
        session = AsyncMock()
        order = MagicMock(
            id=6,
            note=None,
            service_id=9,
            user_id=1,
            reseller_id=None,
            plan_id=None,
            plan=MagicMock(name="ماهانه ۳۰ گیگ", is_trial=False),
        )
        # plan.name via MagicMock attribute
        order.plan.name = "ماهانه ۳۰ گیگ"
        svc = MagicMock(subscription_url="https://pg.example/sub/tok123", pg_username="u1")

        async def _get(model, pk):
            if getattr(model, "__name__", "") == "UserService" or model is not None:
                from app.db.models import UserService, BotUser

                if model is UserService:
                    return svc
                if model is BotUser:
                    return MagicMock(role="user")
            return None

        session.get = AsyncMock(side_effect=_get)

        payload = {
            "text": "SHORT",
            "send_kw": {},
            "detail_text": None,
            "ui": {"qr_enabled": "1", "delivery_title": "✅ سرویس آماده است"},
            "sub_url": svc.subscription_url,
            "sub_info": {"username": "u1", "status": "active"},
            "skip_qr": False,
            "is_subscription": True,
        }

        call_order: list[str] = []

        async def _qr(*a, **k):
            call_order.append("qr")
            return True

        async def _wg(*a, **k):
            call_order.append("wg")
            return 1

        async def _guides(*a, **k):
            call_order.append("guides")

        with (
            patch("app.services.delivery.get_all_settings", AsyncMock(return_value=payload["ui"])),
            patch("app.services.delivery._buyer_reply_markup", AsyncMock(return_value=None)),
            patch(
                "app.services.delivery.order_quantity",
                create=True,
            ),
            patch(
                "app.services.orders.order_quantity",
                return_value=1,
            ),
            patch(
                "app.services.delivery.build_delivery_content",
                AsyncMock(return_value=payload),
            ),
            patch(
                "app.services.delivery.send_subscription_qr_photo",
                AsyncMock(side_effect=_qr),
            ),
            patch(
                "app.services.delivery._send_wireguard_documents",
                AsyncMock(side_effect=_wg),
            ),
            patch(
                "app.services.delivery._send_delivery_guides",
                AsyncMock(side_effect=_guides),
            ),
            patch(
                "app.services.wireguard_delivery.fetch_wireguard_files",
                AsyncMock(
                    return_value=[
                        WireGuardFile(filename="u1.conf", content=SAMPLE_CONF.encode())
                    ]
                ),
            ),
        ):
            await send_delivery_to_user(bot, 100, session, None, order)

        self.assertEqual(call_order, ["qr", "wg", "guides"])
        bot.send_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
