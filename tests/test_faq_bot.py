"""«سوالات متداول» in the bot: the staff editor, the customer list and answer, and «جوابم را نگرفتم»."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, MessageEntity
from sqlalchemy import select

from app.bot.handlers import faq as customer
from app.bot.handlers.faq_editor import Actor, handle_callback, handle_message
from app.bot.handlers.support import SupportStates
from app.bot.nav_inline import support_hub_keyboard
from app.db.models import FaqItem, Setting
from app.services import faq
from app.services.faq import FaqDraft
from app.services.users import clear_settings_cache
from tests.postgres_support import PostgresTestCase, add_shop_users

OWNER = Actor(audience="a", reseller_id=None)
SHOP2 = Actor(audience="r", reseller_id=2)
CHAT = 500
PREMIUM_ID = "5368324170671202286"


def _callback(data: str) -> MagicMock:
    # ``spec`` so ``ask_text`` treats it as a callback and edits its message.
    callback = MagicMock(spec=CallbackQuery)
    callback.data = data
    callback.answer = AsyncMock()
    callback.bot = AsyncMock()
    callback.message = MagicMock(message_id=10, chat=MagicMock(id=CHAT))
    callback.message.edit_text = AsyncMock()
    callback.message.answer = AsyncMock()
    return callback


def _message(text: str, *, html_text: str | None = None, entities: list[MessageEntity] | None = None) -> MagicMock:
    message = MagicMock(text=text, html_text=html_text or text, entities=entities, chat=MagicMock(id=CHAT), message_id=20)
    message.bot = AsyncMock()
    message.answer = AsyncMock()
    message.delete = AsyncMock()
    return message


def _shown(callback: MagicMock) -> str:
    call = callback.message.edit_text.await_args
    return call.args[0] if call.args else call.kwargs["text"]


def _markup(callback: MagicMock):
    return callback.message.edit_text.await_args.kwargs["reply_markup"]


def _data(callback: MagicMock) -> list[str]:
    return [b.callback_data for row in _markup(callback).inline_keyboard for b in row]


class FaqBotCase(PostgresTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await add_shop_users(self.Session, 2, 3)
        clear_settings_cache()
        self.addCleanup(clear_settings_cache)
        self.state = FSMContext(MemoryStorage(), StorageKey(bot_id=77, chat_id=CHAT, user_id=CHAT))

    async def tap(self, actor: Actor, data: str) -> MagicMock:
        callback = _callback(data)
        async with self.Session() as session:
            await handle_callback(callback, session, self.state, actor)
        return callback

    async def send(self, actor: Actor, message: MagicMock) -> MagicMock:
        async with self.Session() as session:
            await handle_message(message, session, self.state, actor)
        return message

    async def items(self, shop: int | None = None) -> list[FaqItem]:
        async with self.Session() as session:
            return await faq.list_faq_items(session, reseller_id=shop)

    async def seed(self, shop: int | None, question: str = "چطور تمدید کنم؟", **kw) -> FaqItem:
        async with self.Session() as session:
            return await faq.create_faq_item(session, FaqDraft(question=question, answer="از <b>سرویس‌ها</b>", **kw), reseller_id=shop)


class FaqEditorTests(FaqBotCase):
    async def test_wizard_creates_a_question_with_premium_icon_and_formatted_answer(self) -> None:
        premium = [MessageEntity(type="custom_emoji", offset=0, length=1, custom_emoji_id=PREMIUM_ID)]
        await self.tap(OWNER, "fqe:a:n")
        await self.send(OWNER, _message("⭐ لینک کار نمی‌کند؟", entities=premium))
        await self.send(OWNER, _message("QR بگیرید", html_text="<b>QR</b> بگیرید"))
        [item] = await self.items()
        self.assertEqual(item.question, "لینک کار نمی‌کند؟")
        self.assertEqual(item.icon_custom_emoji_id, PREMIUM_ID)
        self.assertEqual(item.answer, "<b>QR</b> بگیرید")
        self.assertIsNone(await self.state.get_state())

    async def test_broken_answer_is_refused_and_the_prompt_stays_open(self) -> None:
        await self.tap(OWNER, "fqe:a:n")
        await self.send(OWNER, _message("سوال"))
        reply = await self.send(OWNER, _message("x", html_text="<b>باز"))
        reply.answer.assert_awaited()
        self.assertEqual(await self.items(), [])
        self.assertIsNotNone(await self.state.get_state())

    async def test_color_toggle_and_order_from_the_card(self) -> None:
        first = await self.seed(None, "اول")
        second = await self.seed(None, "دوم")
        await self.tap(OWNER, f"fqe:a:s:{first.id}:s")
        await self.tap(OWNER, f"fqe:a:e:{first.id}")
        await self.tap(OWNER, f"fqe:a:o:{second.id}:u")
        items = await self.items()
        self.assertEqual([i.id for i in items], [second.id, first.id])
        stored = next(i for i in items if i.id == first.id)
        self.assertEqual((stored.button_style, stored.is_active), ("success", False))

    async def test_unknown_color_code_changes_nothing(self) -> None:
        item = await self.seed(None)
        callback = await self.tap(OWNER, f"fqe:a:s:{item.id}:z")
        callback.answer.assert_awaited_with(customer.feedback.INVALID, show_alert=True)
        self.assertIsNone((await self.items())[0].button_style)

    async def test_delete_needs_the_confirm_step(self) -> None:
        item = await self.seed(None)
        ask = await self.tap(OWNER, f"fqe:a:x:{item.id}")
        self.assertIn(f"fqe:a:X:{item.id}", _data(ask))
        self.assertEqual(len(await self.items()), 1)
        await self.tap(OWNER, f"fqe:a:X:{item.id}")
        self.assertEqual(await self.items(), [])

    async def test_shop_editor_cannot_touch_another_shop_or_the_platform(self) -> None:
        platform = await self.seed(None, "پلتفرم")
        other = await self.seed(3, "فروشگاه ۳")
        for item in (platform, other):
            for view in ("g", "e", "x", "X", "p"):
                callback = await self.tap(SHOP2, f"fqe:r:{view}:{item.id}")
                callback.answer.assert_awaited_with(faq.NOT_FOUND, show_alert=True)
            await self.tap(SHOP2, f"fqe:r:b:{item.id}")
            self.assertIsNone(await self.state.get_state())
        self.assertEqual([i.question for i in await self.items()], ["پلتفرم"])
        self.assertTrue((await self.items(3))[0].is_active)

    async def test_question_html_is_escaped_on_the_card(self) -> None:
        item = await self.seed(None, "<b>x</b>?")
        card = await self.tap(OWNER, f"fqe:a:g:{item.id}")
        self.assertIn("&lt;b&gt;x&lt;/b&gt;?", _shown(card))


class FaqCustomerTests(FaqBotCase):
    async def open(self, handler, data: str, shop: int | None, **kw) -> MagicMock:
        callback = _callback(data)
        async with self.Session() as session:
            await handler(callback, session, reseller_owner_id=shop, **kw)
        return callback

    async def test_list_shows_only_this_shops_active_questions_in_their_colors(self) -> None:
        shown = await self.seed(2, "دیده می‌شود", button_style="danger", icon_custom_emoji_id=PREMIUM_ID)
        await self.seed(2, "پنهان", is_active=False)
        await self.seed(3, "فروشگاه دیگر")
        callback = await self.open(customer.faq_list, "faq:l", 2)
        buttons = [b for row in _markup(callback).inline_keyboard for b in row]
        self.assertEqual(buttons[0].callback_data, f"faq:v:{shown.id}")
        self.assertEqual(buttons[0].style, "danger")
        self.assertEqual(buttons[0].icon_custom_emoji_id, PREMIUM_ID)
        self.assertEqual(len(buttons), 2)

    async def test_inherited_color_comes_from_the_colors_setting(self) -> None:
        await self.seed(None)
        callback = await self.open(customer.faq_list, "faq:l", None)
        self.assertEqual(_markup(callback).inline_keyboard[0][0].style, "primary")

    async def test_answer_counts_a_view_and_forged_ids_read_as_not_found(self) -> None:
        item = await self.seed(2)
        callback = await self.open(customer.faq_answer, f"faq:v:{item.id}", 2)
        self.assertIn("<b>سرویس‌ها</b>", _shown(callback))
        for data, shop in ((f"faq:v:{item.id}", 3), (f"faq:v:{item.id}", None), ("faq:v:abc", 2), ("faq:v:99999999999", 2)):
            forged = await self.open(customer.faq_answer, data, shop)
            forged.answer.assert_awaited_with(faq.NOT_FOUND, show_alert=True)
        self.assertEqual((await self.items(2))[0].view_count, 1)

    async def test_not_answered_opens_a_ticket_with_the_question_as_subject(self) -> None:
        item = await self.seed(2, "<i>تمدید</i>؟")
        callback = await self.open(customer.faq_ticket, f"faq:t:{item.id}", 2, state=self.state)
        self.assertEqual(await self.state.get_state(), SupportStates.body.state)
        self.assertEqual((await self.state.get_data())["subject"], "❓ <i>تمدید</i>؟")
        self.assertIn("&lt;i&gt;", _shown(callback))
        self.assertEqual((await self.items(2))[0].ticket_count, 1)

    async def test_not_answered_on_another_shops_question_starts_nothing(self) -> None:
        item = await self.seed(3)
        await self.open(customer.faq_ticket, f"faq:t:{item.id}", 2, state=self.state)
        self.assertIsNone(await self.state.get_state())

    async def test_old_entry_falls_back_to_the_single_faq_text_until_questions_exist(self) -> None:
        async with self.Session() as session:
            session.add(Setting(key="faq_text", value="متن قدیمی"))
            await session.commit()
        clear_settings_cache()
        legacy = await self.open(customer.faq_list, "help:faq", None)
        self.assertIn("متن قدیمی", _shown(legacy))
        await self.seed(None)
        clear_settings_cache()
        fresh = await self.open(customer.faq_list, "help:faq", None)
        self.assertEqual(_markup(fresh).inline_keyboard[0][0].callback_data.split(":")[:2], ["faq", "v"])

    async def test_support_hub_shows_the_faq_button_only_when_asked(self) -> None:
        with_faq = support_hub_keyboard({}, show_faq=True)
        without = support_hub_keyboard({})
        self.assertEqual(with_faq.inline_keyboard[0][0].callback_data, "faq:l")
        self.assertNotIn("faq:l", [b.callback_data for row in without.inline_keyboard for b in row])

    async def test_current_shop_has_faq_follows_the_bot_shop(self) -> None:
        from app.services.users import reset_shop_reseller_id, set_shop_reseller_id

        await self.seed(2)
        for shop, expected in ((2, True), (3, False), (None, False)):
            token = set_shop_reseller_id(shop)
            try:
                async with self.Session() as session:
                    self.assertEqual(await faq.current_shop_has_faq(session), expected)
            finally:
                reset_shop_reseller_id(token)

    async def test_stored_rows_never_leak_between_shops(self) -> None:
        await self.seed(2)
        async with self.Session() as session:
            shops = set(await session.scalars(select(FaqItem.reseller_id)))
        self.assertEqual(shops, {2})
