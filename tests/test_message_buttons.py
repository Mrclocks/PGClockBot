"""Buttons under staff messages: parsing, permissions, presets per shop, the broadcast worker and DMs."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.db.models import BotUser, ResellerProfile, Ticket, UserService
from app.services.broadcast_queue import BroadcastRequest, process_broadcast_batch, queue_broadcast
from app.services.message_buttons import (
    ButtonRules,
    broadcast_buttons,
    build_keyboard,
    delete_preset,
    dump_rows,
    list_presets,
    load_rows,
    parse_buttons,
    resolve_buttons,
    save_preset,
    split_message,
)
from app.services.shop_broadcast import queue_shop_broadcast
from app.services.users_quick import send_staff_dm

ROOT = Path(__file__).resolve().parents[1]
LINKS = ButtonRules(allow_links=True)
NO_LINKS = ButtonRules()
SHOP_A, SHOP_B = 20, 30


def specs(rows):
    return [[(b.kind, b.label, b.target) for b in row] for row in rows]


class ParseTests(unittest.TestCase):
    def test_rows_and_screen_buttons(self) -> None:
        rows = parse_buttons("🛒 خرید - @shop | پشتیبانی - @support\nکیف پول - @wallet", NO_LINKS)
        self.assertEqual(specs(rows), [
            [("screen", "🛒 خرید", "shop"), ("screen", "پشتیبانی", "support")],
            [("screen", "کیف پول", "wallet")],
        ])

    def test_a_label_may_contain_a_dash(self) -> None:
        self.assertEqual(specs(parse_buttons("پلن - ۳۰ روزه - @shop", NO_LINKS)), [[("screen", "پلن - ۳۰ روزه", "shop")]])

    def test_links_need_permission(self) -> None:
        with self.assertRaisesRegex(ValueError, "اجازه"):
            parse_buttons("کانال - https://t.me/x", NO_LINKS)
        self.assertEqual(specs(parse_buttons("کانال - https://t.me/x", LINKS)), [[("url", "کانال", "https://t.me/x")]])

    def test_unsafe_or_malformed_targets_are_refused(self) -> None:
        for line in ("x - http://t.me/a", "x - javascript:alert(1)", "x - https://a b.com", "x - https://user:pw@a.com",
                     "x - @admin", "بدون مقصد", "x - https://localhost"):
            with self.subTest(line=line), self.assertRaises(ValueError):
                parse_buttons(line, LINKS)

    def test_limits(self) -> None:
        with self.assertRaisesRegex(ValueError, "ردیف"):
            parse_buttons("a - @shop | b - @shop | c - @shop | d - @shop", NO_LINKS)
        with self.assertRaises(ValueError):
            parse_buttons("\n".join(["a - @shop"] * 9), NO_LINKS)
        with self.assertRaisesRegex(ValueError, "نویسه"):
            parse_buttons(("x" * 41) + " - @shop", NO_LINKS)

    def test_renewal_only_for_the_allowed_services(self) -> None:
        rules = ButtonRules(renew_service_ids=frozenset({7}))
        self.assertEqual(specs(parse_buttons("تمدید - @renew:7", rules)), [[("renew", "تمدید", "7")]])
        with self.assertRaisesRegex(ValueError, "تمدید"):
            parse_buttons("تمدید - @renew:8", rules)
        with self.assertRaises(ValueError):
            parse_buttons("تمدید - @renew:7", NO_LINKS)

    def test_keyboard_opens_screens_under_the_message(self) -> None:
        rows = parse_buttons("a - @shop | b - @renew:7 | c - https://t.me/x", ButtonRules(True, frozenset({7})))
        keyboard = build_keyboard(rows)
        self.assertEqual([b.callback_data for b in keyboard[0][:2]], ["nto:shop:list", "nto:svc:renew:7"])
        self.assertEqual(keyboard[0][2].url, "https://t.me/x")

    def test_stored_rows_are_checked_again(self) -> None:
        stored = dump_rows(parse_buttons("a - @shop | c - https://t.me/x", LINKS))
        self.assertEqual(specs(load_rows(stored, NO_LINKS)), [[("screen", "a", "shop")]])
        self.assertEqual(load_rows("not json", LINKS), [])
        self.assertEqual(load_rows('[[{"k": "screen", "l": "x", "t": "adm:wallet"}]]', LINKS), [])

    def test_split_message(self) -> None:
        self.assertEqual(split_message("سلام\nخوبی\n---\na - @shop"), ("سلام\nخوبی", "a - @shop"))
        self.assertEqual(split_message("بدون دکمه"), ("بدون دکمه", ""))


class DbCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.tmp.name) / 'm.db'}")
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
            session.add_all([
                UserService(id=7, bot_user_id=2, shop_id=SHOP_A, pg_username="mine"),
                UserService(id=8, bot_user_id=3, shop_id=SHOP_A, pg_username="other"),
            ])
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self.tmp.cleanup()


class PresetTests(DbCase):
    async def test_presets_are_per_shop_and_expand_by_name(self) -> None:
        async with self.Session() as session:
            await save_preset(session, SHOP_A, name="فروش", buttons="خرید - @shop", rules=NO_LINKS)
            await save_preset(session, SHOP_A, name="فروش", buttons="کیف پول - @wallet", rules=NO_LINKS)
            self.assertEqual([p.name for p in await list_presets(session, SHOP_A)], ["فروش"])
            self.assertEqual(await list_presets(session, SHOP_B), [])
            rows = await resolve_buttons(session, "@preset:فروش\nپشتیبانی - @support", shop_id=SHOP_A, rules=NO_LINKS)
            self.assertEqual(specs(rows), [[("screen", "کیف پول", "wallet")], [("screen", "پشتیبانی", "support")]])
            with self.assertRaisesRegex(ValueError, "پیدا نشد"):
                await resolve_buttons(session, "@preset:فروش", shop_id=SHOP_B, rules=NO_LINKS)

    async def test_another_shop_cannot_delete_a_preset(self) -> None:
        async with self.Session() as session:
            preset = await save_preset(session, SHOP_A, name="x", buttons="خرید - @shop", rules=NO_LINKS)
            self.assertFalse(await delete_preset(session, SHOP_B, preset.id))
            self.assertFalse(await delete_preset(session, None, preset.id))
            self.assertTrue(await delete_preset(session, SHOP_A, preset.id))

    async def test_a_shop_without_permission_cannot_add_links(self) -> None:
        async with self.Session() as session:
            with self.assertRaisesRegex(ValueError, "اجازه"):
                await broadcast_buttons(session, "کانال - https://t.me/x", shop_id=SHOP_A)
            self.assertIsNotNone(await broadcast_buttons(session, "کانال - https://t.me/x", shop_id=SHOP_B))
            self.assertIsNotNone(await broadcast_buttons(session, "کانال - https://t.me/x", shop_id=None))


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
            buttons = await broadcast_buttons(session, "خرید - @shop", shop_id=None)
            log = await queue_broadcast(session, text="سلام", audience="users", created_by="o", request=BroadcastRequest(buttons=buttons))
        await self.run_batches(log.id, bot)
        markup = bot.send_message.await_args.kwargs["reply_markup"]
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, "nto:shop:list")

    async def test_a_revoked_link_is_not_sent(self) -> None:
        bot = MagicMock()
        bot.send_message = AsyncMock()
        stored = dump_rows(parse_buttons("کانال - https://t.me/x | خرید - @shop", LINKS))
        async with self.Session() as session:
            log = await queue_shop_broadcast(session, shop_id=SHOP_A, text="سلام", created_by="s", buttons=stored)
        await self.run_batches(log.id, bot)
        markup = bot.send_message.await_args.kwargs["reply_markup"]
        self.assertEqual([b.text for row in markup.inline_keyboard for b in row], ["خرید"])


class DirectMessageTests(DbCase):
    async def send(self, buttons: str, user_id: int = 2):
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

    async def test_renewal_of_the_customers_own_service_sits_above_reply_and_close(self) -> None:
        bot = await self.send("تمدید - @renew:7")
        rows = bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
        self.assertEqual(rows[0][0].callback_data, "nto:svc:renew:7")
        self.assertGreater(len(rows), 1)

    async def test_another_customers_service_is_refused_and_nothing_is_stored(self) -> None:
        with self.assertRaisesRegex(ValueError, "تمدید"):
            await self.send("تمدید - @renew:8")
        self.assertEqual(await self.tickets(), 0)

    async def test_plain_message_is_unchanged(self) -> None:
        bot = await self.send("")
        self.assertNotIn("nto:", str(bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data))


class MigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade_keep_broadcasts(self) -> None:
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        spec = importlib.util.spec_from_file_location("m0116", ROOT / "alembic/versions/0116_message_buttons.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
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
