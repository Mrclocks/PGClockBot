"""Buttons under an FAQ answer (the group-message model, in the bot and on the web) and «بازگشت» one step
back on every screen, including the shared button picker."""

from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import sqlalchemy as sa
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from app.bot import message_button_picker as picker
from app.bot.handlers import faq as customer
from app.bot.handlers import faq_editor as editor
from app.bot.nav_input import CANCEL_CODE_KEY, PROMPT_TEXT_KEY, RETURN_TO_KEY, cancel_callback
from app.db.models import BotUser, FaqItem, Plan, ResellerProfile
from app.services import faq
from app.services.faq import FaqDraft, FaqError
from app.services.message_buttons import ButtonInput, stored_inputs
from app.services.users import clear_settings_cache
from tests.postgres_support import PostgresTestCase, scoped_sync_engine
from tests.test_faq_pages import FORM, OWNER, SHOP_STAFF, FaqPageCase

ROOT = Path(__file__).resolve().parents[1]
SHOP = 41
OTHER = 42
CHAT = 500


def _state() -> FSMContext:
    return FSMContext(MemoryStorage(), StorageKey(bot_id=77, chat_id=CHAT, user_id=CHAT))


def _callback(data: str) -> MagicMock:
    callback = MagicMock(spec=CallbackQuery)
    callback.data = data
    callback.answer = AsyncMock()
    callback.message = MagicMock(spec=Message)
    callback.message.edit_text = AsyncMock()
    callback.message.edit_reply_markup = AsyncMock()
    callback.message.answer = AsyncMock()
    callback.message.chat = MagicMock(id=CHAT)
    callback.message.message_id = 10
    return callback


def _markup(callback: MagicMock):
    return callback.message.edit_text.await_args.kwargs["reply_markup"]


def _data(markup) -> list[list[str]]:
    return [[b.callback_data for b in row] for row in markup.inline_keyboard]


class ButtonsCase(PostgresTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        clear_settings_cache()
        self.addCleanup(clear_settings_cache)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=SHOP, telegram_id=9041, referral_code="s41", role="reseller"),
                BotUser(id=OTHER, telegram_id=9042, referral_code="s42", role="reseller"),
            ])
            await session.flush()
            session.add_all([
                ResellerProfile(user_id=SHOP, is_active=True, bot_permissions="[]"),
                Plan(id=7, name="پلن ۴۱", price=1000, owner_reseller_id=SHOP, is_active=True),
                Plan(id=8, name="پلن ۴۲", price=1000, owner_reseller_id=OTHER, is_active=True),
            ])
            await session.commit()

    async def seed(self, shop: int | None, buttons: list[ButtonInput] | None = None) -> FaqItem:
        async with self.Session() as session:
            stored = await faq.clean_answer_buttons(session, buttons or [], reseller_id=shop)
            return await faq.create_faq_item(
                session, FaqDraft(question="چطور تمدید کنم؟", answer="از سرویس‌ها", buttons=stored), reseller_id=shop
            )


class ServiceTests(ButtonsCase):
    async def test_answer_buttons_follow_the_group_message_rules_of_the_shop(self) -> None:
        async with self.Session() as session:
            stored = await faq.clean_answer_buttons(
                session, [ButtonInput("shop", style="success"), ButtonInput("plan", plan="7")], reseller_id=SHOP
            )
            self.assertEqual([i.target for i in stored_inputs(stored)], ["shop", "plan"])
            for bad in ([ButtonInput("link", "سایت", "https://a.example")],  # no link permission for this shop
                        [ButtonInput("plan", plan="8")],  # another shop's plan
                        [ButtonInput("renew")],  # an FAQ has no single service to renew
                        [ButtonInput("shop")] * 5):  # at most four
                with self.assertRaises(FaqError):
                    await faq.clean_answer_buttons(session, bad, reseller_id=SHOP)
            self.assertIsNone(await faq.clean_answer_buttons(session, [], reseller_id=SHOP))

    async def test_a_plan_no_longer_sold_is_dropped_when_shown(self) -> None:
        item = await self.seed(SHOP, [ButtonInput("plan", plan="7"), ButtonInput("support")])
        async with self.Session() as session:
            await session.execute(sa.update(Plan).where(Plan.id == 7).values(is_active=False))
            await session.commit()
            rows = await faq.answer_button_rows(session, item.buttons, reseller_id=SHOP)
        self.assertEqual(len([b for row in rows for b in row]), 1)

    async def test_toggling_a_question_keeps_its_buttons(self) -> None:
        item = await self.seed(SHOP, [ButtonInput("shop")])
        async with self.Session() as session:
            changed = await faq.change_faq_item(session, item.id, reseller_id=SHOP, is_active=False)
        self.assertEqual(changed.buttons, item.buttons)


class CustomerTests(ButtonsCase):
    async def open(self, handler, data: str, shop: int | None, **kw) -> MagicMock:
        callback = _callback(data)
        async with self.Session() as session:
            await handler(callback, session, reseller_owner_id=shop, **kw)
        return callback

    async def test_answer_shows_its_buttons_then_not_answered_then_back_to_the_list(self) -> None:
        item = await self.seed(SHOP, [ButtonInput("shop"), ButtonInput("support"), ButtonInput("services")])
        callback = await self.open(customer.faq_answer, f"faq:v:{item.id}", SHOP)
        rows = _data(_markup(callback))
        self.assertEqual(len(rows[0]), 2)  # two to a row, as under a group message
        self.assertEqual(rows[-2], [f"faq:t:{item.id}"])
        self.assertEqual(rows[-1], ["faq:l"])

    async def test_back_from_the_ticket_prompt_shows_the_answer_without_counting_a_view(self) -> None:
        item = await self.seed(SHOP)
        state = _state()
        prompt = await self.open(customer.faq_ticket, f"faq:t:{item.id}", SHOP, state=state)
        data = await state.get_data()
        self.assertEqual(data[RETURN_TO_KEY], f"faq:b:{item.id}")
        back = _markup(prompt).inline_keyboard[-1][0]
        self.assertEqual(back.callback_data, cancel_callback(customer.TICKET_CANCEL_CODE))
        await self.open(customer.faq_answer_again, f"faq:b:{item.id}", SHOP)
        forged = await self.open(customer.faq_answer_again, f"faq:b:{item.id}", OTHER)
        forged.answer.assert_awaited_with(faq.NOT_FOUND, show_alert=True)
        self.assertEqual((await self.reload(item.id)).view_count, 0)

    async def test_list_goes_back_to_support(self) -> None:
        await self.seed(SHOP)
        callback = await self.open(customer.faq_list, "faq:l", SHOP)
        self.assertEqual(_data(_markup(callback))[-1], ["nv:s:home"])

    async def reload(self, item_id: int) -> FaqItem:
        async with self.Session() as session:
            return await session.get(FaqItem, item_id)


class EditorTests(ButtonsCase):
    async def test_card_opens_the_picker_filled_and_saving_stores_the_choice(self) -> None:
        item = await self.seed(SHOP, [ButtonInput("support", style="danger")])
        actor = editor.Actor(audience="r", reseller_id=SHOP)
        state = _state()
        async with self.Session() as session:
            card = _callback(f"fqe:r:g:{item.id}")
            await editor.handle_callback(card, session, state, actor)
            self.assertIn(f"fqe:r:k:{item.id}", [d for row in _data(_markup(card)) for d in row])
            opened = _callback(f"fqe:r:k:{item.id}")
            await editor.handle_callback(opened, session, state, actor)
        data = await state.get_data()
        self.assertEqual((data["mbp_selected"], data["mbp_styles"]), (["support"], {"support": "danger"}))
        self.assertEqual(_data(_markup(opened))[-1], [picker.CB_BACK])
        finish, _back = editor._picker_flow("r")
        done = _callback("mbp:s")
        async with self.Session() as session:
            with patch.object(editor, "_shop_actor", AsyncMock(return_value=actor)):
                await finish(done, state, session, BotUser(id=SHOP), [ButtonInput("shop"), ButtonInput("plan", plan="7")])
        self.assertEqual([i.target for i in stored_inputs((await self.reload(item.id)).buttons)], ["shop", "plan"])
        self.assertIsNone(await state.get_state())

    async def test_a_forged_item_from_another_shop_is_not_saved(self) -> None:
        other = await self.seed(OTHER, [ButtonInput("support")])
        actor = editor.Actor(audience="r", reseller_id=SHOP)
        state = _state()
        await state.update_data(**{editor._ITEM_KEY: other.id})
        finish, _back = editor._picker_flow("r")
        async with self.Session() as session:
            with patch.object(editor, "_shop_actor", AsyncMock(return_value=actor)):
                await finish(_callback("mbp:s"), state, session, BotUser(id=SHOP), [ButtonInput("shop")])
        self.assertEqual([i.target for i in stored_inputs((await self.reload(other.id)).buttons)], ["support"])

    async def test_owner_flow_refuses_someone_who_is_not_the_owner(self) -> None:
        item = await self.seed(None, [ButtonInput("support")])
        state = _state()
        await state.update_data(**{editor._ITEM_KEY: item.id})
        finish, _back = editor._picker_flow("a")
        async with self.Session() as session:
            with patch.object(editor, "require_bot_owner", AsyncMock(return_value=False)):
                await finish(_callback("mbp:s"), state, session, BotUser(id=SHOP), [])
        self.assertIsNotNone((await self.reload(item.id)).buttons)

    async def test_typing_steps_have_back_instead_of_cancel(self) -> None:
        item = await self.seed(SHOP)
        actor = editor.Actor(audience="r", reseller_id=SHOP)
        async with self.Session() as session:
            prompt = _callback(f"fqe:r:b:{item.id}")
            await editor.handle_callback(prompt, session, _state(), actor)
        back = _markup(prompt).inline_keyboard[-1][0]
        self.assertEqual(back.callback_data, cancel_callback("fqe_r"))
        self.assertNotIn("انصراف", back.text)

    async def reload(self, item_id: int) -> FaqItem:
        async with self.Session() as session:
            return await session.get(FaqItem, item_id)


class PickerBackTests(unittest.IsolatedAsyncioTestCase):
    def test_every_screen_ends_with_the_shared_back_on_its_own_row(self) -> None:
        checklist = picker.checklist_markup([], renew=False, presets=[], back=picker.CB_QUESTION)
        self.assertEqual(_data(checklist)[-1], [picker.CB_QUESTION])
        colors = picker.colors_markup(["shop"], {}, labels={"shop": "خرید"}, send_label="✅ ذخیرهٔ دکمه‌ها")
        self.assertEqual(_data(colors)[-2:], [[picker.CB_SEND], [picker.CB_YES]])
        self.assertEqual(colors.inline_keyboard[-2][0].text, "✅ ذخیرهٔ دکمه‌ها")
        self.assertEqual(_data(picker.question_markup(has_back=True))[-1], [picker.CB_BACK])
        self.assertNotIn([picker.CB_BACK], _data(picker.question_markup()))

    async def test_question_back_asks_the_text_prompt_again(self) -> None:
        state = _state()
        await state.set_state("AdminStates:broadcast_text")
        await state.update_data(**{PROMPT_TEXT_KEY: "متن پیام:", CANCEL_CODE_KEY: "adm_bc", RETURN_TO_KEY: None})
        message = MagicMock(spec=Message)
        message.chat = MagicMock(id=CHAT)
        message.message_id = 3
        message.edit_text = AsyncMock()
        with patch.object(picker, "finish_text_step", AsyncMock()):
            await picker.ask_for_buttons(message, state, flow="x", shop_id=None)
        self.assertIsNotNone((await state.get_data())["mbp_text_step"])
        back = _callback(picker.CB_BACK)
        await picker.on_back(back, state, MagicMock(), BotUser(id=1))
        self.assertEqual(await state.get_state(), "AdminStates:broadcast_text")
        self.assertEqual(back.message.edit_text.await_args.args[0], "متن پیام:")

    async def test_link_url_back_returns_to_the_link_text(self) -> None:
        state = _state()
        await state.set_state(picker.PickerStates.link_url)
        back = _callback(picker.CB_LINK_BACK)
        await picker.on_link_url_back(back, state)
        self.assertEqual(await state.get_state(), picker.PickerStates.link_label.state)
        self.assertEqual(_data(_markup(back))[-1], [picker.CB_YES])

    def test_saved_buttons_fill_the_checklist(self) -> None:
        selected, styles, links = picker.initial_choice([
            ButtonInput("shop", style="success"), ButtonInput("link", "سایت", "https://a.example"),
            ButtonInput("plan", "پلن", plan="7"), ButtonInput("renew"),
        ])
        self.assertEqual(selected, ["shop", "u1", "u2"])
        self.assertEqual(styles["shop"], "success")
        chosen = picker.chosen_inputs(selected, styles, links)
        self.assertEqual([(i.target, i.plan, i.url) for i in chosen],
                         [("shop", "", ""), ("link", "", "https://a.example"), ("plan", "7", "")])


class WebTests(FaqPageCase):
    async def test_buttons_are_saved_and_shown_again_in_the_edit_form(self) -> None:
        data = {**FORM, "btn_target": ["shop", "support"], "btn_label": ["", "پشتیبانی"], "btn_url": ["", ""],
                "btn_style": ["success", ""], "btn_plan": ["", ""]}
        response = await self.call(SHOP_STAFF, "POST", "/faq", data=data)
        self.assertEqual(response.headers["location"], "/faq?ok=created")
        [item] = await self.items(41)
        self.assertEqual([(i.target, i.style) for i in stored_inputs(item.buttons)], [("shop", "success"), ("support", "")])
        page = await self.call(SHOP_STAFF, "GET", "/faq")
        self.assertIn("data-initial=", page.text)
        self.assertIn("data-msg-btn-list", page.text)

    async def test_a_link_without_the_permission_is_refused_and_the_rows_come_back(self) -> None:
        data = {**FORM, "btn_target": ["link"], "btn_label": ["سایت"], "btn_url": ["https://a.example"],
                "btn_style": [""], "btn_plan": [""]}
        response = await self.call(SHOP_STAFF, "POST", "/faq", data=data)
        self.assertEqual(response.status_code, 400)
        self.assertIn("https://a.example", response.text)
        self.assertEqual(await self.items(41), [])
        owner = await self.call(OWNER, "POST", "/faq", data=data)
        self.assertEqual(owner.status_code, 303)
        self.assertEqual(stored_inputs((await self.items(None))[0].buttons)[0].url, "https://a.example")


class MigrationTests(unittest.TestCase):
    def test_upgrade_twice_then_downgrade_keeps_questions(self) -> None:
        spec = importlib.util.spec_from_file_location("faq_buttons_mig", ROOT / "alembic/versions/0140_faq_buttons.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = scoped_sync_engine()
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE faq_items (id INTEGER PRIMARY KEY, question TEXT)"))
            conn.execute(text("INSERT INTO faq_items VALUES (1, 'س')"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                self.assertIn("buttons", {c["name"] for c in sa.inspect(conn).get_columns("faq_items")})
                conn.execute(text("UPDATE faq_items SET buttons = :b"), {"b": json.dumps([{"k": "screen", "l": "", "t": "shop"}])})
                migration.downgrade()
                self.assertNotIn("buttons", {c["name"] for c in sa.inspect(conn).get_columns("faq_items")})
            self.assertEqual(conn.execute(text("SELECT question FROM faq_items")).scalar(), "س")
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
