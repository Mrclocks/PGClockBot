"""Delivery guide audience follows purchase context, not BotUser.role."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.db.models import ResellerApplicationStatus


class DeliveryGuideAudienceTests(unittest.IsolatedAsyncioTestCase):
    async def test_subscription_delivery_sends_user_guides_for_reseller_buyer(self):
        from app.services.delivery import _send_delivery_guides

        bot = AsyncMock()
        session = AsyncMock()
        order = MagicMock(user_id=7, note=None)
        ui = {"btn_guides": "📘 آموزش اتصال"}
        captured: dict = {}

        def fake_kb(ui_arg, *, audience="user"):
            captured["audience"] = audience
            return MagicMock()

        with (
            patch(
                "app.bot.handlers.guides.delivery_guides_keyboard",
                side_effect=fake_kb,
            ),
            patch(
                "app.services.connection_guides.get_connection_guides",
                new=AsyncMock(
                    return_value=[
                        {"id": "u", "audience": "user", "enabled": True},
                        {"id": "r", "audience": "reseller", "enabled": True},
                    ]
                ),
            ),
        ):
            await _send_delivery_guides(
                bot, 1, session, order=order, ui=ui, shop_rid=None
            )

        self.assertEqual(captured.get("audience"), "user")
        bot.send_message.assert_awaited()

    async def test_reseller_app_credentials_send_reseller_guides(self):
        from app.services.delivery import _deliver_reseller_app_order

        bot = AsyncMock()
        session = AsyncMock()
        order = MagicMock()
        order.id = 11
        order.user_id = 7
        order.note = "reseller_app:3"
        order.__dict__["_reseller_app_creds"] = {
            "username": "r1",
            "password": "x",
            "panel_url": "https://example.test",
        }

        app = MagicMock()
        app.status = ResellerApplicationStatus.APPROVED.value
        captured: dict = {}

        async def fake_send_guides(*args, **kwargs):
            captured["audience"] = kwargs.get("audience")

        with (
            patch(
                "app.services.delivery._try_send_html",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "app.services.delivery._send_delivery_guides",
                new=fake_send_guides,
            ),
            patch(
                "app.services.resellers.get_application",
                new=AsyncMock(return_value=app),
            ),
            patch(
                "app.services.resellers.format_credentials_message",
                return_value="creds",
            ),
            patch(
                "app.services.delivery.get_all_settings",
                new=AsyncMock(return_value={"btn_guides": "g"}),
            ),
            patch(
                "app.services.delivery.get_settings",
                return_value=MagicMock(admin_ids=[]),
            ),
        ):
            session.get = AsyncMock(return_value=MagicMock())
            await _deliver_reseller_app_order(bot, 1, session, order, None)

        self.assertEqual(captured.get("audience"), "reseller")

    async def test_service_card_guide_uses_user_for_reseller(self):
        from app.bot.handlers.services import svc_guide

        callback = AsyncMock()
        callback.data = "guide:svc:5"
        callback.message = AsyncMock()
        callback.answer = AsyncMock()
        session = AsyncMock()
        svc = MagicMock()
        svc.id = 5
        svc.bot_user_id = 1
        session.get = AsyncMock(return_value=svc)
        db_user = MagicMock()
        db_user.id = 1
        db_user.role = "reseller"

        with (
            patch(
                "app.bot.handlers.services.customer_may_use_service",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "app.services.connection_guides.get_connection_guides",
                new=AsyncMock(
                    return_value=[
                        {"id": "g1", "audience": "user", "enabled": True},
                        {"id": "panel", "audience": "reseller", "enabled": True},
                    ]
                ),
            ),
            patch(
                "app.bot.handlers.guides.show_guides_list",
                new=AsyncMock(),
            ) as show,
        ):
            await svc_guide(callback, session, db_user)

        self.assertEqual(show.await_args.kwargs.get("audience"), "user")


if __name__ == "__main__":
    unittest.main()
