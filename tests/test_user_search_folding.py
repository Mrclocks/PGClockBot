"""Staff search finds customers typed on an Arabic keyboard, by the last digits of the id, or by service name."""

from __future__ import annotations

import unittest

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.db.models import BotUser, Role, UserService
from app.services.list_query import fold_search_text, text_matches
from app.services.user_search import search_bot_users, service_owner_clause
from app.services.users_ops import scoped_users_where


class FoldTests(unittest.TestCase):
    def test_arabic_letters_half_spaces_digits_and_spacing_are_folded(self) -> None:
        self.assertEqual(fold_search_text("علي"), fold_search_text("علی"))
        self.assertEqual(fold_search_text("كاربر"), fold_search_text("کاربر"))
        self.assertEqual(fold_search_text("نیم‌فاصله"), fold_search_text("نیم  فاصله"))
        self.assertEqual(fold_search_text("۱۲۳٤"), "1234")

    def test_the_panel_list_filter_uses_the_same_folds(self) -> None:
        self.assertTrue(text_matches("مریم کریمی", needle="مريم"))
        self.assertFalse(text_matches("مریم کریمی", needle="سارا"))


class SearchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            self.shop = BotUser(telegram_id=1, full_name="shop", role=Role.RESELLER.value, referral_code="R1")
            self.other_shop = BotUser(telegram_id=2, full_name="other", role=Role.RESELLER.value, referral_code="R2")
            session.add_all([self.shop, self.other_shop])
            await session.flush()
            rows = [
                (123456789, "علی رضایی", "Ali_Rez", self.shop.id),
                (987654321, "مریم کریمی", None, self.shop.id),
                (555000111, "کاربر ‌نیم‌فاصله", "zed", self.shop.id),
                (777456789, "علی دیگر", "ali_other", self.other_shop.id),
            ]
            for index, (telegram_id, name, username, shop_id) in enumerate(rows):
                session.add(BotUser(telegram_id=telegram_id, full_name=name, username=username, role=Role.USER.value,
                                    referral_code=f"R{index + 10}", reseller_id=shop_id))
            await session.flush()
            owner = await session.scalar(select(BotUser).where(BotUser.telegram_id == 987654321))
            session.add(UserService(bot_user_id=owner.id, pg_username="sara_vpn_01", shop_id=self.shop.id))
            session.add(UserService(bot_user_id=owner.id, pg_username="platform_only_02", shop_id=None))
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _find(self, query: str, *, with_services: bool = False) -> set[int]:
        async with self.Session() as session:
            also = service_owner_clause(query, shop_id=self.shop.id) if with_services else None
            users, _ = await search_bot_users(session, query, scope=scoped_users_where(self.shop.id), also=also)
            return {int(user.telegram_id) for user in users}

    async def test_arabic_letters_and_half_spaces_find_the_same_customer(self) -> None:
        self.assertEqual(await self._find("علي"), {123456789})
        self.assertEqual(await self._find("مريم"), {987654321})
        self.assertEqual(await self._find("كاربر نیم فاصله"), {555000111})
        self.assertEqual(await self._find("ALI_REZ"), {123456789})

    async def test_the_last_digits_of_an_id_match_only_inside_the_shop(self) -> None:
        self.assertEqual(await self._find("456789"), {123456789}, "the other shop's 777456789 stays hidden")
        self.assertEqual(await self._find("۴۵۶۷۸۹"), {123456789})
        self.assertEqual(await self._find("789"), set(), "three digits is not enough for a partial id")

    async def test_a_service_name_finds_its_owner_but_never_another_scopes_service(self) -> None:
        self.assertEqual(await self._find("vpn_01", with_services=True), {987654321})
        self.assertEqual(await self._find("platform_only", with_services=True), set())
        self.assertEqual(await self._find("sa", with_services=True), set(), "two letters are too short for a service name")

    async def test_wildcards_are_not_patterns(self) -> None:
        self.assertEqual(await self._find("%%"), set())
        self.assertEqual(await self._find("__"), set())
