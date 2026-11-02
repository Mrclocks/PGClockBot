"""Staff find users and resellers by numeric id or Telegram @username, inside their own scope only."""

from __future__ import annotations

import unittest

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, BotUser
from app.services.user_search import clean_handle, find_bot_user, with_search_matches
from app.services.users_ops import scoped_users_where

SHOP = 5


class CleanHandleTests(unittest.TestCase):
    def test_the_usual_ways_of_writing_a_username(self) -> None:
        for raw in ("@Ali_Shop", " Ali_Shop ", "t.me/Ali_Shop", "https://t.me/Ali_Shop", "@ Ali_Shop"):
            with self.subTest(raw=raw):
                self.assertEqual(clean_handle(raw), "Ali_Shop")
        self.assertEqual(clean_handle(None), "")


class UserSearchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add_all([
            BotUser(id=SHOP, telegram_id=5005, referral_code="o5", role="reseller", username="Shop_Owner"),
            BotUser(id=10, telegram_id=1010, referral_code="c10", role="user", username="Ali_Shop"),
            BotUser(id=11, telegram_id=1011, referral_code="c11", role="user", username="other_100%",
                    reseller_id=SHOP),
            BotUser(id=12, telegram_id=1012, referral_code="c12", role="user", username="alishop2"),
        ])
        await self.session.commit()

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.engine.dispose()

    async def test_a_username_or_numeric_id_finds_one_user(self) -> None:
        platform = BotUser.reseller_id.is_(None)
        for raw in ("@ali_shop", "ALI_SHOP", "t.me/Ali_Shop", "1010"):
            with self.subTest(raw=raw):
                self.assertEqual(int((await find_bot_user(self.session, raw, scope=platform)).id), 10)
        self.assertEqual(int((await find_bot_user(self.session, "@Shop_Owner", scope=platform)).id), SHOP)
        self.assertIsNone(await find_bot_user(self.session, "@ali", scope=platform), "an exact username only")

    async def test_a_user_outside_the_scope_is_never_found(self) -> None:
        platform = BotUser.reseller_id.is_(None)
        for raw in ("@other_100%", "1011"):
            with self.subTest(raw=raw):
                self.assertIsNone(await find_bot_user(self.session, raw, scope=platform))

    async def test_the_list_search_reaches_users_beyond_the_recent_window(self) -> None:
        platform = scoped_users_where(None)
        found = await with_search_matches(self.session, [], platform, "@ALI")
        self.assertEqual([int(u.id) for u in found], [12, 10], "partial, case-insensitive, newest first")
        shop = await with_search_matches(self.session, [], scoped_users_where(SHOP), "ali")
        self.assertEqual(shop, [], "the shop sees only its own customers")

    async def test_like_wildcards_in_the_query_are_literal(self) -> None:
        found = await with_search_matches(self.session, [], scoped_users_where(SHOP), "%")
        self.assertEqual([int(u.id) for u in found], [11])
        self.assertEqual(await with_search_matches(self.session, [], scoped_users_where(None), "_"),
                         [await self.session.get(BotUser, 10)])


if __name__ == "__main__":
    unittest.main()
