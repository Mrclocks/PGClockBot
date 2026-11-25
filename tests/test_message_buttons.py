"""Buttons under staff messages: one to four, permissions, colours, presets per shop, the bot checklist,
the broadcast worker and direct messages."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import sqlalchemy as sa
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.datastructures import FormData

from app.bot import message_button_picker as picker
from app.db import Base
from app.db.models import BotUser, ResellerProfile, Ticket, UserService
from app.services.broadcast_queue import BroadcastRequest, process_broadcast_batch, queue_broadcast
from app.services.message_buttons import (
    ButtonInput,
    ButtonRules,
    broadcast_buttons,
    build_keyboard,
    delete_preset,
    dump_buttons,
    form_inputs,
    list_presets,
    load_buttons,
    make_buttons,
    preset_inputs,
    save_preset,
)
from app.services.shop_broadcast import queue_shop_broadcast
from app.services.users_quick import send_staff_dm
from tests.postgres_support import scoped_async_engine, scoped_sync_engine

ROOT = Path(__file__).resolve().parents[1]
LINKS = ButtonRules(allow_links=True)
NO_LINKS = ButtonRules()
SHOP_A, SHOP_B = 20, 30


def kinds(buttons):
    return [(b.kind, b.label, b.target, b.style) for b in buttons]


class MakeButtonsTests(unittest.TestCase):
    def test_a_screen_button_without_a_label_is_stored_empty_and_keeps_its_colour(self) -> None:
        buttons = make_buttons([ButtonInput("shop"), ButtonInput("support", "سؤال دارید؟", style="success")], NO_LINKS)
        self.assertEqual(kinds(buttons), [
            ("screen", "", "shop", ""),
            ("screen", "سؤال دارید؟", "support", "success"),
        ])

    def test_one_to_four_buttons(self) -> None:
        self.assertEqual(make_buttons([ButtonInput("")], NO_LINKS), [])
        with self.assertRaises(ValueError):
            make_buttons([ButtonInput("shop")] * 5, NO_LINKS)

    def test_links_need_permission_https_and_a_label(self) -> None:
        with self.assertRaisesRegex(ValueError, "اجازه"):
            make_buttons([ButtonInput("link", "کانال", "https://t.me/x")], NO_LINKS)
        for url in ("http://t.me/a", "javascript:alert(1)", "https://a b.com", "https://u:p@a.com", "https://localhost"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                make_buttons([ButtonInput("link", "x", url)], LINKS)
        with self.assertRaisesRegex(ValueError, "متن"):
            make_buttons([ButtonInput("link", "", "https://t.me/x")], LINKS)

    def test_unknown_targets_and_bad_colours(self) -> None:
        with self.assertRaises(ValueError):
            make_buttons([ButtonInput("adm:wallet")], LINKS)
        self.assertEqual(make_buttons([ButtonInput("shop", style="purple")], NO_LINKS)[0].style, "")

    def test_renewal_only_with_a_service(self) -> None:
        with self.assertRaisesRegex(ValueError, "تمدید"):
            make_buttons([ButtonInput("renew")], NO_LINKS)
        self.assertEqual(kinds(make_buttons([ButtonInput("renew")], ButtonRules(renew_service_id=7))),
                         [("renew", "🔄 تمدید سرویس", "7", "")])

    def test_keyboard_two_per_row_opening_under_the_message(self) -> None:
        buttons = make_buttons(
            [ButtonInput("shop", style="primary"), ButtonInput("renew"), ButtonInput("link", "کانال", "https://t.me/x")],
            ButtonRules(True, 7),
        )
        keyboard = build_keyboard(buttons)
        self.assertEqual([len(row) for row in keyboard], [2, 1])
        self.assertEqual([b.callback_data for b in keyboard[0]], ["nto:shop:list", "nto:svc:renew:7"])
        self.assertEqual(keyboard[0][0].style, "primary")
        self.assertEqual(keyboard[1][0].url, "https://t.me/x")

    def test_stored_buttons_are_checked_again(self) -> None:
        stored = dump_buttons(make_buttons([ButtonInput("shop"), ButtonInput("link", "c", "https://t.me/x")], LINKS))
        self.assertEqual([b.target for b in load_buttons(stored, NO_LINKS)], ["shop"])
        self.assertEqual(load_buttons("not json", LINKS), [])
        self.assertEqual(load_buttons('[{"k": "screen", "l": "x", "t": "adm:wallet"}]', LINKS), [])

    def test_form_rows(self) -> None:
        form = FormData([
            ("btn_target", "shop"), ("btn_label", ""), ("btn_url", ""), ("btn_style", "success"),
            ("btn_target", "link"), ("btn_label", "کانال"), ("btn_url", "https://t.me/x"), ("btn_style", ""),
        ])
        self.assertEqual(form_inputs(form), [
            ButtonInput("shop", "", "", "success"), ButtonInput("link", "کانال", "https://t.me/x", ""),
        ])


class ChecklistTests(unittest.TestCase):
    def test_toggle_keeps_order_and_stops_at_four(self) -> None:
        selected: list[str] = []
        for key in ("shop", "wallet", "support", "club"):
            selected = picker.toggle(selected, key)
        self.assertEqual(selected, ["shop", "wallet", "support", "club"])
        with self.assertRaises(ValueError):
            picker.toggle(selected, "services")
        self.assertEqual(picker.toggle(selected, "wallet"), ["shop", "support", "club"])

    def test_checklist_marks_choices_and_lists_presets(self) -> None:
        markup = picker.checklist_markup(["shop"], renew=True, presets=[(3, "فروش")])
        texts = [b.text for row in markup.inline_keyboard for b in row]
        self.assertIn("✅ 🛒 خرید سرویس", texts)
        self.assertIn("⬜ 🔄 تمدید سرویس", texts)
        self.assertIn("📚 قالب: فروش", texts)
        self.assertNotIn("⬜ 🔗 لینک", texts)


class DbCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=SHOP_A, telegram_id=9020, referral_code="a", role="reseller"),
                BotUser(id=SHOP_B, telegram_id=9030, referral_code="b", role="reseller"),
            ])
            await session.flush()
            session.add_all([
                ResellerProfile(user_id=SHOP_A, is_active=True, bot_token="t", web_permissions="broadcast"),
                ResellerProfile(user_id=SHOP_B, is_active=True, bot_token="t", web_permissions="broadcast,broadcast_links"),
                BotUser(id=1, telegram_id=1001, referral_code="u1", role="user"),
                BotUser(id=2, telegram_id=2002, referral_code="u2", role="user", reseller_id=SHOP_A),
                BotUser(id=3, telegram_id=3003, referral_code="u3", role="user", reseller_id=SHOP_A),
            ])
            await session.flush()
            session.add(UserService(id=7, bot_user_id=2, shop_id=SHOP_A, pg_username="mine"))
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()


class PresetTests(DbCase):
    async def test_presets_are_per_shop(self) -> None:
        async with self.Session() as session:
            await save_preset(session, SHOP_A, name="فروش", items=[ButtonInput("shop")], rules=NO_LINKS)
            preset = await save_preset(session, SHOP_A, name="فروش", items=[ButtonInput("wallet", style="danger")], rules=NO_LINKS)
            self.assertEqual([p.name for p in await list_presets(session, SHOP_A)], ["فروش"])
            self.assertEqual(await list_presets(session, SHOP_B), [])
            self.assertEqual(await preset_inputs(session, SHOP_A, preset.id), [ButtonInput("wallet", "", "", "danger")])
            self.assertEqual(await preset_inputs(session, SHOP_B, preset.id), [])
            self.assertFalse(await delete_preset(session, SHOP_B, preset.id))
            self.assertFalse(await delete_preset(session, None, preset.id))
            self.assertTrue(await delete_preset(session, SHOP_A, preset.id))

    async def test_a_preset_cannot_hold_a_renewal(self) -> None:
        async with self.Session() as session:
            with self.assertRaisesRegex(ValueError, "تمدید"):
                await save_preset(session, SHOP_A, name="x", items=[ButtonInput("renew")], rules=LINKS)

    async def test_a_shop_without_permission_cannot_add_links(self) -> None:
        link = [ButtonInput("link", "کانال", "https://t.me/x")]
        async with self.Session() as session:
            with self.assertRaisesRegex(ValueError, "اجازه"):
                await broadcast_buttons(session, link, shop_id=SHOP_A)
            self.assertIsNotNone(await broadcast_buttons(session, link, shop_id=SHOP_B))
            self.assertIsNotNone(await broadcast_buttons(session, link, shop_id=None))


class PickerFlowTests(DbCase):
    async def test_a_preset_tap_uses_the_shop_the_flow_started_in(self) -> None:
        async with self.Session() as session:
            other = await save_preset(session, SHOP_B, name="b", items=[ButtonInput("shop")], rules=NO_LINKS)
        state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))
        message = MagicMock()
        message.answer = AsyncMock()
        await picker.ask_for_buttons(message, state, flow="t", shop_id=SHOP_A)
        finished = AsyncMock()
        picker.register_flow("t", finished)
        callback = MagicMock(data=f"mbp:p:{other.id}")
        callback.answer = AsyncMock()
        async with self.Session() as session:
            await picker.on_preset(callback, state, session, MagicMock())
        finished.assert_not_awaited()
        callback.answer.assert_awaited_with(picker.EXPIRED, show_alert=True)


class WorkerTests(DbCase):
    async def run_batches(self, log_id: int, bot) -> None:
        for _ in range(10):
            async with self.Session() as session:
                if not await process_broadcast_batch(session, log_id, bot, delay=0):
                    return

    async def test_platform_broadcast_carries_its_buttons(self) -> None:
        bot = MagicMock()
        bot.send_message = AsyncMock()
        async with self.Session() as session:
            stored = await broadcast_buttons(session, [ButtonInput("shop", style="success")], shop_id=None)
            log = await queue_broadcast(session, text="سلام", audience="users", created_by="o", request=BroadcastRequest(buttons=stored))
        await self.run_batches(log.id, bot)
        button = bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
        self.assertEqual((button.callback_data, button.style), ("nto:shop:list", "success"))

    async def test_a_revoked_link_is_not_sent(self) -> None:
        bot = MagicMock()
        bot.send_message = AsyncMock()
        stored = dump_buttons(make_buttons([ButtonInput("link", "کانال", "https://t.me/x"), ButtonInput("shop", "خرید")], LINKS))
        async with self.Session() as session:
            log = await queue_shop_broadcast(session, shop_id=SHOP_A, text="سلام", created_by="s", buttons=stored)
        await self.run_batches(log.id, bot)
        markup = bot.send_message.await_args.kwargs["reply_markup"]
        self.assertEqual([b.text for row in markup.inline_keyboard for b in row], ["خرید"])


class DirectMessageTests(DbCase):
    async def send(self, buttons, user_id: int = 2):
        bot = MagicMock()
        bot.send_message = AsyncMock()
        async with self.Session() as session:
            user = await session.get(BotUser, user_id)
            with patch("app.services.reseller_bots.open_notify_bot_for_user", AsyncMock(return_value=(bot, False))):
                await send_staff_dm(session, user, "سلام", shop_id=SHOP_A, actor="x", buttons=buttons)
            await session.commit()
        return bot

    async def tickets(self) -> int:
        async with self.Session() as session:
            return int(await session.scalar(select(func.count()).select_from(Ticket)))

    async def test_renewal_targets_the_customers_service_above_reply_and_close(self) -> None:
        bot = await self.send([ButtonInput("renew")])
        rows = bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
        self.assertEqual(rows[0][0].callback_data, "nto:svc:renew:7")
        self.assertGreater(len(rows), 1)

    async def test_a_customer_without_a_service_gets_no_renewal_and_nothing_is_stored(self) -> None:
        with self.assertRaisesRegex(ValueError, "تمدید"):
            await self.send([ButtonInput("renew")], user_id=3)
        self.assertEqual(await self.tickets(), 0)

    async def test_a_shop_without_permission_cannot_link_in_a_dm(self) -> None:
        with self.assertRaisesRegex(ValueError, "اجازه"):
            await self.send([ButtonInput("link", "x", "https://t.me/x")])
        self.assertEqual(await self.tickets(), 0)


class MigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade_keep_broadcasts(self) -> None:
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        spec = importlib.util.spec_from_file_location("m0116", ROOT / "alembic/versions/0116_message_buttons.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = scoped_sync_engine()
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE broadcast_logs (id INTEGER PRIMARY KEY, text TEXT)"))
            conn.execute(sa.text("INSERT INTO broadcast_logs VALUES (1, 'hi')"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                self.assertIn("buttons", {c["name"] for c in sa.inspect(conn).get_columns("broadcast_logs")})
                self.assertIn("message_button_presets", sa.inspect(conn).get_table_names())
                migration.downgrade()
                self.assertNotIn("message_button_presets", sa.inspect(conn).get_table_names())
                self.assertEqual(conn.execute(sa.text("SELECT text FROM broadcast_logs")).scalar(), "hi")
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
