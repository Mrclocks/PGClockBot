"""Long lists open page by page, nothing past a page is unreachable, and services can be searched."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot import keyboards as kb
from app.bot.handlers import admin_plans, service_search
from app.bot.list_pages import page_number, page_of
from app.bot.nav_inline import services_list_keyboard
from app.db import Base
from app.db.models import BotUser, UserService
from app.services.service_scope import search_customer_services
from tests.postgres_support import scoped_async_engine


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


def _services(count: int) -> list[SimpleNamespace]:
    return [SimpleNamespace(id=i, pg_username=f"user_{i}") for i in range(1, count + 1)]


class PageTests(unittest.TestCase):
    def test_a_plain_list_callback_is_the_first_page(self) -> None:
        self.assertEqual(page_number("svc:list"), 0)
        self.assertEqual(page_number("svc:list:p:3"), 3)
        for forged in ("svc:list:p:-1", "svc:list:p:x", "svc:list:p:", None):
            self.assertEqual(page_number(forged), 0)

    def test_a_page_past_the_end_shows_the_last_page(self) -> None:
        page = page_of(list(range(45)), 99, 20)
        self.assertEqual((page.index, page.count, page.items), (2, 3, list(range(40, 45))))


class ServiceListTests(unittest.TestCase):
    def test_every_service_of_a_long_list_is_reachable(self) -> None:
        services = _services(45)
        seen: set[str] = set()
        for index in range(5):
            seen |= {c for c in _callbacks(services_list_keyboard(services, {}, page_index=index)) if c.startswith("svc:view:")}
        self.assertEqual(seen, {f"svc:view:{i}" for i in range(1, 46)})

    def test_a_long_list_pages_and_offers_search(self) -> None:
        services = _services(45)
        first = _callbacks(services_list_keyboard(services, {}))
        self.assertIn("svc:list:p:1", first)
        self.assertNotIn("svc:list:p:0", first)
        self.assertIn("svc:find", first)
        self.assertIn("svc:list:p:1", _callbacks(services_list_keyboard(services, {}, page_index=2)))

    def test_a_short_list_has_no_pager_or_search(self) -> None:
        data = _callbacks(services_list_keyboard(_services(5), {}))
        self.assertFalse([c for c in data if ":p:" in c or c == "svc:find"])


class PlanListTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_platform_fixed_plans_open_page_by_page(self) -> None:
        plans = [SimpleNamespace(id=i, name=f"P{i}", is_active=True, is_trial=False, pg_template_id=1) for i in range(1, 22)]
        session = MagicMock(execute=AsyncMock(return_value=MagicMock(scalars=lambda: MagicMock(all=lambda: plans))))
        with (
            patch.object(admin_plans, "get_all_settings", AsyncMock(return_value={})),
            patch("app.bot.handlers.admin._plan_line", side_effect=lambda p: f"card {p.id}"),
        ):
            text, markup = await admin_plans.users_fixed_list_view(session, 2)
        data = _callbacks(markup)
        self.assertEqual([c for c in data if c.startswith("adm:plan:view:")], ["adm:plan:view:21"])
        self.assertIn("adm:plans:kind:users:fixed:p:1", data)
        self.assertIn("card 21", text)
        self.assertIn("صفحه ۳ از ۳", text)

    async def test_a_page_callback_opens_that_page(self) -> None:
        callback = MagicMock(data="adm:plans:kind:resellers:payg:p:2", answer=AsyncMock())
        callback.message = MagicMock(edit_text=AsyncMock(), delete=AsyncMock())
        with (
            patch("app.bot.auth.require_bot_owner", AsyncMock(return_value=True)),
            patch.object(admin_plans, "_is_admin", return_value=True),
            patch.object(admin_plans, "open_kind_screen", AsyncMock()) as opened,
        ):
            await admin_plans.plans_kind_cb(callback, MagicMock(), MagicMock(), AsyncMock())
        self.assertEqual(opened.await_args.args[2:], ("resellers", "payg", 2))

    def test_shop_plans_page_too(self) -> None:
        plans = [SimpleNamespace(id=i, name=f"P{i}", is_active=True, price=1000) for i in range(1, 13)]
        second = _callbacks(kb.reseller_plans_list_keyboard(plans, {}, page_index=1))
        self.assertEqual([c for c in second if c.startswith("res:plan:view:")], ["res:plan:view:11", "res:plan:view:12"])
        self.assertIn("res:plans", second)


class ServiceSearchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=5, telegram_id=105, referral_code="shop", role="reseller"),
                BotUser(id=7, telegram_id=107, referral_code="me", role="user"),
                BotUser(id=8, telegram_id=108, referral_code="other", role="user"),
            ])
            await session.flush()
            for sid, owner, name, shop in (
                (1, 7, "Ali_Home", None), (2, 7, "ali_work", None), (3, 7, "reza", None),
                (4, 8, "ali_other_customer", None), (5, 7, "ali_other_shop", 5), (6, 7, "100%_safe", None),
            ):
                session.add(UserService(id=sid, bot_user_id=owner, pg_username=name, shop_id=shop, shop_needs_review=False))
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _found(self, text: str, shop: int | None = None) -> list[int]:
        async with self.Session() as session:
            return [int(s.id) for s in await search_customer_services(session, 7, shop, text, limit=20)]

    async def test_search_finds_only_the_customers_services_in_this_shop(self) -> None:
        self.assertEqual(await self._found("ALI"), [2, 1])
        self.assertEqual(await self._found("ali", shop=5), [5])

    async def test_wildcards_are_plain_letters(self) -> None:
        self.assertEqual(await self._found("%"), [6])
        self.assertEqual(await self._found("_"), [6, 2, 1])

    async def test_empty_or_too_long_text_is_refused(self) -> None:
        for text in ("   ", "x" * 65):
            with self.subTest(length=len(text)), self.assertRaises(ValueError):
                await self._found(text)

    async def test_results_show_the_typed_text_escaped(self) -> None:
        message = MagicMock(text="<b>ali</b>")
        with (
            patch.object(service_search, "search_customer_services", AsyncMock(return_value=[])),
            patch.object(service_search, "finish_text_step", AsyncMock()) as shown,
        ):
            await service_search.show_service_search(message, AsyncMock(), MagicMock(), SimpleNamespace(id=7))
        self.assertIn("&lt;b&gt;ali&lt;/b&gt;", shown.await_args.kwargs["text"])
        self.assertEqual(_callbacks(shown.await_args.kwargs["inline"]), ["svc:find", "svc:list"])


if __name__ == "__main__":
    unittest.main()
