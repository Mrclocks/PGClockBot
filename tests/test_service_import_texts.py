"""Customer texts and buttons for services that were not sold by the bot (linked or imported)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


from app.bot.handlers import services as services_handlers
from app.bot.nav_inline import service_more_keyboard
from app.db.models import BotUser, UserService
from tests.test_service_import import ImportCase


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _callback(data: str):
    message = AsyncMock()
    message.from_user = SimpleNamespace(is_bot=True)
    return SimpleNamespace(data=data, message=message, answer=AsyncMock())


class ServiceCardKeyboardTests(unittest.TestCase):
    def test_a_sold_service_offers_cancellation(self):
        self.assertIn("svc:cancel:7", _callbacks(service_more_keyboard(7)))

    def test_an_external_service_has_no_cancel_button_but_keeps_everything_else(self):
        sold = _callbacks(service_more_keyboard(7))
        external = _callbacks(service_more_keyboard(7, external=True))
        self.assertNotIn("svc:cancel:7", external)
        self.assertEqual([c for c in sold if c != "svc:cancel:7"], external)


class DeleteConfirmationTests(ImportCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        async with self.Session() as session:
            session.add_all([
                UserService(id=901, bot_user_id=1, pg_user_id=901, pg_username="sold", remark="order:1"),
                UserService(id=902, bot_user_id=1, pg_user_id=902, pg_username="old", remark="linked"),
                UserService(id=903, bot_user_id=1, pg_user_id=903, pg_username="new", remark="imported"),
            ])
            await session.commit()

    async def _ask(self, service_id: int) -> str:
        async with self.Session() as session:
            user = await session.get(BotUser, 1)
            with patch.object(services_handlers, "safe_edit_text", AsyncMock()) as edit:
                await services_handlers.svc_delete_ask(_callback(f"svc:delask:{service_id}"), session, user)
        return edit.await_args.args[1]

    async def test_a_sold_service_warns_that_the_link_stops_working(self):
        text = await self._ask(901)
        self.assertIn("برای همیشه", text)
        self.assertIn("از کار می‌افتد", text)

    async def test_an_external_service_says_it_is_only_removed_from_the_list(self):
        for service_id in (902, 903):
            text = await self._ask(service_id)
            self.assertNotIn("برای همیشه", text)
            self.assertNotIn("از کار می‌افتد", text)
            self.assertIn("از لیست شما حذف شود", text)

    async def test_the_confirm_button_names_what_it_does(self):
        async with self.Session() as session:
            user = await session.get(BotUser, 1)
            labels = {}
            for service_id in (901, 903):
                with patch.object(services_handlers, "safe_edit_text", AsyncMock()) as edit:
                    await services_handlers.svc_delete_ask(_callback(f"svc:delask:{service_id}"), session, user)
                labels[service_id] = edit.await_args.kwargs["reply_markup"].inline_keyboard[0][0].text
        self.assertIn("حذف شود", labels[901])
        self.assertEqual(labels[903], "بله، از لیست من حذف شود")


class ImportedServiceCardTests(ImportCase):
    async def test_the_card_shown_after_a_deep_link_has_no_cancel_button(self):
        from app.bot.handlers import start

        message = AsyncMock()
        async with self.Session() as session:
            user = await session.get(BotUser, 1)
            with patch.object(start, "get_all_settings", AsyncMock(return_value={"service_import_enabled": "1"})):
                await start._link_subscription(message, session, user, "tok-plat-0000001")
        callbacks = _callbacks(message.answer.await_args.kwargs["reply_markup"])
        self.assertTrue(any(c.startswith("svc:renew:") for c in callbacks))
        self.assertFalse(any(c.startswith("svc:cancel:") for c in callbacks))

    async def test_more_hides_cancel_for_imported_but_not_for_sold_services(self):
        async with self.Session() as session:
            session.add_all([
                UserService(id=901, bot_user_id=1, pg_user_id=901, pg_username="sold", remark="order:1"),
                UserService(id=903, bot_user_id=1, pg_user_id=903, pg_username="new", remark="imported"),
            ])
            await session.commit()
            user = await session.get(BotUser, 1)
            shown = {}
            for service_id in (901, 903):
                with (
                    patch.object(services_handlers, "get_all_settings", AsyncMock(return_value={})),
                    patch.object(services_handlers, "safe_edit_text", AsyncMock()) as edit,
                ):
                    await services_handlers.svc_more(_callback(f"svc:more:{service_id}"), session, user)
                shown[service_id] = _callbacks(edit.await_args.kwargs["reply_markup"])
        self.assertIn("svc:cancel:901", shown[901])
        self.assertNotIn("svc:cancel:903", shown[903])


if __name__ == "__main__":
    unittest.main()
