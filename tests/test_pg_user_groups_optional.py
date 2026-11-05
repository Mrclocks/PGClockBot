"""Custom PG user create/edit must not require groups; delivery guides follow purchase context."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.pasarguard import build_user_create_payload, build_user_modify_payload


class BuildPayloadGroupsOptionalTests(unittest.TestCase):
    def test_create_payload_allows_empty_groups(self):
        p = build_user_create_payload(username="u1", group_ids=[])
        self.assertEqual(p["group_ids"], [])
        self.assertEqual(p["username"], "u1")

    def test_create_payload_none_groups_becomes_empty_list(self):
        p = build_user_create_payload(username="u2", group_ids=None)
        self.assertEqual(p["group_ids"], [])

    def test_modify_omits_groups_when_none(self):
        p = build_user_modify_payload(data_limit=1024, expire_ts=0, group_ids=None)
        self.assertIn("data_limit", p)
        self.assertNotIn("group_ids", p)

    def test_modify_includes_explicit_empty_groups(self):
        p = build_user_modify_payload(group_ids=[])
        self.assertEqual(p["group_ids"], [])


class DeliveryGuideAudienceTests(unittest.IsolatedAsyncioTestCase):
    async def test_subscription_delivery_defaults_to_user_guides(self):
        from app.services.delivery import _send_delivery_guides

        bot = AsyncMock()
        session = AsyncMock()
        order = MagicMock(user_id=7)
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

    async def test_explicit_reseller_audience_for_panel_onboarding(self):
        from app.services.delivery import _send_delivery_guides

        bot = AsyncMock()
        session = AsyncMock()
        order = MagicMock(user_id=7)
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
                bot,
                1,
                session,
                order=order,
                ui=ui,
                shop_rid=None,
                audience="reseller",
            )

        self.assertEqual(captured.get("audience"), "reseller")

    async def test_audience_helper_ignores_reseller_role(self):
        from app.bot.handlers.guides import _audience_for_user

        db_user = MagicMock()
        db_user.role = "reseller"
        self.assertEqual(await _audience_for_user(AsyncMock(), db_user), "user")

    async def test_service_card_guide_uses_user_audience_for_reseller(self):
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

        show.assert_awaited()
        self.assertEqual(show.await_args.kwargs.get("audience"), "user")
        self.assertEqual(show.await_args.kwargs.get("svc_id"), 5)


if __name__ == "__main__":
    unittest.main()
