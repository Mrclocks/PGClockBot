"""The service card keeps the daily actions; the rest lives on «⚙️ بیشتر»."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.bot.handlers import services
from app.bot.nav_inline import service_card_keyboard, service_more_keyboard
from app.services import bot_feedback_texts as feedback


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


class CardLayoutTests(unittest.TestCase):
    def test_the_card_has_the_daily_actions_and_more(self) -> None:
        self.assertEqual(
            _callbacks(service_card_keyboard(7, {})),
            ["svc:renew:7", "svc:addon:7", "svc:link:7", "guide:svc:7", "svc:more:7", "svc:list"],
        )

    def test_more_holds_the_rest_and_backs_up_to_the_card(self) -> None:
        self.assertEqual(
            _callbacks(service_more_keyboard(7, {}, wireguard=True)),
            ["svc:view:7", "svc:wg:7", "svc:auto:7", "svc:cancel:7", "svc:delask:7", "svc:view:7"],
        )
        self.assertNotIn("svc:wg:7", _callbacks(service_more_keyboard(7, {})))
        self.assertNotIn("svc:cancel:7", _callbacks(service_more_keyboard(7, {}, external=True)))

    def test_an_edited_label_is_kept(self) -> None:
        texts = [b.text for row in service_card_keyboard(7, {"btn_svc_more": "🔧 سایر"}).inline_keyboard for b in row]
        self.assertIn("🔧 سایر", texts)


class MoreScreenTests(unittest.IsolatedAsyncioTestCase):
    def _callback(self, data: str) -> MagicMock:
        return MagicMock(data=data, answer=AsyncMock(), message=MagicMock())

    async def test_more_opens_for_the_customers_own_service(self) -> None:
        callback = self._callback("svc:more:7")
        svc = SimpleNamespace(id=7, pg_username="<b>me</b>", pg_user_id=None)
        with (
            patch.object(services, "customer_may_use_service", AsyncMock(return_value=True)),
            patch.object(services, "get_all_settings", AsyncMock(return_value={})),
            patch.object(services, "is_external_service", return_value=False),
            patch.object(services, "fetch_live_service_info", AsyncMock(return_value={})),
            patch.object(services, "safe_edit_text", AsyncMock()) as shown,
        ):
            await services.svc_more(callback, MagicMock(get=AsyncMock(return_value=svc)), SimpleNamespace(id=1))
        self.assertIn("&lt;b&gt;me&lt;/b&gt;", shown.await_args.args[1])
        self.assertIn("svc:delask:7", _callbacks(shown.await_args.kwargs["reply_markup"]))

    async def test_another_customers_service_is_refused(self) -> None:
        callback = self._callback("svc:more:8")
        with (
            patch.object(services, "customer_may_use_service", AsyncMock(return_value=False)),
            patch.object(services, "safe_edit_text", AsyncMock()) as shown,
        ):
            await services.svc_more(callback, MagicMock(get=AsyncMock(return_value=None)), SimpleNamespace(id=1))
        callback.answer.assert_awaited_once_with(feedback.NOT_FOUND, show_alert=True)
        shown.assert_not_awaited()

    async def test_a_forged_id_is_refused(self) -> None:
        callback = self._callback("svc:more:x")
        await services.svc_more(callback, MagicMock(), SimpleNamespace(id=1))
        callback.answer.assert_awaited_once_with(feedback.INVALID, show_alert=True)

    async def test_screens_opened_from_more_back_up_to_more(self) -> None:
        self.assertEqual(services.more_callback(7), "svc:more:7")
        callback = self._callback("svc:delask:7")
        with (
            patch.object(services, "customer_may_use_service", AsyncMock(return_value=True)),
            patch.object(services, "is_external_service", return_value=False),
            patch.object(services, "safe_edit_text", AsyncMock()) as shown,
        ):
            await services.svc_delete_ask(
                callback, MagicMock(get=AsyncMock(return_value=SimpleNamespace(id=7, pg_username="a"))), SimpleNamespace(id=1)
            )
        self.assertIn("svc:more:7", _callbacks(shown.await_args.kwargs["reply_markup"]))


if __name__ == "__main__":
    unittest.main()
