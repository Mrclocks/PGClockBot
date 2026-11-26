"""Naming a person by numeric id or @username, the contact picker, and «وقتی استارت زد خبرم کن»."""

from __future__ import annotations

import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import sqlalchemy as sa
from aiogram.exceptions import TelegramForbiddenError
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from sqlalchemy import select, text, true

from app.bot.handlers.start_watch import watch_offer_markup
from app.bot.user_picker import PICK_REQUEST_ID, picker_keyboard, shared_user_id
from app.db.models import BotUser, StartWatch
from app.jobs import start_watch as job
from app.services import start_watch as watches
from app.services.telegram_target import (
    TARGET_AMBIGUOUS,
    TargetNotFound,
    TelegramTarget,
    find_target_user,
    parse_telegram_target,
)
from tests.postgres_support import PostgresTestCase, scoped_sync_engine

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 26, 12, tzinfo=timezone.utc)
SHOP = 2


class ParseTests(unittest.TestCase):
    def test_ids_usernames_and_links_are_understood(self) -> None:
        self.assertEqual(parse_telegram_target(" 123456789 "), TelegramTarget(telegram_id=123456789))
        self.assertEqual(parse_telegram_target("۱۲۳۴۵۶"), TelegramTarget(telegram_id=123456))
        for raw in ("@Ali_Shop", "Ali_Shop", "https://t.me/Ali_Shop", "t.me/Ali_Shop"):
            self.assertEqual(parse_telegram_target(raw), TelegramTarget(username="Ali_Shop"), raw)

    def test_anything_else_is_refused(self) -> None:
        for raw in ("", "0", "-5", "12ab", "@ab", "@1abc", "<b>x</b>", "@" + "a" * 40, str(2**60)):
            with self.assertRaises(ValueError, msg=raw):
                parse_telegram_target(raw)


class PickerTests(unittest.TestCase):
    def test_only_this_pickers_single_share_counts(self) -> None:
        user = SimpleNamespace(user_id=777)
        share = lambda request_id, users: SimpleNamespace(users_shared=SimpleNamespace(request_id=request_id, users=users, user_ids=None))  # noqa: E731 - tiny local factory
        self.assertEqual(shared_user_id(share(PICK_REQUEST_ID, [user])), 777)
        self.assertIsNone(shared_user_id(share(PICK_REQUEST_ID + 1, [user])))
        self.assertIsNone(shared_user_id(share(PICK_REQUEST_ID, [user, SimpleNamespace(user_id=8)])))
        self.assertIsNone(shared_user_id(SimpleNamespace(users_shared=None)))

    def test_picker_asks_for_one_human(self) -> None:
        request = picker_keyboard().keyboard[0][0].request_users
        self.assertEqual((request.request_id, request.user_is_bot, request.max_quantity), (PICK_REQUEST_ID, False, 1))

    def test_watch_button_fits_telegram_callback_limit(self) -> None:
        for target in (TelegramTarget(username="a" * 32), TelegramTarget(telegram_id=2**52 - 1)):
            data = watch_offer_markup(target).inline_keyboard[0][0].callback_data
            self.assertLessEqual(len(data.encode()), 64)


class TargetCase(PostgresTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        async with self.Session() as session:
            session.add_all([
                BotUser(id=SHOP, telegram_id=9002, referral_code="shop", role="reseller"),
                BotUser(id=10, telegram_id=111, username="Ali_Shop", referral_code="a"),
                BotUser(id=11, telegram_id=222, username="twin", referral_code="b"),
                BotUser(id=12, telegram_id=333, username="TWIN", referral_code="c"),
                BotUser(id=13, telegram_id=444, username="shopper", referral_code="d", reseller_id=SHOP),
            ])
            await session.commit()


class FindTests(TargetCase):
    async def test_id_and_case_insensitive_username_find_the_same_person(self) -> None:
        async with self.Session() as session:
            self.assertEqual((await find_target_user(session, "111", scope=true())).id, 10)
            self.assertEqual((await find_target_user(session, "@ali_shop", scope=true())).id, 10)

    async def test_two_people_with_one_username_are_never_guessed(self) -> None:
        async with self.Session() as session:
            with self.assertRaisesRegex(ValueError, TARGET_AMBIGUOUS):
                await find_target_user(session, "@twin", scope=true())

    async def test_unknown_people_say_why(self) -> None:
        async with self.Session() as session:
            with self.assertRaises(TargetNotFound) as by_name:
                await find_target_user(session, "@nobody_here", scope=true())
            self.assertEqual(by_name.exception.username, "nobody_here")
            with self.assertRaises(TargetNotFound) as by_id:
                await find_target_user(session, "999999", scope=true())
            self.assertIsNone(by_id.exception.username)

    async def test_scope_hides_other_shops_customers(self) -> None:
        async with self.Session() as session:
            with self.assertRaises(TargetNotFound):
                await find_target_user(session, "@shopper", scope=BotUser.reseller_id.is_(None))
            self.assertEqual((await find_target_user(session, "@shopper", scope=BotUser.reseller_id == SHOP)).id, 13)


class WatchTests(TargetCase):
    async def test_one_open_watch_per_watcher_and_person(self) -> None:
        async with self.Session() as session:
            self.assertTrue(await watches.add_start_watch(session, watcher_telegram_id=5, target=TelegramTarget(username="Late_One"), now=NOW))
            self.assertFalse(await watches.add_start_watch(session, watcher_telegram_id=5, target=TelegramTarget(username="late_one"), now=NOW))
            self.assertTrue(await watches.add_start_watch(session, watcher_telegram_id=6, target=TelegramTarget(username="late_one"), now=NOW))

    async def test_arrivals_match_by_id_or_username_but_not_an_ambiguous_username(self) -> None:
        async with self.Session() as session:
            for target in (TelegramTarget(telegram_id=111), TelegramTarget(username="ALI_SHOP"),
                           TelegramTarget(username="twin"), TelegramTarget(username="not_yet")):
                await watches.add_start_watch(session, watcher_telegram_id=5, target=target, now=NOW)
            arrived = await watches.arrived_start_watches(session, now=NOW)
        self.assertEqual(sorted((w.target, u.id) for w, u in arrived), [("id:111", 10), ("u:ali_shop", 10)])

    async def test_expired_watches_are_ignored_and_purged(self) -> None:
        async with self.Session() as session:
            await watches.add_start_watch(session, watcher_telegram_id=5, target=TelegramTarget(telegram_id=111), now=NOW)
            later = NOW + timedelta(days=watches.WATCH_DAYS, seconds=1)
            self.assertEqual(await watches.arrived_start_watches(session, now=later), [])
            self.assertEqual(await watches.purge_start_watches(session, now=later), 1)

    async def test_job_announces_once_with_the_numeric_id_and_survives_a_blocked_watcher(self) -> None:
        async with self.Session() as session:
            await watches.add_start_watch(session, watcher_telegram_id=5, target=TelegramTarget(username="ali_shop"), now=datetime.now(timezone.utc))
            await watches.add_start_watch(session, watcher_telegram_id=6, target=TelegramTarget(telegram_id=111), now=datetime.now(timezone.utc))
        bot = AsyncMock()
        bot.send_message = AsyncMock(side_effect=[TelegramForbiddenError(method=None, message="blocked"), None])
        with patch.object(job, "SessionLocal", self.Session):
            await job.run_start_watch(bot)
            await job.run_start_watch(bot)
        self.assertEqual(bot.send_message.await_count, 2)
        sent = bot.send_message.await_args_list[1]
        self.assertIn("<code>111</code>", sent.args[1])
        self.assertEqual(sent.kwargs["reply_markup"].inline_keyboard[0][0].callback_data, "stw:mk:10")
        async with self.Session() as session:
            self.assertEqual(await session.scalar(select(sa.func.count()).where(StartWatch.notified_at.is_(None))), 0)

    async def test_arrival_text_escapes_names(self) -> None:
        user = BotUser(id=1, telegram_id=5, full_name="<b>x</b>", username="a_b_c")
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", job.arrival_text(user))


class UserCreateTests(TargetCase):
    def app(self, staff: dict) -> FastAPI:
        from app.api.user_pages import register_user_pages

        app = FastAPI()

        async def db():
            async with self.Session() as session:
                yield session

        async def guard():
            return staff

        register_user_pages(app, render=None, require_admin=guard, get_db=db)
        return app

    async def post(self, staff: dict, data: dict) -> httpx.Response:
        transport = httpx.ASGITransport(app=self.app(staff))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post("/users/create", data=data, follow_redirects=False)

    async def test_a_username_opens_the_existing_person_and_never_creates_one(self) -> None:
        owner = {"role": "admin", "permissions": [], "org_principal_id": 1, "org_depth": 0, "org_parent_id": None, "org_status": "active"}
        found = await self.post(owner, {"telegram_id": "@Ali_Shop"})
        self.assertIn("edit=10", found.headers["location"])
        missing = await self.post(owner, {"telegram_id": "@never_started"})
        self.assertIn("err=", missing.headers["location"])
        async with self.Session() as session:
            self.assertEqual(await session.scalar(select(sa.func.count(BotUser.id))), 5)

    async def test_a_shop_cannot_open_another_shops_customer_by_username(self) -> None:
        shop_staff = {"role": "reseller", "permissions": ["dashboard"], "bot_user_id": 3}
        async with self.Session() as session:
            session.add(BotUser(id=3, telegram_id=9003, referral_code="shop3", role="reseller"))
            await session.commit()
        response = await self.post(shop_staff, {"telegram_id": "@shopper"})
        self.assertNotIn("edit=13", response.headers["location"])
        self.assertIn("err=", response.headers["location"])


class StartWatchMigrationTests(unittest.TestCase):
    def test_upgrade_twice_then_downgrade(self) -> None:
        spec = importlib.util.spec_from_file_location("start_watches_mig", ROOT / "alembic/versions/0139_start_watches.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = scoped_sync_engine()
        with engine.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                names = {i["name"] for i in sa.inspect(conn).get_indexes("start_watches")}
                self.assertEqual(names, {"uq_start_watches_open", "ix_start_watches_open_expiry"})
                conn.execute(text("INSERT INTO start_watches (watcher_telegram_id, target, expires_at) VALUES (1, 'id:2', now())"))
                migration.downgrade()
                self.assertFalse(sa.inspect(conn).has_table("start_watches"))
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
