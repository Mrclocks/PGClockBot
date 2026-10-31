"""Owner search among resellers in the bot: same rules as the customer search, owner only."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.handlers import admin
from app.bot.handlers import subscription_lookup as bot
from app.bot.nav_input import CANCEL_REGISTRY
from app.bot.nav_inline import admin_resellers_hub_keyboard
from app.db.models import Base, BotUser
from app.services.users import DEFAULT_SETTINGS


def buttons(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


class ResellerSearchCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.tmp.name) / 'res.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add_all([
            BotUser(id=2, telegram_id=1002, referral_code="r2", role="reseller", full_name="Ali Reseller", username="alires"),
            BotUser(id=3, telegram_id=1003, referral_code="r3", role="reseller", full_name="Sara Shop Owner", username="sarashop"),
            BotUser(id=4, telegram_id=1004, referral_code="u4", role="user", full_name="Ali Customer", username="alicust"),
            BotUser(id=5, telegram_id=1005, referral_code="u5", role="admin", full_name="Ali Admin", username="aliadmin"),
        ])
        await self.session.commit()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()
        self.tmp.cleanup()


class ResellerSearchTests(ResellerSearchCase):
    async def test_only_resellers_match_by_name_and_username_parts(self):
        text, markup = await bot.staff_reseller_search(self.session, "ali")
        self.assertEqual(buttons(markup)[:-2], ["adm:resellers:view:2"])
        self.assertEqual(buttons(markup)[-2:], [bot.RESELLER_ASK, bot.RESELLER_BACK])
        self.assertIn("نتیجهٔ جستجو", text)

    async def test_names_with_spaces_and_ids_and_handles_match(self):
        for query, expected in (("sara shop", "adm:resellers:view:3"), ("1003", "adm:resellers:view:3"), ("@alires", "adm:resellers:view:2"), ("t.me/sarashop", "adm:resellers:view:3")):
            _, markup = await bot.staff_reseller_search(self.session, query)
            self.assertIn(expected, buttons(markup), query)

    async def test_customers_and_admins_are_never_listed_even_by_exact_id(self):
        for query in ("1004", "alicust", "aliadmin", "1005"):
            text, markup = await bot.staff_reseller_search(self.session, query)
            self.assertEqual(text, bot.RESELLER_NOT_FOUND_TEXT, query)
            self.assertEqual([c for c in buttons(markup) if c.startswith("adm:resellers:view:")], [])

    async def test_wildcards_are_literal_and_one_letter_is_refused(self):
        for query in ("%%", "__"):
            text, _ = await bot.staff_reseller_search(self.session, query)
            self.assertEqual(text, bot.RESELLER_NOT_FOUND_TEXT, query)
        text, _ = await bot.staff_reseller_search(self.session, "a")
        self.assertEqual(text, bot.SHORT_QUERY_TEXT)

    async def test_results_are_bounded_and_say_when_there_are_more(self):
        self.session.add_all([BotUser(id=100 + i, telegram_id=7000 + i, referral_code=f"m{i}", role="reseller", full_name=f"Many Shops {i}") for i in range(12)])
        await self.session.commit()
        text, markup = await bot.staff_reseller_search(self.session, "many shops")
        self.assertIn("دقیق‌تر", text)
        self.assertEqual(len([c for c in buttons(markup) if c.startswith("adm:resellers:view:")]), 10)


class OwnerStepTests(ResellerSearchCase):
    async def run_handler(self, handler, *args, manage=True, owner=True):
        with patch("app.bot.auth.is_bot_owner_principal", AsyncMock(return_value=owner)), \
                patch("app.bot.auth.platform_can_manage_representatives", AsyncMock(return_value=manage)), \
                patch.object(admin, "_is_admin", return_value=True):
            return await handler(*args)

    async def search(self, text, **kw):
        message = AsyncMock()
        message.text = text
        state = AsyncMock()
        with patch.object(admin, "_reseller_card", AsyncMock(return_value=("card", None))) as card, \
                patch.object(admin, "finish_text_step", AsyncMock()) as finish:
            await self.run_handler(admin.adm_resellers_search, message, state, self.session, SimpleNamespace(id=1, telegram_id=1, role="admin", is_blocked=False), **kw)
        return message, finish, card

    async def test_an_exact_handle_opens_the_card_and_a_part_lists_matches(self):
        _, finish, card = await self.search("@sarashop")
        card.assert_awaited_once()
        self.assertEqual(card.await_args.args[1].id, 3)
        self.assertEqual(finish.await_args.kwargs["text"], "card")
        message, finish, card = await self.search("ali")
        card.assert_not_awaited()
        message.answer.assert_not_awaited()  # the prompt becomes the result; no extra message
        self.assertIn("adm:resellers:view:2", buttons(finish.await_args.kwargs["inline"]))

    async def test_an_exact_customer_id_is_not_opened_as_a_reseller(self):
        _, finish, card = await self.search("alicust")
        card.assert_not_awaited()
        self.assertEqual(finish.await_args.kwargs["text"], bot.RESELLER_NOT_FOUND_TEXT)

    async def test_overlong_text_is_refused_and_a_name_with_spaces_works(self):
        message, finish, _ = await self.search("x " * 33)
        self.assertEqual(len(message.answer.await_args_list), 1)
        finish.assert_not_awaited()
        _, finish, _ = await self.search("sara shop")
        self.assertIn("adm:resellers:view:3", buttons(finish.await_args.kwargs["inline"]))

    async def test_a_non_owner_or_disabled_representative_management_searches_nothing(self):
        for kwargs in ({"owner": False}, {"manage": False}):
            message = AsyncMock()
            message.text = "ali"
            with patch.object(admin, "_reseller_card", AsyncMock()) as card, \
                    patch.object(admin, "staff_reseller_search", AsyncMock()) as search:
                await self.run_handler(admin.adm_resellers_search, message, AsyncMock(), self.session, SimpleNamespace(id=1, telegram_id=1, role="admin", is_blocked=False), **kwargs)
            card.assert_not_awaited()
            search.assert_not_awaited()

    async def test_the_start_step_lists_what_can_be_searched_and_cancel_returns_to_the_hub(self):
        callback = AsyncMock()
        callback.message = AsyncMock()
        with patch("app.bot.nav_input.ask_text", AsyncMock()) as ask:
            await self.run_handler(admin.adm_resellers_search_start, callback, AsyncMock(), SimpleNamespace(id=1, telegram_id=1, role="admin", is_blocked=False))
        prompt = ask.await_args.kwargs["prompt"]
        for part in ("نام", "یوزرنیم", "آیدی عددی"):
            self.assertIn(part, prompt)
        self.assertEqual(ask.await_args.kwargs["return_to"], "nv:adm:resellers")
        import app.bot.staff_cancel  # noqa: F401 - registers the cancel codes

        self.assertIn(ask.await_args.kwargs["cancel_code"], CANCEL_REGISTRY)


class HubTests(unittest.TestCase):
    def test_the_resellers_hub_has_a_search_button_between_list_and_requests(self):
        found = buttons(admin_resellers_hub_keyboard({}))
        self.assertIn("adm:resellers:search", found)
        self.assertLess(found.index("adm:resellers:list:0"), found.index("adm:resellers:search"))
        self.assertLess(found.index("adm:resellers:search"), found.index("adm:resapp:list"))

    def test_the_label_is_unique_so_a_tap_resolves_to_one_action(self):
        self.assertEqual(DEFAULT_SETTINGS["btn_adm_res_search"], "🔎 جستجوی نماینده")
        self.assertNotEqual(DEFAULT_SETTINGS["btn_adm_res_search"], DEFAULT_SETTINGS["btn_adm_users_search"])


if __name__ == "__main__":
    unittest.main()
