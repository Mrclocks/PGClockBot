"""Command center record search: shop isolation, owner-only sources, and subscription links kept secret."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.handlers import command_center
from app.db import Base
from app.db.models import BotUser, Order, Role, UserService
from app.services.command_data_search import DataHit, DataKind, DataScope, search_data
from app.services.command_search import CommandKind
from tests.postgres_support import scoped_async_engine

SHOP = 2
OTHER_SHOP = 3
TOKEN = "ShopTwoTokenAbcdef0123456789ABCDEF-_xyzq"
OTHER_TOKEN = "ShopThreeTokenAbcdef0123456789ABCDEF-_xy"
LINK = f"https://sub.example.com/sub/{TOKEN}"
OWNER_SCOPE = DataScope(shop_id=None, customers=True, resellers=True, orders=True)
SHOP_SCOPE = DataScope(shop_id=SHOP, customers=True)


class DataDb(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        import app.db.models  # noqa: F401  (registers every table)

        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all(
                [
                    BotUser(id=1, telegram_id=1001, referral_code="p", full_name="Sara Platform"),
                    BotUser(id=SHOP, telegram_id=1002, referral_code="s2", role=Role.RESELLER.value, full_name="Sara Shop"),
                    BotUser(id=OTHER_SHOP, telegram_id=1003, referral_code="s3", role=Role.RESELLER.value),
                    BotUser(id=4, telegram_id=1004, referral_code="c2", full_name="Sara Two", reseller_id=SHOP),
                    BotUser(id=5, telegram_id=1005, referral_code="c3", full_name="Sara Three", reseller_id=OTHER_SHOP),
                ]
            )
            session.add_all(
                [
                    UserService(bot_user_id=4, pg_username="two_svc", subscription_token=TOKEN, shop_id=SHOP),
                    UserService(bot_user_id=5, pg_username="three_svc", subscription_token=OTHER_TOKEN, shop_id=OTHER_SHOP),
                    Order(id=70, user_id=1, amount=1000),
                    Order(id=71, user_id=4, amount=1000, reseller_id=SHOP),
                ]
            )
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _search(self, query: str, scope: DataScope) -> list[DataHit]:
        async with self.Session() as session:
            return await search_data(session, query, scope)


class IsolationTests(DataDb):
    async def test_a_shop_finds_only_its_own_customers(self) -> None:
        hits = await self._search("Sara", SHOP_SCOPE)
        self.assertEqual([(h.kind, h.entity_id) for h in hits], [(DataKind.CUSTOMER, 4)])

    async def test_the_owner_finds_platform_customers_and_resellers_not_shop_customers(self) -> None:
        hits = await self._search("Sara", OWNER_SCOPE)
        found = {(h.kind, h.entity_id) for h in hits}
        self.assertIn((DataKind.CUSTOMER, 1), found)
        self.assertIn((DataKind.RESELLER, SHOP), found)
        self.assertFalse({4, 5} & {h.entity_id for h in hits if h.kind is DataKind.CUSTOMER})

    async def test_a_shop_without_customer_access_finds_nobody(self) -> None:
        self.assertEqual(await self._search("Sara", DataScope(shop_id=SHOP, customers=False)), [])

    async def test_a_service_name_finds_its_owner_inside_the_shop(self) -> None:
        self.assertEqual([h.entity_id for h in await self._search("two_svc", SHOP_SCOPE)], [4])
        self.assertEqual(await self._search("three_svc", SHOP_SCOPE), [])

    async def test_a_link_finds_its_owner_only_in_the_shop_that_sold_it(self) -> None:
        hits = await self._search(LINK, SHOP_SCOPE)
        self.assertEqual([(h.kind, h.entity_id) for h in hits], [(DataKind.LINK_OWNER, 4)])
        self.assertNotIn(TOKEN, hits[0].label + hits[0].detail)
        self.assertEqual(await self._search(f"https://sub.example.com/sub/{OTHER_TOKEN}", SHOP_SCOPE), [])

    async def test_order_numbers_are_found_by_the_owner_inside_the_platform_only(self) -> None:
        self.assertEqual([h.entity_id for h in await self._search("#70", OWNER_SCOPE)], [70])
        self.assertEqual(await self._search("#71", OWNER_SCOPE), [])
        self.assertEqual(await self._search("#71", SHOP_SCOPE), [])

    async def test_one_letter_finds_nobody(self) -> None:
        self.assertEqual(await self._search("S", OWNER_SCOPE), [])


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    def test_a_shop_viewer_never_gets_an_owner_card(self) -> None:
        viewer = command_center.CommandViewer([], "nv:res:home", SHOP_SCOPE, command_center.SHOP_DATA_CARDS)
        hits = [DataHit(DataKind.RESELLER, 9, "x", "نماینده"), DataHit(DataKind.CUSTOMER, 4, "y", "مشتری")]
        entries = command_center.data_entries(viewer, hits)
        self.assertEqual([e.target for e in entries], ["res:user:4"])
        self.assertEqual(entries[0].kind, CommandKind.DATA)

    async def test_a_pasted_link_is_neither_stored_nor_echoed_and_the_message_is_deleted(self) -> None:
        state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=9, user_id=9))
        message = MagicMock()
        message.text = LINK
        viewer = command_center.CommandViewer([], "nv:adm:home", OWNER_SCOPE, command_center.OWNER_DATA_CARDS)
        record = command_center.data_entries(viewer, [DataHit(DataKind.LINK_OWNER, 4, "Sara Two", "صاحب سرویس two_svc")])
        with patch.object(command_center, "load_command_viewer", AsyncMock(return_value=viewer)), patch.object(
            command_center, "find_data_entries", AsyncMock(return_value=record)
        ), patch.object(command_center, "finish_text_step", AsyncMock()) as finish, patch.object(
            command_center, "delete_user_message", AsyncMock()
        ) as delete:
            await command_center.run_command_search(message, MagicMock(), SimpleNamespace(id=1), state)
        self.assertNotIn(TOKEN, repr(await state.get_data()))
        shown = finish.await_args.kwargs
        self.assertNotIn(TOKEN, shown["text"])
        self.assertIn("adm:users:view:4", [b.callback_data for row in shown["inline"].inline_keyboard for b in row])
        delete.assert_awaited_once_with(message)

    def test_records_come_first_and_have_their_own_filter(self) -> None:
        viewer = command_center.CommandViewer(
            [command_center.CommandEntry("سارا منو", "پنل", "m:1", CommandKind.MENU)], "nv:adm:home"
        )
        record = command_center.CommandEntry("👤 سارا", "مشتری", "adm:users:view:1", CommandKind.DATA)
        _body, markup = command_center.results_panel(viewer, "سارا", records=[record])
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("cmd:r:d", callbacks)
        self.assertLess(callbacks.index("adm:users:view:1"), callbacks.index("m:1"))


if __name__ == "__main__":
    unittest.main()
