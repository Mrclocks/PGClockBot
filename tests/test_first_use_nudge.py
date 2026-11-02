"""First-use nudge: once per service, per-shop switch, shop bots only, and an unreachable panel retries."""
import asyncio
import importlib.util
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.exceptions import TelegramForbiddenError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, BotUser, UserService
from app.services import first_use_nudge as nudge
from app.services.users import _SETTINGS_CACHE, SETTING_GROUPS, clear_settings_cache, set_setting

ROOT = Path(__file__).resolve().parents[1]
UNUSED = {"status": "active", "used_traffic": 0}


class FakePanel:
    def __init__(self):
        self.infos = {}
        self.calls = []

    async def subscription_info(self, token):
        self.calls.append(token)
        info = self.infos.get(token, UNUSED)
        if isinstance(info, Exception):
            raise info
        return info


class NudgeCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.tmp.name) / 'nudge.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.session = self.Session()
        _SETTINGS_CACHE.clear()
        self.now = datetime.now(timezone.utc)
        self.session.add(BotUser(id=2, telegram_id=1002, referral_code="shop2", role="reseller"))
        await self.session.flush()
        self.session.add_all([
            BotUser(id=1, telegram_id=1001, referral_code="c1"),
            BotUser(id=4, telegram_id=1004, referral_code="c4", reseller_id=2),
            BotUser(id=5, telegram_id=1005, referral_code="c5", is_blocked=True),
        ])
        await self.session.flush()
        self.session.add_all([
            self.service(10, 1, "plat-unused"),
            self.service(11, 4, "shop-unused", shop_id=2),
            self.service(12, 1, "too-new", age=10),
            self.service(13, 1, "too-old", age=100),
            self.service(14, 1, "imported", remark="imported"),
            self.service(15, 5, "blocked-owner"),
            self.service(16, 1, "cancelled", is_cancelled=True),
            self.service(17, 1, "no-token", token=None),
        ])
        await self.session.commit()
        async with self.Session() as session:
            await set_setting(session, nudge.SETTING_KEY, "1")
            await set_setting(session, nudge.SETTING_KEY, "1", reseller_id=2)
        clear_settings_cache()
        self.panel = FakePanel()
        self.platform_bot, self.shop_bot = AsyncMock(), AsyncMock()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()
        clear_settings_cache()
        self.tmp.cleanup()

    def service(self, service_id, user_id, name, *, shop_id=None, age=30, remark="order:1", token="auto", is_cancelled=False):
        return UserService(
            id=service_id, bot_user_id=user_id, pg_user_id=service_id, pg_username=name, shop_id=shop_id, remark=remark,
            subscription_token=f"tok{service_id}" if token == "auto" else token, is_cancelled=is_cancelled,
            created_at=self.now - timedelta(hours=age),
        )

    def bot_for(self, shop_id):
        return {None: self.platform_bot, 2: self.shop_bot}.get(shop_id)

    async def run_check(self, bot_for=None):
        async with self.Session() as session:
            return await nudge.run_first_use_check(session, self.panel, bot_for or self.bot_for, now=self.now)

    async def stamped(self, service_id):
        row = await self.session.get(UserService, service_id)
        await self.session.refresh(row)
        return row.first_use_checked_at is not None


class FirstUseNudgeTests(NudgeCase):
    async def test_only_unused_services_in_the_window_are_messaged_from_their_own_bot(self):
        self.assertEqual(await self.run_check(), 2)
        self.assertEqual([c.args[0] for c in self.platform_bot.send_message.await_args_list], [1001])
        self.assertEqual([c.args[0] for c in self.shop_bot.send_message.await_args_list], [1004])
        message = self.platform_bot.send_message.await_args
        self.assertIn("plat-unused", message.args[1])
        self.assertEqual([b.callback_data for row in message.kwargs["reply_markup"].inline_keyboard for b in row], ["guide:svc:10", nudge.SUPPORT_CALLBACK])
        for service_id in (12, 13, 14, 15, 16, 17):
            self.assertFalse(await self.stamped(service_id), service_id)
        self.assertNotIn("toktoo", " ".join(self.panel.calls))

    async def test_a_service_is_checked_and_messaged_once(self):
        await self.run_check()
        await self.run_check()
        self.assertEqual(self.platform_bot.send_message.await_count, 1)
        self.assertEqual(self.shop_bot.send_message.await_count, 1)
        self.assertEqual(len(self.panel.calls), 2)

    async def test_a_used_service_is_stamped_without_a_message(self):
        self.panel.infos["tok10"] = {"status": "active", "used_traffic": 5000}
        await self.run_check()
        self.assertTrue(await self.stamped(10))
        self.platform_bot.send_message.assert_not_awaited()

    async def test_on_hold_counts_as_unused_but_expired_and_disabled_do_not(self):
        self.panel.infos["tok10"] = {"status": "on_hold", "used_traffic": 0}
        self.panel.infos["tok11"] = {"status": "expired", "used_traffic": 0}
        await self.run_check()
        self.assertEqual(self.platform_bot.send_message.await_count, 1)
        self.shop_bot.send_message.assert_not_awaited()

    async def test_the_switch_is_per_shop_and_an_off_shop_costs_no_panel_call(self):
        async with self.Session() as session:
            await set_setting(session, nudge.SETTING_KEY, "0", reseller_id=2)
        clear_settings_cache()
        await self.run_check()
        self.shop_bot.send_message.assert_not_awaited()
        self.assertNotIn("tok11", self.panel.calls)
        self.assertFalse(await self.stamped(11))
        self.assertEqual(self.platform_bot.send_message.await_count, 1)

    async def test_a_shop_without_a_running_bot_is_skipped_and_never_messaged_from_the_platform_bot(self):
        await self.run_check(bot_for=lambda shop_id: self.platform_bot if shop_id is None else None)
        self.assertEqual([c.args[0] for c in self.platform_bot.send_message.await_args_list], [1001])
        self.assertFalse(await self.stamped(11))

    async def test_an_unreachable_panel_leaves_the_service_for_the_next_run(self):
        self.panel.infos["tok10"] = TimeoutError("panel down")
        await self.run_check()
        self.assertFalse(await self.stamped(10))
        self.platform_bot.send_message.assert_not_awaited()
        del self.panel.infos["tok10"]
        await self.run_check()
        self.assertEqual(self.platform_bot.send_message.await_count, 1)

    async def test_a_failed_or_unconfirmed_send_is_never_repeated(self):
        self.platform_bot.send_message.side_effect = TelegramForbiddenError(method=MagicMock(), message="blocked")
        self.shop_bot.send_message.side_effect = TimeoutError("lost response")
        self.assertEqual(await self.run_check(), 0)
        await self.run_check()
        self.assertEqual(self.platform_bot.send_message.await_count, 1)
        self.assertEqual(self.shop_bot.send_message.await_count, 1)
        self.assertTrue(await self.stamped(10))

    async def test_two_runs_at_once_message_a_customer_once(self):
        await asyncio.gather(self.run_check(), self.run_check())
        self.assertEqual(self.platform_bot.send_message.await_count, 1)
        self.assertEqual(self.shop_bot.send_message.await_count, 1)

    async def test_one_run_is_bounded(self):
        with patch.object(nudge, "BATCH_SIZE", 1), patch.object(nudge, "MAX_BATCHES", 1):
            await self.run_check()
        self.assertEqual(self.platform_bot.send_message.await_count + self.shop_bot.send_message.await_count, 1)

    def test_names_are_escaped_and_the_setting_is_off_by_default(self):
        self.assertNotIn("<i>", nudge.nudge_text("<i>x</i>"))
        from app.services.users import DEFAULT_SETTINGS

        self.assertEqual(DEFAULT_SETTINGS[nudge.SETTING_KEY], "0")
        fields = [item for group in SETTING_GROUPS.values() for item in group if item[0] == nudge.SETTING_KEY]
        self.assertEqual([item[2] for item in fields], ["toggle"])

    def test_is_unused_reads_the_panel_status_and_traffic(self):
        for info, expected in (
            ({"status": "active", "used_traffic": 0}, True), ({"status": "on_hold"}, True), ({"status": "active", "used_traffic": 1}, False),
            ({"status": "disabled", "used_traffic": 0}, False), ({"status": "active", "used_traffic": "x"}, False), ({}, False),
        ):
            self.assertEqual(nudge.is_unused(info), expected, info)


class FirstUseMigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade_keep_services(self):
        import sqlalchemy as sa
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        spec = importlib.util.spec_from_file_location("first_use_migration", ROOT / "alembic/versions/0074_first_use_nudge.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE user_services (id INTEGER PRIMARY KEY, pg_username TEXT, created_at TIMESTAMP)"))
            conn.execute(text("INSERT INTO user_services VALUES (1, 'old-service', '2026-01-01 00:00:00')"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                self.assertIsNone(conn.execute(text("SELECT first_use_checked_at FROM user_services")).scalar())
                self.assertIn(migration.INDEX, {i["name"] for i in sa.inspect(conn).get_indexes("user_services")})
                migration.downgrade()
                self.assertEqual(conn.execute(text("SELECT pg_username FROM user_services")).scalar(), "old-service")
                self.assertNotIn(migration.INDEX, {i["name"] for i in sa.inspect(conn).get_indexes("user_services")})
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
