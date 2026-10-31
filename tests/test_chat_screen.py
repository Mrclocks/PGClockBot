"""One keyboard, one screen: the rules every message the bot sends in a private chat follows.

The flow tests drive the real bot through ``tests.chat_sim`` and check what the person sees.
"""

from __future__ import annotations

import unittest
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.methods import SendMessage
from aiogram.types import ReplyKeyboardMarkup
from sqlalchemy import select

from app.bot import chat_cleanup, chat_screen
from app.bot import keyboards as kb
from app.bot.chat_cleanup import ThrowawayLog
from app.db.models import BotUser, Plan
from app.services.chat_records import keep_sent
from app.services.users import clear_settings_cache
from tests.chat_sim import ChatSim
from tests.test_chat_cleanup import _Chat

ADMIN_PANEL = "🛠 پنل ادمین"


def _main_keyboard() -> ReplyKeyboardMarkup:
    return kb.main_reply_keyboard("user", ui={})


class _Rules(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        for name, value in (("_log", ThrowawayLog()), ("_homes", ThrowawayLog())):
            patcher = patch.object(chat_cleanup, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(chat_screen, "_shown_keyboard", OrderedDict())
        patcher.start()
        self.addCleanup(patcher.stop)
        token = chat_cleanup._cleanup_on.set(True)
        self.addCleanup(chat_cleanup._cleanup_on.reset, token)
        self.chat = _Chat(chat_id=100)

    async def send(self, text: str, markup=None, *, chat_id: int = 100):
        sent = self.chat.message()
        sent.chat = SimpleNamespace(id=chat_id, type="private")
        make_request = AsyncMock(return_value=sent)
        method = SendMessage(chat_id=chat_id, text=text, reply_markup=markup)
        with patch.object(chat_screen, "Message", MagicMock):
            await chat_screen.screen_requests(make_request, self.chat.bot, method)
        return sent, make_request.await_args.args[1]


class KeyboardRuleTests(_Rules):
    async def test_a_submenu_keyboard_never_reaches_the_chat(self) -> None:
        _, sent = await self.send("🔎 نتیجه جستجو", kb.admin_resellers_reply_keyboard())
        self.assertIsNone(sent.reply_markup)

    async def test_the_menu_keyboard_is_not_sent_again_while_it_is_shown(self) -> None:
        _, first = await self.send("home", _main_keyboard())
        _, again = await self.send("لغو شد.", _main_keyboard())
        self.assertIsNotNone(first.reply_markup)
        self.assertIsNone(again.reply_markup)

    async def test_a_home_re_attaches_it_and_replaces_the_older_keyboard_message(self) -> None:
        old, _ = await self.send("home", _main_keyboard())
        with chat_screen.fresh_keyboard():
            new, sent = await self.send("home", _main_keyboard())
        self.assertIsNotNone(sent.reply_markup)
        self.assertEqual(self.chat.deleted(), [old.message_id])
        self.assertNotIn(new.message_id, self.chat.deleted())

    async def test_a_changed_menu_keyboard_is_sent_and_becomes_the_only_one(self) -> None:
        old, _ = await self.send("home", _main_keyboard())
        preview = kb.main_reply_keyboard("user", ui={}, as_user=True, preview_exit_action=kb.REPLY_ACTION_ADMIN)
        _, sent = await self.send("پیش‌نمایش", preview)
        self.assertIsNotNone(sent.reply_markup)
        self.assertEqual(self.chat.deleted(), [old.message_id])

    async def test_the_phone_number_step_may_show_its_contact_button(self) -> None:
        from aiogram.types import KeyboardButton

        contact = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📱 ارسال شماره تماس", request_contact=True)]])
        _, sent = await self.send("شماره را بفرستید", contact)
        self.assertIs(sent.reply_markup, contact)
        _, back = await self.send("✅ شماره تأیید شد", _main_keyboard())
        self.assertIsNotNone(back.reply_markup)  # the menu returns after the step

    async def test_the_keyboard_rule_holds_while_cleanup_is_off(self) -> None:
        chat_cleanup._cleanup_on.set(False)
        _, sent = await self.send("x", kb.admin_resellers_reply_keyboard())
        self.assertIsNone(sent.reply_markup)


class TemporaryByDefaultTests(_Rules):
    async def test_the_next_update_that_shows_something_deletes_earlier_messages(self) -> None:
        with chat_screen.turn((1, 100)):
            notice, _ = await self.send("لغو شد.")
        with chat_screen.turn((1, 100)) as active:
            panel, _ = await self.send("panel")
            await chat_screen.end_turn(self.chat.bot, active)
        self.assertEqual(self.chat.deleted(), [notice.message_id])
        self.assertNotIn(panel.message_id, self.chat.deleted())

    async def test_records_and_messages_to_other_chats_stay(self) -> None:
        with chat_screen.turn((1, 100)):
            with keep_sent():
                delivery, _ = await self.send("vless://…")
            await self.send("سفارش جدید", chat_id=200)
        with chat_screen.turn((1, 100)) as active:
            await self.send("panel")
            await chat_screen.end_turn(self.chat.bot, active)
        self.assertEqual(self.chat.deleted(), [])
        self.assertIsNotNone(delivery)

    async def test_an_update_that_shows_nothing_leaves_the_screen_alone(self) -> None:
        with chat_screen.turn((1, 100)):
            await self.send("panel")
        with chat_screen.turn((1, 100)) as active:
            await chat_screen.end_turn(self.chat.bot, active)
        self.chat.bot.delete_messages.assert_not_awaited()


class PhoneStepCancelTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_during_the_purchase_phone_step_brings_the_menu_back(self) -> None:
        from app.bot.handlers import purchase_contact

        message = MagicMock(text="❌ انصراف", answer=AsyncMock())
        with patch.object(purchase_contact, "restore_main_reply", AsyncMock()) as restore:
            await purchase_contact.purchase_contact_hint(message, MagicMock(), MagicMock(), AsyncMock())
        restore.assert_awaited_once()
        message.answer.assert_not_awaited()


class _Flow(unittest.IsolatedAsyncioTestCase):
    owner = True

    async def asyncSetUp(self) -> None:
        self.sim = ChatSim(5001 if self.owner else 6001)
        await self.sim.start(owner=self.owner)
        self.addAsyncCleanup(self.sim.stop)
        for target, value in (
            ("app.bot.middlewares._rate_limited", MagicMock(return_value=False)),
            ("app.bot.auth.platform_can_manage_representatives", AsyncMock(return_value=True)),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        async with self.sim.Session() as session:
            session.add_all([
                BotUser(id=50, telegram_id=6495996073, referral_code="u1", full_name="SID", username="sid_official"),
                BotUser(id=51, telegram_id=7000000001, referral_code="r1", full_name="Sid Reseller",
                        username="sid_res", role="reseller"),
                Plan(id=1, name="بسته اقتصادی", price=100000, duration_days=30, data_limit_gb=10, is_active=True),
            ])
            await session.commit()
        await self.sim.type("/start")
        self.menu = self.sim.telegram.keyboard

    def assert_one_screen(self) -> None:
        """Exactly one keyboard message, the menu unchanged, and only what the last update showed."""
        tel = self.sim.telegram
        visible = tel.visible()
        self.assertEqual(len(tel.keyboard_carriers()), 1, self.sim.screen())
        self.assertEqual(tel.keyboard, self.menu, self.sim.screen())
        others = [m for m in visible if m.keyboard is None]
        self.assertTrue(all(m.from_bot for m in others), self.sim.screen())
        self.assertLessEqual(len(others), 1, self.sim.screen())

    def panel_text(self) -> str:
        return [m for m in self.sim.telegram.visible() if m.keyboard is None][-1].text


class OwnerSearchFlowTests(_Flow):
    async def test_searching_a_customer_keeps_the_menu_and_one_result(self) -> None:
        await self.sim.type(ADMIN_PANEL)
        await self.sim.tap("افراد")
        await self.sim.tap("کاربران")
        await self.sim.tap("جستجو")
        await self.sim.type("Sid")
        self.assert_one_screen()
        self.assertIn("نتیجهٔ جستجو", self.panel_text())
        sent = [m.text for m in self.sim.telegram.visible()]
        self.assertNotIn("منوی اصلی", sent)

    async def test_searching_a_reseller_keeps_the_menu_and_one_result(self) -> None:
        await self.sim.type(ADMIN_PANEL)
        await self.sim.tap("افراد")
        await self.sim.tap("نمایندگان")
        await self.sim.tap("جستجوی نماینده")
        await self.sim.type("Sid")
        self.assert_one_screen()
        self.assertIn("نتیجهٔ جستجو", self.panel_text())

    async def test_opening_the_admin_panel_again_clears_the_old_screen(self) -> None:
        await self.sim.type(ADMIN_PANEL)
        await self.sim.tap("افراد")
        await self.sim.tap("کاربران")
        await self.sim.type(ADMIN_PANEL)
        self.assert_one_screen()
        self.assertIn("پنل ادمین", self.panel_text())

    async def test_every_plan_kind_opens_a_screen(self) -> None:
        for kind in ("تست", "دلخواه", "فروش عمده"):
            await self.sim.type(ADMIN_PANEL)
            await self.sim.tap("محصول")
            await self.sim.tap("پلن‌ها")
            await self.sim.tap("پلن‌های کاربران")
            await self.sim.tap(kind)
            self.assert_one_screen()


class PlanEditFlowTests(_Flow):
    async def test_saving_a_trial_field_redraws_the_trial_screen_in_place(self) -> None:
        await self.sim.type(ADMIN_PANEL)
        await self.sim.tap("محصول")
        await self.sim.tap("پلن‌ها")
        await self.sim.tap("پلن‌های کاربران")
        await self.sim.tap("تست")
        await self.sim.tap("نام")
        await self.sim.type("تست سه روزه")
        self.assert_one_screen()
        self.assertIn("تست سه روزه", self.panel_text())


class CustomerFlowTests(_Flow):
    owner = False

    async def test_typing_a_custom_top_up_shows_only_the_payment_methods(self) -> None:
        await self.sim.type("🟡👛 کیف پول")
        await self.sim.tap("شارژ کیف پول")
        await self.sim.tap("مبلغ دیگر")
        await self.sim.type("150000")
        self.assert_one_screen()
        self.assertIn("روش واریز", self.panel_text())

    async def test_card_payment_details_and_the_receipt_stay_and_lose_stale_buttons(self) -> None:
        from app.db.models import Setting

        async with self.sim.Session() as session:
            session.add_all([Setting(key="pay_card_enabled", value="1"),
                             Setting(key="card_number", value="6037991122334455"), Setting(key="card_holder", value="Ali")])
            await session.commit()
        clear_settings_cache()
        await self.sim.type("🟢🛒 خرید سرویس")
        await self.sim.tap("ثابت")
        await self.sim.tap("اقتصادی")
        await self.sim.tap("کارت به کارت")
        await self.sim.send_photo()
        await self.sim.type("🟡👛 کیف پول")
        texts = [m.text for m in self.sim.telegram.visible()]
        details = next((m for m in self.sim.telegram.visible() if "کارت به کارت" in m.text), None)
        self.assertIsNotNone(details, self.sim.screen())
        self.assertEqual(details.inline, [])  # its «انصراف» no longer applies
        self.assertTrue(any("رسید دریافت شد" in t for t in texts), texts)
        self.assertTrue(any("کیف پول" in t for t in texts), texts)
        self.assertEqual(len(self.sim.telegram.keyboard_carriers()), 1)

    async def test_moving_between_menus_never_piles_up_messages(self) -> None:
        for label in ("🟢🛒 خرید سرویس", "🟡👛 کیف پول", "🟣🎧 پشتیبانی", "⭐ باشگاه مشتریان"):
            await self.sim.type(label)
            self.assert_one_screen()


class OwnerOwnPurchaseTests(_Flow):
    """The Owner buying for themself gets the payment review in their own chat; it must stay."""

    async def _buy_with_card(self) -> None:
        from app.db.models import Setting

        async with self.sim.Session() as session:
            session.add_all([Setting(key="pay_card_enabled", value="1"),
                             Setting(key="card_number", value="6037991122334455"), Setting(key="card_holder", value="Ali")])
            await session.commit()
        clear_settings_cache()
        await self.sim.type("🟢🛒 خرید سرویس")
        await self.sim.tap("ثابت")
        await self.sim.tap("اقتصادی")
        await self.sim.tap("کارت به کارت")
        await self.sim.send_photo()

    def _review(self):
        return next((m for m in self.sim.telegram.visible() if "تأیید" in " ".join(m.buttons())), None)

    async def test_the_review_with_approve_and_reject_stays_after_the_receipt(self) -> None:
        await self._buy_with_card()
        review = self._review()
        self.assertIsNotNone(review, self.sim.screen())
        self.assertTrue(any("رد" in b for b in review.buttons()), review.buttons())

    async def test_the_review_survives_moving_to_another_menu(self) -> None:
        await self._buy_with_card()
        await self.sim.type("🟡👛 کیف پول")
        await self.sim.type("🟣🎧 پشتیبانی")
        self.assertIsNotNone(self._review(), self.sim.screen())

    async def test_approving_from_the_kept_review_settles_the_payment(self) -> None:
        from sqlalchemy import select

        from app.db.models import Payment

        await self._buy_with_card()
        await self.sim.type("🟡👛 کیف پول")
        await self.sim.tap("تأیید")
        async with self.sim.Session() as session:
            statuses = list(await session.scalars(select(Payment.status)))
        self.assertTrue(statuses, "no payment recorded")
        self.assertTrue(all(status != "pending" for status in statuses), statuses)

    async def test_approving_one_listed_receipt_keeps_the_others(self) -> None:
        from app.db.models import Payment

        async with self.sim.Session() as session:
            session.add_all([Payment(id=71, user_id=50, amount=100000, receipt_file_id="r71"),
                             Payment(id=72, user_id=50, amount=200000, receipt_file_id="r72", is_wallet_topup=True)])
            await session.commit()
        await self.sim.type(ADMIN_PANEL)
        panel = self.sim.telegram.visible()[-1]
        await self.sim._tap(panel, "adm:payments")
        listed = [m for m in self.sim.telegram.visible() if m.text.startswith("پرداخت #")]
        self.assertEqual(len(listed), 2, self.sim.screen())
        approve = next(data for text, data in listed[0].inline[0] if "تأیید" in text)
        await self.sim._tap(listed[0], approve)
        self.assertFalse(listed[1].deleted, self.sim.screen())


if __name__ == "__main__":
    unittest.main()


class RestartKeepsMyServicesTests(unittest.IsolatedAsyncioTestCase):
    """«شروع مجدد» rebuilds the same menu the person had, «سرویس‌های من» included."""

    async def _chat_with_a_service(self, *, owner: bool) -> ChatSim:
        from sqlalchemy import select

        from app.db.models import UserService

        sim = ChatSim(5001 if owner else 6001)
        await sim.start(owner=owner)
        self.addAsyncCleanup(sim.stop)
        patcher = patch("app.bot.middlewares._rate_limited", MagicMock(return_value=False))
        patcher.start()
        self.addCleanup(patcher.stop)
        await sim.type("/start")
        async with sim.Session() as session:
            user = (await session.execute(select(BotUser).where(BotUser.telegram_id == sim.telegram_id))).scalar_one()
            session.add(UserService(id=1, bot_user_id=user.id, pg_username="u1", pg_user_id=1))
            await session.commit()
        return sim

    def labels(self, sim: ChatSim) -> list[str]:
        return [label for row in sim.telegram.keyboard for label in row]

    async def test_a_customer_keeps_my_services_after_a_restart(self) -> None:
        sim = await self._chat_with_a_service(owner=False)
        await sim.type("/start")
        await sim.type("🔄 شروع مجدد")
        self.assertTrue(any("سرویس" in label and "من" in label for label in self.labels(sim)), self.labels(sim))

    async def test_an_owner_with_a_service_sees_my_services_and_keeps_the_admin_entry(self) -> None:
        sim = await self._chat_with_a_service(owner=True)
        await sim.type("/start")
        before = self.labels(sim)
        await sim.type("🔄 شروع مجدد")
        self.assertEqual(self.labels(sim), before)
        self.assertTrue(any("سرویس" in label and "من" in label for label in before), before)
        self.assertIn(ADMIN_PANEL, before)

    async def test_the_menu_after_a_purchase_is_the_menu_a_restart_builds(self) -> None:
        from sqlalchemy import select

        from app.bot.menu_nav import build_main_reply_keyboard, buyer_main_reply_keyboard

        sim = await self._chat_with_a_service(owner=True)
        async with sim.Session() as session:
            user = (await session.execute(select(BotUser).where(BotUser.telegram_id == sim.telegram_id))).scalar_one()
            after_purchase, _ = await buyer_main_reply_keyboard(session, user, is_reseller_bot=False, reseller_owner_id=None)
            restart, _, _ = await build_main_reply_keyboard(session, user, is_reseller_bot=False, reseller_owner_id=None)
        self.assertEqual(after_purchase.keyboard, restart.keyboard)


class HomeIsAFreshStartTests(_Flow):
    owner = False

    async def test_back_to_home_leaves_one_welcome_and_nothing_else(self) -> None:
        await self.sim.type("🟡👛 کیف پول")
        await self.sim.tap("بازگشت")
        visible = self.sim.telegram.visible()
        self.assertEqual(len(visible), 1, self.sim.screen())
        self.assertIsNotNone(visible[0].keyboard)

    async def test_restart_after_several_menus_leaves_one_welcome(self) -> None:
        for label in ("🟡👛 کیف پول", "🟣🎧 پشتیبانی", "⭐ باشگاه مشتریان"):
            await self.sim.type(label)
        await self.sim.type("🔄 شروع مجدد")
        self.assertEqual(len(self.sim.telegram.visible()), 1, self.sim.screen())

    async def test_a_notice_rides_on_the_welcome_instead_of_its_own_message(self) -> None:
        from app.bot.menu_nav import restore_main_reply

        tapped = self.sim.telegram.visible()[-1]
        message = self.sim.telegram._message(tapped, user=self.sim.person, bot=self.sim.bot)
        async with self.sim.Session() as session:
            user = (await session.execute(select(BotUser).where(BotUser.telegram_id == self.sim.telegram_id))).scalar_one()
            with chat_cleanup.cleanup_switch(True):  # as inside a real update
                await restore_main_reply(message, session, user, text="✅ تیکت ثبت شد", is_reseller_bot=False, reseller_owner_id=None)
        visible = self.sim.telegram.visible()
        self.assertEqual(len(visible), 1, self.sim.screen())
        self.assertIn("✅ تیکت ثبت شد", visible[0].text)


class MyServicesBackTests(_Flow):
    owner = False

    async def test_my_services_without_services_has_a_way_back(self) -> None:
        from app.db.models import Setting

        async with self.sim.Session() as session:
            session.add(Setting(key="service_import_enabled", value="1"))
            await session.commit()
        clear_settings_cache()
        await self.sim.type("/start")
        label = next(text for row in self.sim.telegram.keyboard for text in row if "سرویس" in text and "من" in text)
        await self.sim.type(label)
        await self.sim.tap("بازگشت")
        self.assertEqual(len(self.sim.telegram.visible()), 1, self.sim.screen())


class AlwaysOpenMenuSettingTests(unittest.TestCase):
    def test_off_by_default_and_on_when_switched(self) -> None:
        self.assertFalse(kb.main_reply_keyboard("user", ui={}).is_persistent)
        self.assertTrue(kb.main_reply_keyboard("user", ui={"menu_keyboard_persistent": "1"}).is_persistent)
        self.assertTrue(kb.persistent_reply_keyboard({"menu_keyboard_persistent": "1"}).is_persistent)

    def test_switching_it_counts_as_a_new_keyboard(self) -> None:
        from app.bot.keyboard_guard import keyboard_fingerprint

        closed = kb.main_reply_keyboard("user", ui={})
        open_ = kb.main_reply_keyboard("user", ui={"menu_keyboard_persistent": "1"})
        self.assertNotEqual(keyboard_fingerprint(closed), keyboard_fingerprint(open_))

    def test_the_menu_form_saves_both_switches(self) -> None:
        from app.services.chat_cleanup_config import chat_cleanup_payload

        self.assertEqual(chat_cleanup_payload({"menu_keyboard_persistent": "1"}),
                         {"chat_cleanup_enabled": "0", "menu_keyboard_persistent": "1"})
        self.assertEqual(chat_cleanup_payload({}), {"chat_cleanup_enabled": "0", "menu_keyboard_persistent": "0"})
