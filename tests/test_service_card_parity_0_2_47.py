"""Service-card parity with legacy REPLY_ACTION_SVC_* + ownership gates."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import app.bot.keyboards  # noqa: F401

from app.bot.reply_keyboards import (
    REPLY_ACTION_SVC_ADDON,
    REPLY_ACTION_SVC_AUTO,
    REPLY_ACTION_SVC_CANCEL,
    REPLY_ACTION_SVC_DELETE,
    REPLY_ACTION_SVC_GUIDE,
    REPLY_ACTION_SVC_LINK,
    REPLY_ACTION_SVC_REFRESH,
    REPLY_ACTION_SVC_RENEW,
)


# Legacy reply action → expected inline callback prefix on the card.
_CARD_CALLBACK_BY_ACTION = {
    REPLY_ACTION_SVC_RENEW: "svc:renew:",
    REPLY_ACTION_SVC_ADDON: "svc:addon:",
    REPLY_ACTION_SVC_LINK: "svc:link:",
    REPLY_ACTION_SVC_GUIDE: "guide:svc:",
    REPLY_ACTION_SVC_CANCEL: "svc:cancel:",
    REPLY_ACTION_SVC_AUTO: "svc:auto:",
    REPLY_ACTION_SVC_REFRESH: "svc:view:",
    REPLY_ACTION_SVC_DELETE: "svc:delask:",
}


class ServiceCardParityTests(unittest.TestCase):
    def test_every_legacy_svc_action_has_card_button(self):
        from app.bot.nav_inline import service_card_keyboard

        kb = service_card_keyboard(42, {"btn_renew": "تمدید", "btn_guides": "آموزش"})
        data = [b.callback_data for row in kb.inline_keyboard for b in row]
        for action, prefix in _CARD_CALLBACK_BY_ACTION.items():
            self.assertTrue(
                any((d or "").startswith(prefix) for d in data),
                f"{action} missing prefix {prefix!r} in {data}",
            )
        self.assertIn("svc:cancel:42", data)
        self.assertIn("guide:svc:42", data)
        # Delete remains after cancel (visually separated).
        cancel_i = data.index("svc:cancel:42")
        delete_i = data.index("svc:delask:42")
        self.assertLess(cancel_i, delete_i)

    def test_card_no_longer_uses_plain_nv_guide(self):
        from app.bot.nav_inline import service_card_keyboard

        data = [
            b.callback_data
            for row in service_card_keyboard(7, {}).inline_keyboard
            for b in row
        ]
        self.assertFalse(any((d or "").startswith("nv:svc:guide:") for d in data))


class SvcCancelOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_b_cannot_open_user_a_cancel(self):
        from app.bot.handlers.services import svc_cancel

        callback = AsyncMock()
        callback.data = "svc:cancel:99"
        callback.message = AsyncMock()
        callback.answer = AsyncMock()
        session = AsyncMock()
        db_user = MagicMock()
        db_user.id = 2

        with (
            patch(
                "app.services.service_cancellations.cancellation_status",
                new=AsyncMock(side_effect=ValueError("سرویس یافت نشد")),
            ),
            patch(
                "app.bot.handlers.services.safe_edit_text",
                new=AsyncMock(),
            ) as edit,
        ):
            await svc_cancel(callback, session, db_user)

        callback.answer.assert_awaited()
        self.assertTrue(callback.answer.await_args.kwargs.get("show_alert"))
        edit.assert_not_awaited()


class SvcGuideCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_catalog_opens_guides_list(self):
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
        db_user.role = "user"

        with (
            patch(
                "app.bot.handlers.guides._audience_for_user",
                new=AsyncMock(return_value="user"),
            ),
            patch(
                "app.services.connection_guides.get_connection_guides",
                new=AsyncMock(return_value=[{"id": "g1", "audience": "user", "enabled": True}]),
            ),
            patch(
                "app.services.connection_guides.guides_for_audience",
                return_value=[{"id": "g1"}],
            ),
            patch(
                "app.bot.handlers.guides.show_guides_list",
                new=AsyncMock(),
            ) as show,
        ):
            await svc_guide(callback, session, db_user)

        show.assert_awaited()
        self.assertEqual(show.await_args.kwargs.get("svc_id"), 5)

    async def test_empty_catalog_falls_back_to_guide_text(self):
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
        db_user.role = "user"

        with (
            patch(
                "app.bot.handlers.guides._audience_for_user",
                new=AsyncMock(return_value="user"),
            ),
            patch(
                "app.services.connection_guides.get_connection_guides",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "app.services.connection_guides.guides_for_audience",
                return_value=[],
            ),
            patch(
                "app.bot.handlers.services.get_all_settings",
                new=AsyncMock(return_value={"guide_text": "متن راهنما", "btn_back": "بازگشت"}),
            ),
            patch(
                "app.bot.handlers.services.safe_edit_text",
                new=AsyncMock(),
            ) as edit,
            patch(
                "app.bot.handlers.guides.show_guides_list",
                new=AsyncMock(),
            ) as show,
        ):
            await svc_guide(callback, session, db_user)

        show.assert_not_awaited()
        edit.assert_awaited()
        markup = edit.await_args.kwargs.get("reply_markup")
        flat = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("svc:view:5", flat)

    async def test_foreign_service_guide_refused(self):
        from app.bot.handlers.services import svc_guide

        callback = AsyncMock()
        callback.data = "guide:svc:5"
        callback.answer = AsyncMock()
        session = AsyncMock()
        svc = MagicMock()
        svc.bot_user_id = 99
        session.get = AsyncMock(return_value=svc)
        db_user = MagicMock()
        db_user.id = 1

        await svc_guide(callback, session, db_user)
        callback.answer.assert_awaited()
        self.assertTrue(callback.answer.await_args.kwargs.get("show_alert"))


if __name__ == "__main__":
    unittest.main()
