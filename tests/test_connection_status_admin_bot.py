"""Monitoring callbacks acknowledge promptly, validate IDs and keep FSM navigation safe."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, User

from connection_monitor_support import MonitorDatabaseCase

from app.bot import keyboards
from app.bot.nav_inline import admin_groups_hub_keyboard
from app.bot.connection_status_admin_ui import parse_monitor_callback
from app.bot.connection_status_admin_ui import monitor_error
from app.services.connection_monitor_accounts import ACCOUNT_MESSAGES, MonitorAccountError
from app.bot.connection_status_admin_ui import show_monitor_home
from app.bot.connection_status_ui import MonitorAdminLabel, admin_page_keyboard, config_picker_text, target_keyboard
from app.services.connection_status_texts import LEGACY_MONITOR_TEXTS, MONITOR_TEXT_DEFAULTS
from app.bot.handlers import connection_status_admin as admin
from app.bot.handlers import connection_status_admin_input as forms
from app.bot.nav_input import CANCEL_CODE_KEY
from app.db.models import BotUser, ConnectionMonitorTarget
from app.services.connection_monitor_admin import MonitorConfigPage
from app.services.connection_monitor_subscription import subscription_entries
from app.services.connection_monitor_views import monitor_admin_page
from app.services.users import DEFAULT_SETTINGS


class MonitorAdminBotTests(MonitorDatabaseCase):
    async def test_picker_formats_protocol_details_and_uses_bounded_content_key_callbacks(self) -> None:
        native = {"remarks": "<b>My config</b>", "outbounds": [{
            "tag": "proxy", "protocol": "shadowsocks", "settings": {"servers": [{
                "address": "ss.example", "port": 443, "method": "chacha20-ietf-poly1305", "password": "synthetic-only-password",
            }]},
        }]}
        entries = subscription_entries([native])
        text = config_picker_text(MonitorConfigPage(entries, None, 1))
        self.assertIn("shadowsocks · tcp", text)
        self.assertIn("&lt;b&gt;My config&lt;/b&gt;", text)
        self.assertNotIn("synthetic-only-password", text)
        data = "cma:pick:" + entries[0].config_key
        self.assertLessEqual(len(data.encode()), 64)
        self.assertEqual(parse_monitor_callback(data).config_key, entries[0].config_key)
        with self.assertRaises(ValueError):
            parse_monitor_callback("cma:pick:41")

    async def test_admin_panel_has_a_direct_status_button_in_both_navigation_modes(self) -> None:
        ui = {**DEFAULT_SETTINGS, "btn_connection_status_admin": "تست هاست‌ها"}
        reply = keyboards.admin_reply_keyboard(ui)
        self.assertIn("تست هاست‌ها", [button.text for row in reply.keyboard for button in row])
        inline = admin_groups_hub_keyboard(ui)
        entry = next(button for row in inline.inline_keyboard for button in row if button.text == "تست هاست‌ها")
        self.assertEqual(entry.callback_data, "cma:home:0")
        with patch.object(CallbackQuery, "answer", new_callable=AsyncMock), \
             patch.object(admin, "bot_monitor_access", AsyncMock(return_value=self.access)), \
             patch.object(admin, "show_monitor_home", new_callable=AsyncMock) as show:
            async with self.Session() as session:
                await admin.monitor_admin_callback(self.callback(entry.callback_data), session, self.user, self.state)
            self.assertEqual(show.await_count, 1)
            self.assertEqual(show.call_args.kwargs, {"after_id": 0})

    async def test_customer_and_reseller_home_do_not_offer_private_status_controls(self) -> None:
        for role in ("user", "reseller"):
            with self.subTest(role=role):
                reply = keyboards.main_reply_keyboard(role, ui=dict(DEFAULT_SETTINGS))
                self.assertNotIn(DEFAULT_SETTINGS["btn_connection_status_admin"], [button.text for row in reply.keyboard for button in row])

    async def test_direct_admin_button_rechecks_access_before_reading_private_status(self) -> None:
        with patch.object(CallbackQuery, "answer", new_callable=AsyncMock), \
             patch.object(Message, "answer", new_callable=AsyncMock), \
             patch.object(admin, "bot_monitor_access", AsyncMock(side_effect=ValueError("دسترسی مجاز نیست"))) as authorize, \
             patch.object(admin, "show_monitor_home", new_callable=AsyncMock) as show:
            async with self.Session() as session:
                await admin.monitor_admin_callback(self.callback("cma:home:0"), session, self.user, self.state, is_reseller_bot=True)
            self.assertTrue(authorize.call_args.kwargs["is_reseller_bot"])
            show.assert_not_called()

    async def test_old_default_menu_button_still_opens_settings_without_overriding_custom_labels(self) -> None:
        old_button = self.message.model_copy(update={"text": LEGACY_MONITOR_TEXTS["btn_connection_status_admin"]})
        matcher = MonitorAdminLabel()
        async with self.Session() as session:
            with patch("app.bot.connection_status_ui.get_all_settings", AsyncMock(return_value=MONITOR_TEXT_DEFAULTS)):
                self.assertTrue(await matcher(old_button, session))
            with patch("app.bot.connection_status_ui.get_all_settings", AsyncMock(return_value={"btn_connection_status_admin": "هاست‌ها"})):
                self.assertFalse(await matcher(old_button, session))
                self.assertTrue(await matcher(self.message.model_copy(update={"text": "هاست‌ها"}), session))

    async def test_account_validation_failure_shows_the_actionable_reason(self) -> None:
        with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
            async with self.Session() as session:
                await monitor_error(self.message, session, MonitorAccountError("inactive"))
        self.assertEqual(answer.call_args.args[0], ACCOUNT_MESSAGES["inactive"])

    async def test_monitor_home_ends_with_a_back_button_to_the_admin_panel(self) -> None:
        with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
            async with self.Session() as session:
                await show_monitor_home(self.message, session, self.access, edit=False)
        last_row = answer.call_args.kwargs["reply_markup"].inline_keyboard[-1]
        self.assertEqual([button.callback_data for button in last_row], ["nv:adm:home"])
        self.assertEqual(last_row[0].text, "⬅️ بازگشت")

    async def test_back_button_follows_the_admin_configured_label_and_every_callback_stays_bounded(self) -> None:
        async with self.Session() as session:
            overview = await monitor_admin_page(session, self.access, now=datetime.now(timezone.utc))
        markup = admin_page_keyboard(overview, {**DEFAULT_SETTINGS, "btn_back": "برگشت به پنل"})
        self.assertEqual(markup.inline_keyboard[-1][0].text, "برگشت به پنل")
        self.assertTrue(all(len(button.callback_data.encode()) <= 64 for row in markup.inline_keyboard for button in row))

    async def test_config_screens_go_back_to_the_monitor_home(self) -> None:
        stub = SimpleNamespace(id=1, test_enabled=False, config_key=None, is_public=False, public_name="", config_name="")
        self.assertEqual(target_keyboard(stub).inline_keyboard[-1][0].callback_data, "cma:home:0")
        with patch.object(admin, "monitor_config_choices", AsyncMock(return_value=MonitorConfigPage((), None))), \
             patch.object(admin, "safe_edit_text", new_callable=AsyncMock) as render:
            async with self.Session() as session:
                await admin._config_picker(self.message, session, self.access, after_id=0)
        self.assertEqual(render.call_args.kwargs["reply_markup"].inline_keyboard[-1][0].callback_data, "cma:home:0")

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.message = Message(message_id=100, date=datetime.now(timezone.utc), chat=Chat(id=101, type="private"),
                               from_user=User(id=101, is_bot=False, first_name="Owner"), text="نام عمومی")
        self.storage = MemoryStorage()
        self.state = FSMContext(self.storage, StorageKey(bot_id=1, chat_id=101, user_id=101))
        self.user = BotUser(id=1, telegram_id=101, referral_code="owner", role="admin")
        delete = patch.object(Message, "delete", new_callable=AsyncMock)
        delete.start()
        self.addCleanup(delete.stop)

    async def asyncTearDown(self) -> None:
        await self.storage.close()
        await super().asyncTearDown()

    def callback(self, data: str) -> CallbackQuery:
        return CallbackQuery(id="callback", from_user=self.message.from_user, chat_instance="synthetic", data=data, message=self.message)

    async def test_callback_acknowledges_before_a_slow_catalog_operation(self) -> None:
        with patch.object(CallbackQuery, "answer", new_callable=AsyncMock) as answer:
            async def picker(*args: object, **kwargs: object) -> MonitorConfigPage:
                self.assertEqual(answer.await_count, 1)
                return MonitorConfigPage((), None)
            with patch.object(admin, "bot_monitor_access", AsyncMock(return_value=self.access)):
                with patch.object(admin, "monitor_config_choices", side_effect=picker):
                    with patch.object(admin, "safe_edit_text", new_callable=AsyncMock) as render:
                        async with self.Session() as session:
                            await admin.monitor_admin_callback(self.callback("cma:hosts:0"), session, self.user, self.state)
                        self.assertIn("کانفیگ دیگری", render.call_args.args[1])

    async def test_invalid_callback_never_mutates_settings_or_leaks_input(self) -> None:
        invalid = ("cma:public:-1:1", "cma:item:2147483648", "cma:item:True", "cma:public:10:2",
                   "cma:item:۱۰", "cma:halt:9", "cma:item:1:extra")
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_monitor_callback(raw)
        with patch.object(CallbackQuery, "answer", new_callable=AsyncMock):
            with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
                with patch.object(admin, "_mutate", new_callable=AsyncMock) as mutate:
                    async with self.Session() as session:
                        await admin.monitor_admin_callback(self.callback("cma:item:invalid-secret"), session, self.user, self.state)
                    mutate.assert_not_called()
                    self.assertNotIn("invalid-secret", str(answer.call_args))

    async def test_name_prompt_and_cancel_reopen_without_saving_the_field(self) -> None:
        await self.state.set_state(forms.MonitorInput.public_name)
        await self.state.update_data(**{CANCEL_CODE_KEY: forms.CANCEL_CODE, "monitor_target_id": 10})
        cancel = self.message.model_copy(update={"text": "انصراف"})
        with patch.object(Message, "answer", new_callable=AsyncMock):
            with patch("app.bot.connection_status_admin_ui.bot_monitor_access", AsyncMock(return_value=self.access)):
                with patch.object(forms, "edit_monitor_target", new_callable=AsyncMock) as edit:
                    async with self.Session() as session:
                        await forms.monitor_input(cancel, session, self.user, self.state)
                    edit.assert_not_called()
        self.assertIsNone(await self.state.get_state())

    async def test_field_input_calls_the_scoped_service_and_clears_the_fsm_after_success(self) -> None:
        async with self.Session() as session:
            session.add(ConnectionMonitorTarget(id=10, pg_host_id=41, test_enabled=True, generation="one", endpoint_count=1))
            await session.commit()
        await self.state.set_state(forms.MonitorInput.public_name)
        await self.state.update_data(monitor_target_id=10)
        with patch.object(Message, "answer", new_callable=AsyncMock):
            with patch.object(forms, "bot_monitor_access", AsyncMock(return_value=self.access)):
                with patch.object(forms, "edit_monitor_target", new_callable=AsyncMock) as edit:
                    async with self.Session() as session:
                        await forms.monitor_input(self.message, session, self.user, self.state)
                        self.assertEqual(edit.call_args.kwargs, {"target_id": 10, "field": "public_name", "value": "نام عمومی"})
        self.assertIsNone(await self.state.get_state())

    async def test_private_entry_denial_does_not_read_monitoring_data(self) -> None:
        with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
            with patch.object(admin, "bot_monitor_access", AsyncMock(side_effect=ValueError("دسترسی به مدیریت وضعیت اتصال مجاز نیست."))):
                with patch.object(admin, "show_monitor_home", new_callable=AsyncMock) as show:
                    async with self.Session() as session:
                        await admin.monitor_admin_entry(self.message, session, self.user, self.state)
                    show.assert_not_called()
                    self.assertIn("دسترسی", answer.call_args.args[0])


class MonitorCancelAndLabelTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_from_prompt_edits_the_prompt_back_into_the_menu(self) -> None:
        from app.bot.connection_status_admin_ui import reopen_monitor_home

        prompt = SimpleNamespace(from_user=SimpleNamespace(is_bot=True))
        state = AsyncMock()
        with patch("app.bot.connection_status_admin_ui.bot_monitor_access", AsyncMock()), \
             patch("app.bot.connection_status_admin_ui.show_monitor_home", AsyncMock()) as show:
            await reopen_monitor_home(prompt, AsyncMock(), SimpleNamespace(), state)
        self.assertTrue(show.call_args.kwargs["edit"])

    async def test_typed_cancel_sends_a_fresh_menu(self) -> None:
        from app.bot.connection_status_admin_ui import reopen_monitor_home

        typed = SimpleNamespace(from_user=SimpleNamespace(is_bot=False))
        with patch("app.bot.connection_status_admin_ui.bot_monitor_access", AsyncMock()), \
             patch("app.bot.connection_status_admin_ui.show_monitor_home", AsyncMock()) as show:
            await reopen_monitor_home(typed, AsyncMock(), SimpleNamespace(), AsyncMock())
        self.assertFalse(show.call_args.kwargs["edit"])

    def test_admin_entry_is_named_connection_status_and_earlier_names_are_refreshed(self) -> None:
        from app.services.connection_status_texts import readable_monitor_texts

        self.assertEqual(MONITOR_TEXT_DEFAULTS["btn_connection_status_admin"], "📡 وضعیت اتصال")
        for old in ("📡 تست و وضعیت اتصال", "📡 مدیریت وضعیت اتصال"):
            refreshed = readable_monitor_texts({"btn_connection_status_admin": old})
            self.assertEqual(refreshed["btn_connection_status_admin"], "📡 وضعیت اتصال")


class MonitorErrorPlacementTests(MonitorDatabaseCase):
    async def test_button_error_edits_the_menu_into_the_reason_with_a_back_button(self) -> None:
        message = SimpleNamespace(answer=AsyncMock())
        with patch("app.bot.connection_status_admin_ui.safe_edit_text", new_callable=AsyncMock) as edit:
            async with self.Session() as session:
                await monitor_error(message, session, MonitorAccountError("inactive"), edit=True)
        message.answer.assert_not_awaited()
        self.assertEqual(edit.call_args.args[1], ACCOUNT_MESSAGES["inactive"])
        buttons = [b for row in edit.call_args.kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertEqual([b.callback_data for b in buttons], ["cma:home:0"])

    async def test_typed_reply_error_is_sent_as_a_message(self) -> None:
        message = SimpleNamespace(answer=AsyncMock())
        async with self.Session() as session:
            await monitor_error(message, session, MonitorAccountError("inactive"))
        message.answer.assert_awaited_once()
