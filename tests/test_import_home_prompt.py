"""The home offer to add a service bought before: who gets it, what it sends, and how it goes away."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendRichMessage

from app.bot import chat_screen
from app.bot import import_prompt as prompt
from app.bot.handlers import service_import as flow
from app.bot.handlers import start
from app.bot.handlers import home_trial
from app.db.models import BotUser, Plan, TrialClaim, UserService
from app.services.trial_offer import offerable_trial_plans
from app.services.service_import_offer import dismiss_offer, is_offer_dismissed
from tests.test_chat_screen import _Rules
from tests.test_service_import import ImportCase

ON = {"service_import_enabled": "1"}
OFF = {"service_import_enabled": "0"}


class OfferRuleTests(unittest.TestCase):
    def test_only_a_customer_without_services_who_did_not_opt_out_gets_it_while_the_feature_is_on(self):
        self.assertTrue(prompt.should_offer_import(ON, has_services=False, dismissed=False))
        self.assertFalse(prompt.should_offer_import(ON, has_services=True, dismissed=False))
        self.assertFalse(prompt.should_offer_import(ON, has_services=False, dismissed=True))
        self.assertFalse(prompt.should_offer_import(OFF, has_services=False, dismissed=False))
        self.assertFalse(prompt.should_offer_import(None, has_services=False, dismissed=False))

    def test_the_add_button_takes_the_colour_set_for_it_and_keeps_its_fixed_label(self):
        for colour in ("success", "primary", "danger"):
            (add,) = prompt.build_import_prompt({**ON, "btn_style_svc_add_existing": colour}).blocks[1].buttons
            self.assertEqual((add.text, add.style), ("اتصال اشتراک قبلی", colour))

    def test_the_message_has_the_note_above_a_single_add_button_and_no_hide_button(self):
        blocks = prompt.build_import_prompt(ON).blocks
        self.assertEqual(blocks[0].text, prompt.HOME_ADD_NOTE)
        self.assertIn("از بخش «سرویس‌های من»", prompt.HOME_ADD_NOTE)
        self.assertEqual(len(blocks), 2)
        (add,) = blocks[1].buttons
        self.assertEqual((add.text, add.callback_data), ("اتصال اشتراک قبلی", prompt.HOME_ADD_CALLBACK))

    def test_the_trial_button_sits_beside_the_add_button_and_nothing_follows_it(self):
        blocks = prompt.build_import_prompt(ON, trial_available=True).blocks
        self.assertEqual(blocks[0].text, prompt.HOME_BOTH_NOTE)
        add, trial = blocks[1].buttons
        self.assertEqual(add.callback_data, prompt.HOME_ADD_CALLBACK)
        self.assertEqual((trial.text, trial.callback_data), ("دریافت تست", prompt.HOME_TRIAL_CALLBACK))
        for data in (prompt.HOME_ADD_CALLBACK, prompt.HOME_TRIAL_CALLBACK, prompt.HOME_HIDE_CALLBACK):
            self.assertLessEqual(len(data.encode()), 64)

    def test_a_shop_without_adding_old_services_offers_the_trial_alone(self):
        blocks = prompt.build_import_prompt(OFF, trial_available=True).blocks
        self.assertEqual(blocks[0].text, prompt.HOME_TRIAL_NOTE)
        (trial,) = blocks[1].buttons
        self.assertEqual(trial.callback_data, prompt.HOME_TRIAL_CALLBACK)

    def test_an_available_trial_is_enough_to_offer_even_with_adding_old_services_off(self):
        self.assertTrue(prompt.should_offer_import(OFF, has_services=False, dismissed=False, trial_available=True))
        self.assertFalse(prompt.should_offer_import(OFF, has_services=True, dismissed=False, trial_available=True))


class SendTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_telegram_refusal_only_skips_the_offer(self):
        message = MagicMock()
        message.chat.id = 5
        message.bot.send_rich_message = AsyncMock(
            side_effect=TelegramBadRequest(method=MagicMock(), message="Bad Request: unsupported")
        )
        self.assertIsNone(await prompt.send_import_prompt(message, ON))

    async def test_it_is_sent_without_a_keyboard(self):
        message = MagicMock()
        message.chat.id = 5
        message.bot.send_rich_message = AsyncMock(return_value="sent")
        self.assertEqual(await prompt.send_import_prompt(message, ON), "sent")
        self.assertNotIn("reply_markup", message.bot.send_rich_message.await_args.kwargs)


class ChatScreenTests(_Rules):
    async def test_a_rich_offer_is_tracked_as_temporary_and_never_touches_the_keyboard(self):
        sent = self.chat.message()
        sent.chat = SimpleNamespace(id=100, type="private")
        make_request = AsyncMock(return_value=sent)
        method = SendRichMessage(chat_id=100, rich_message=prompt.build_import_prompt(ON))
        with patch.object(chat_screen, "Message", MagicMock):
            with chat_screen.turn((int(self.chat.bot.id), 100)) as active:
                await chat_screen.screen_requests(make_request, self.chat.bot, method)
        self.assertIn(int(sent.message_id), active.shown)
        self.assertIsNone(make_request.await_args.args[1].reply_markup)
        self.assertNotIn((int(self.chat.bot.id), 100), chat_screen._shown_keyboard)


class HomeTests(ImportCase):
    async def _home(self, ui: dict, *, user_id: int = 1) -> AsyncMock:
        message = AsyncMock()
        message.chat = SimpleNamespace(id=1, type="private")
        message.answer = AsyncMock(return_value=SimpleNamespace(message_id=1, chat=message.chat, bot=message.bot))
        async with self.Session() as session:
            user = await session.get(BotUser, user_id)
            with (
                patch.object(start, "replace_home", AsyncMock()),
                patch.object(start, "send_import_prompt", AsyncMock()) as offer,
            ):
                await start.render_home(message, session, user, ui={**(await self._ui(session)), **ui})
        message.offer = offer
        return message

    async def _ui(self, session) -> dict:
        from app.services.users import get_all_settings

        return await get_all_settings(session)

    async def test_a_customer_without_services_gets_the_offer_after_the_welcome(self):
        message = await self._home(ON)
        message.answer.assert_awaited_once()
        message.offer.assert_awaited_once()

    async def test_a_trial_alone_is_offered_when_adding_old_services_is_off_and_a_trial_plan_exists(self):
        async with self.Session() as session:
            session.add(Plan(id=900, name="تست", is_trial=True, is_active=True, price=0, duration_days=1))
            await session.commit()
        message = await self._home({**OFF, "trial_enabled": "1"})
        message.offer.assert_awaited_once()
        self.assertTrue(message.offer.await_args.kwargs["trial_available"])

    async def test_a_customer_who_already_took_the_trial_is_not_offered_it_again(self):
        async with self.Session() as session:
            session.add(Plan(id=900, name="تست", is_trial=True, is_active=True, price=0, duration_days=1))
            session.add(TrialClaim(user_id=1, shop_key="platform"))
            await session.commit()
        message = await self._home({**OFF, "trial_enabled": "1"})
        message.offer.assert_not_awaited()

    async def test_a_customer_with_a_service_gets_the_plain_welcome_only(self):
        async with self.Session() as session:
            session.add(UserService(id=801, bot_user_id=1, pg_user_id=801, pg_username="mine", remark="order:1"))
            await session.commit()
        message = await self._home(ON)
        message.answer.assert_awaited_once()
        message.offer.assert_not_awaited()

    async def test_nothing_is_offered_while_the_feature_is_off(self):
        message = await self._home(OFF)
        message.offer.assert_not_awaited()

    async def test_a_customer_who_chose_not_to_see_it_gets_the_plain_welcome_only(self):
        async with self.Session() as session:
            await dismiss_offer(session, 1, None)
        message = await self._home(ON)
        message.answer.assert_awaited_once()
        message.offer.assert_not_awaited()

    async def test_a_service_in_another_shop_does_not_hide_the_offer(self):
        async with self.Session() as session:
            session.add(UserService(id=802, bot_user_id=1, pg_user_id=802, pg_username="other", remark="order:2", shop_id=7))
            await session.commit()
        message = await self._home(ON)
        message.offer.assert_awaited_once()


class ButtonFlowTests(ImportCase):
    async def test_tapping_the_offer_asks_for_the_link_and_removes_the_offer(self):
        callback = SimpleNamespace(data=prompt.HOME_ADD_CALLBACK, message=AsyncMock(), answer=AsyncMock())
        state = AsyncMock()
        async with self.Session() as session:
            user = await session.get(BotUser, 1)
            with (
                patch.object(flow, "get_all_settings", AsyncMock(return_value=ON)),
                patch.object(flow, "ask_text", AsyncMock()) as ask,
            ):
                await flow.svc_add_existing_from_home(callback, session, user, state)
        self.assertFalse(ask.await_args.kwargs.get("edit"))
        callback.message.delete.assert_awaited_once()

    async def test_tapping_it_after_the_feature_was_switched_off_is_refused(self):
        callback = SimpleNamespace(data=prompt.HOME_ADD_CALLBACK, message=AsyncMock(), answer=AsyncMock())
        async with self.Session() as session:
            user = await session.get(BotUser, 1)
            with (
                patch.object(flow, "get_all_settings", AsyncMock(return_value=OFF)),
                patch.object(flow, "ask_text", AsyncMock()) as ask,
            ):
                await flow.svc_add_existing_from_home(callback, session, user, AsyncMock())
        ask.assert_not_awaited()
        callback.message.delete.assert_not_awaited()


class TrialOfferTests(ImportCase):
    async def _add_trial_plan(self) -> None:
        async with self.Session() as session:
            session.add(Plan(id=900, name="تست", is_trial=True, is_active=True, price=0, duration_days=1))
            await session.commit()

    async def test_no_trial_plan_is_offered_while_trials_are_switched_off(self):
        await self._add_trial_plan()
        async with self.Session() as session:
            self.assertEqual(await offerable_trial_plans(session, {"trial_enabled": "0"}, 1), [])
            self.assertEqual([p.id for p in await offerable_trial_plans(session, {"trial_enabled": "1"}, 1)], [900])

    async def test_a_trial_claimed_in_another_shop_does_not_hide_this_shops_trial(self):
        await self._add_trial_plan()
        async with self.Session() as session:
            session.add(TrialClaim(user_id=1, shop_key="7"))
            await session.commit()
            self.assertEqual([p.id for p in await offerable_trial_plans(session, {"trial_enabled": "1"}, 1)], [900])

    async def test_tapping_the_trial_button_hands_the_single_plan_to_the_shop_purchase_and_removes_the_offer(self):
        await self._add_trial_plan()
        callback = SimpleNamespace(data=prompt.HOME_TRIAL_CALLBACK, message=AsyncMock(), answer=AsyncMock())
        callback.model_copy = lambda update: SimpleNamespace(**{**vars(callback), **update})
        async with self.Session() as session:
            user = await session.get(BotUser, 1)
            with (
                patch.object(home_trial, "get_all_settings", AsyncMock(return_value={"trial_enabled": "1"})),
                patch.object(home_trial, "shop_buy", AsyncMock()) as buy,
            ):
                await home_trial.home_get_trial(callback, session, user, AsyncMock())
        self.assertEqual(buy.await_args.args[0].data, "shop:buy:900")
        callback.message.delete.assert_awaited_once()

    async def test_tapping_it_after_the_trial_was_taken_is_refused(self):
        await self._add_trial_plan()
        callback = SimpleNamespace(data=prompt.HOME_TRIAL_CALLBACK, message=AsyncMock(), answer=AsyncMock())
        async with self.Session() as session:
            session.add(TrialClaim(user_id=1, shop_key="platform"))
            await session.commit()
            user = await session.get(BotUser, 1)
            with (
                patch.object(home_trial, "get_all_settings", AsyncMock(return_value={"trial_enabled": "1"})),
                patch.object(home_trial, "shop_buy", AsyncMock()) as buy,
            ):
                await home_trial.home_get_trial(callback, session, user, AsyncMock())
        buy.assert_not_awaited()
        callback.answer.assert_awaited_once()


class HideOfferTests(ImportCase):
    def _tap(self):
        return SimpleNamespace(data=prompt.HOME_HIDE_CALLBACK, message=AsyncMock(), answer=AsyncMock())

    async def test_choosing_not_to_see_it_is_remembered_and_removes_the_message(self):
        callback = self._tap()
        async with self.Session() as session:
            user = await session.get(BotUser, 1)
            await flow.svc_hide_home_offer(callback, session, user)
            self.assertTrue(await is_offer_dismissed(session, 1, None))
        callback.message.delete.assert_awaited_once()
        callback.answer.assert_awaited_once()

    async def test_tapping_twice_keeps_one_record_and_does_not_fail(self):
        async with self.Session() as session:
            user = await session.get(BotUser, 1)
            await flow.svc_hide_home_offer(self._tap(), session, user)
            await flow.svc_hide_home_offer(self._tap(), session, user)
            self.assertTrue(await is_offer_dismissed(session, 1, None))

    async def test_the_choice_belongs_to_one_customer_in_one_shop(self):
        async with self.Session() as session:
            await dismiss_offer(session, 1, 7)
            self.assertTrue(await is_offer_dismissed(session, 1, 7))
            self.assertFalse(await is_offer_dismissed(session, 1, None))
            self.assertFalse(await is_offer_dismissed(session, 1, 8))
            self.assertFalse(await is_offer_dismissed(session, 2, 7))


if __name__ == "__main__":
    unittest.main()
