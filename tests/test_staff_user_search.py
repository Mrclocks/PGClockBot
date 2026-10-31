"""Staff search of customers by name, @username or id: shop scope, bounds and wildcard safety."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.handlers import admin
from app.bot.handlers import subscription_lookup as bot
from app.db.models import Base, BotUser
from app.services.user_search import search_bot_users


def buttons(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def labels(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


class SearchCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.tmp.name) / 'search.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add_all([
            BotUser(id=2, telegram_id=1002, referral_code="shop2", role="reseller", full_name="Shop Two"),
            BotUser(id=3, telegram_id=1003, referral_code="shop3", role="reseller", full_name="Shop Three"),
        ])
        await self.session.flush()
        self.session.add_all([
            BotUser(id=10, telegram_id=5010, referral_code="p1", full_name="Ali Rezaei", username="ali_r"),
            BotUser(id=11, telegram_id=5011, referral_code="p2", full_name="Sara Alinejad", username="sara"),
            BotUser(id=12, telegram_id=5012, referral_code="s2a", full_name="Ali Shop Two", username="alitwo", reseller_id=2),
            BotUser(id=13, telegram_id=5013, referral_code="s3a", full_name="Ali Shop Three", username="alithree", reseller_id=3),
            BotUser(id=14, telegram_id=5014, referral_code="s2b", full_name="<b>Bold</b> Name", username="boldy", reseller_id=2),
        ])
        await self.session.commit()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()
        self.tmp.cleanup()


class SearchTests(SearchCase):
    async def test_platform_search_matches_name_and_username_parts_in_the_platform_shop_only(self):
        text, markup = await bot.staff_user_search(self.session, "ali", shop_id=None)
        self.assertEqual(buttons(markup)[:-2], ["adm:users:view:11", "adm:users:view:10"])
        self.assertEqual(buttons(markup)[-2:], [bot.PLATFORM_ASK, bot.PLATFORM_BACK])
        self.assertIn("نتیجهٔ جستجو", text)
        self.assertNotIn("adm:users:view:12", buttons(markup))
        self.assertNotIn("adm:users:view:13", buttons(markup))

    async def test_a_shop_finds_only_its_own_customers(self):
        _, markup = await bot.staff_user_search(self.session, "ali", shop_id=2)
        self.assertEqual([c for c in buttons(markup) if c.startswith("res:user:")], ["res:user:12"])
        _, markup = await bot.staff_user_search(self.session, "alithree", shop_id=2)
        self.assertEqual([c for c in buttons(markup) if c.startswith("res:user:")], [])
        text, _ = await bot.staff_user_search(self.session, "alithree", shop_id=2)
        self.assertEqual(text, bot.USER_NOT_FOUND_TEXT)
        _, markup = await bot.staff_user_search(self.session, "ali", shop_id=3)
        self.assertEqual([c for c in buttons(markup) if c.startswith("res:user:")], ["res:user:13"])

    async def test_a_shop_owner_account_is_not_listed_as_a_customer(self):
        _, markup = await bot.staff_user_search(self.session, "shop", shop_id=2)
        self.assertNotIn("res:user:2", buttons(markup))
        self.assertNotIn("res:user:3", buttons(markup))

    async def test_id_and_username_and_names_with_spaces_match(self):
        for query, shop, expected in (("5011", None, "adm:users:view:11"), ("@sara", None, "adm:users:view:11"), ("sara alinejad", None, "adm:users:view:11"), ("t.me/boldy", 2, "res:user:14")):
            _, markup = await bot.staff_user_search(self.session, query, shop_id=shop)
            self.assertIn(expected, buttons(markup), query)

    async def test_wildcards_are_literal_and_one_letter_is_refused(self):
        for query in ("%%", "__", "_%"):
            text, markup = await bot.staff_user_search(self.session, query, shop_id=None)
            self.assertEqual(text, bot.USER_NOT_FOUND_TEXT, query)
            self.assertEqual([c for c in buttons(markup) if c.startswith("adm:users:view:")], [])
        for query in ("a", "%"):
            text, _ = await bot.staff_user_search(self.session, query, shop_id=None)
            self.assertEqual(text, bot.SHORT_QUERY_TEXT)
        text, _ = await bot.staff_user_search(self.session, "", shop_id=None)
        self.assertEqual(text, bot.SHORT_QUERY_TEXT)

    async def test_results_are_bounded_and_say_when_there_are_more(self):
        self.session.add_all([BotUser(id=100 + i, telegram_id=7000 + i, referral_code=f"m{i}", full_name=f"Many Match {i}") for i in range(12)])
        await self.session.commit()
        users, more = await search_bot_users(self.session, "many match", scope=BotUser.reseller_id.is_(None))
        self.assertEqual((len(users), more), (10, True))
        text, markup = await bot.staff_user_search(self.session, "many match", shop_id=None)
        self.assertIn("دقیق‌تر", text)
        self.assertEqual(len([c for c in buttons(markup) if c.startswith("adm:users:view:")]), 10)

    async def test_names_never_reach_html_text_and_labels_are_bounded(self):
        text, markup = await bot.staff_user_search(self.session, "bold", shop_id=2)
        self.assertNotIn("Bold", text)
        self.assertTrue(all(len(label) <= 64 for label in labels(markup)))

    async def test_a_link_still_goes_to_the_subscription_lookup(self):
        text, _ = await bot.staff_search(self.session, "https://sub.example.com/sub/UnknownTokenAbcdef0123456789ABCDEF-_xyzqrs", shop_id=2)
        self.assertEqual(text, bot.NOT_FOUND_TEXT)


class TypedStepTests(SearchCase):
    async def receive(self, text):
        message = AsyncMock()
        message.text = text
        user = SimpleNamespace(id=9, telegram_id=777)
        with patch.object(bot, "shop_scope", AsyncMock(return_value=2)), patch.object(bot, "finish_text_step", AsyncMock()) as finish:
            await bot.receive_link(message, AsyncMock(), self.session, user, is_reseller_bot=False, reseller_owner_id=None)
        return message, finish

    async def test_a_name_search_keeps_the_message_and_a_link_deletes_it(self):
        message, finish = await self.receive("ali")
        self.assertIn("res:user:12", buttons(finish.await_args.kwargs["inline"]))
        message.delete.assert_not_awaited()
        message, _ = await self.receive("https://sub.example.com/sub/UnknownTokenAbcdef0123456789ABCDEF-_xyzqrs")
        message.delete.assert_awaited_once()


class PlatformStepTests(SearchCase):
    async def search(self, text):
        message = AsyncMock()
        message.text = text
        state = AsyncMock()
        with patch("app.bot.auth.is_bot_owner_principal", AsyncMock(return_value=True)), \
                patch.object(admin, "_is_admin", return_value=True), \
                patch.object(admin, "finish_text_step", AsyncMock()) as finish:
            await admin.adm_users_search(message, state, self.session, SimpleNamespace(id=1, telegram_id=1, role="admin", is_blocked=False), is_reseller_bot=False)
        return message, finish

    async def test_a_partial_name_lists_matches_and_an_exact_handle_still_opens_the_card(self):
        _, finish = await self.search("ali")
        self.assertIn("adm:users:view:10", buttons(finish.await_args.kwargs["inline"]))
        _, finish = await self.search("@sara")
        self.assertIn("adm:users:svcs:11", buttons(finish.await_args.kwargs["inline"]))
        self.assertIn("5011", finish.await_args.kwargs["text"])

    async def test_a_name_with_spaces_is_accepted_and_too_long_text_is_refused(self):
        _, finish = await self.search("sara alinejad")
        self.assertIn("adm:users:view:11", buttons(finish.await_args.kwargs["inline"]))
        message, finish = await self.search("x " * 33)
        finish.assert_not_awaited()
        self.assertEqual(len(message.answer.await_args_list), 1)


if __name__ == "__main__":
    unittest.main()
