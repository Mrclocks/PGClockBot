"""A customer sees and opens only the services the bot they are using sold."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch


from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot import keyboards  # noqa: F401  (import order: breaks a handler import cycle)
from app.bot.handlers.services import svc_link, svc_list, svc_view
from app.db import Base
from app.db.models import BotUser, Order, OrderStatus, UserService
from app.services import bot_feedback_texts as feedback
from app.services.service_scope import customer_may_use_service, list_customer_services
from app.services.users import reset_shop_reseller_id, set_shop_reseller_id
from tests.postgres_support import scoped_async_engine

SHOP_ID = 5
CUSTOMER_ID = 7
PLATFORM_SVC, SHOP_SVC, IMPORTED_SVC, BROKEN_SVC = 11, 12, 13, 14


class CustomerServiceScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=SHOP_ID, telegram_id=105, referral_code="shop5", role="reseller"),
                BotUser(id=CUSTOMER_ID, telegram_id=107, referral_code="cust", role="user", reseller_id=SHOP_ID),
                BotUser(id=8, telegram_id=108, referral_code="other", role="user"),
            ])
            await session.flush()
            session.add_all([
                Order(id=101, user_id=CUSTOMER_ID, reseller_id=None, amount=1, status=OrderStatus.DELIVERED.value),
                Order(id=102, user_id=CUSTOMER_ID, reseller_id=SHOP_ID, amount=1, status=OrderStatus.DELIVERED.value),
            ])
            await session.flush()
            for sid, remark, shop, review in (
                (PLATFORM_SVC, "order:101", None, False), (SHOP_SVC, "order:102", SHOP_ID, False),
                (IMPORTED_SVC, "imported", SHOP_ID, False), (BROKEN_SVC, "imported", None, True),
            ):
                session.add(UserService(
                    id=sid, bot_user_id=CUSTOMER_ID, pg_user_id=sid, pg_username=f"svc{sid}",
                    subscription_url=f"https://sub.example/{sid}", subscription_token=f"tok{sid}", remark=remark,
                    shop_id=shop, shop_needs_review=review,
                ))
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def listed(self, shop: int | None) -> set[int]:
        async with self.Session() as session:
            return {int(s.id) for s in await list_customer_services(session, CUSTOMER_ID, shop)}

    async def test_the_shop_bot_lists_only_its_own_and_order_less_services(self):
        self.assertEqual(await self.listed(SHOP_ID), {SHOP_SVC, IMPORTED_SVC})

    async def test_the_platform_bot_lists_only_platform_sold_services(self):
        self.assertEqual(await self.listed(None), {PLATFORM_SVC})

    async def test_a_sibling_shop_lists_nothing(self):
        self.assertEqual(await self.listed(99), set())

    async def test_the_limit_applies_after_the_shop_filter(self):
        async with self.Session() as session:
            rows = await list_customer_services(session, CUSTOMER_ID, SHOP_ID, limit=1)
        self.assertEqual([int(s.id) for s in rows], [IMPORTED_SVC])

    async def test_another_customers_service_is_never_usable(self):
        async with self.Session() as session:
            service = await session.get(UserService, SHOP_SVC)
            self.assertFalse(await customer_may_use_service(session, service, 8, SHOP_ID))
            self.assertTrue(await customer_may_use_service(session, service, CUSTOMER_ID, SHOP_ID))

    async def run_in_shop(self, shop: int | None, handler, data: str):
        callback = MagicMock(data=data, message=MagicMock())
        callback.answer = AsyncMock()
        token = set_shop_reseller_id(shop)
        try:
            async with self.Session() as session:
                customer = await session.get(BotUser, CUSTOMER_ID)
                with (
                    patch("app.bot.handlers.services.safe_edit_text", new=AsyncMock()) as edit,
                    patch("app.bot.handlers.services.fetch_live_service_info", new=AsyncMock(return_value={})),
                ):
                    await handler(callback, session, customer)
        finally:
            reset_shop_reseller_id(token)
        return callback, edit

    async def test_the_shop_bot_refuses_the_view_and_link_of_a_platform_service(self):
        for handler, prefix in ((svc_view, "svc:view"), (svc_link, "svc:link")):
            callback, edit = await self.run_in_shop(SHOP_ID, handler, f"{prefix}:{PLATFORM_SVC}")
            callback.answer.assert_awaited_once()
            self.assertEqual(callback.answer.await_args.args[0], feedback.NOT_FOUND, prefix)
            edit.assert_not_awaited()

    async def test_the_shop_bot_still_opens_its_own_service(self):
        callback, _edit = await self.run_in_shop(SHOP_ID, svc_view, f"svc:view:{SHOP_SVC}")
        self.assertNotIn(feedback.NOT_FOUND, [c.args[0] for c in callback.answer.await_args_list if c.args])

    async def test_the_shop_services_list_shows_no_platform_service_button(self):
        callback, edit = await self.run_in_shop(SHOP_ID, svc_list, "svc:list")
        markup = edit.await_args.kwargs.get("reply_markup")
        data = [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]
        self.assertIn(f"svc:view:{SHOP_SVC}", data)
        self.assertNotIn(f"svc:view:{PLATFORM_SVC}", data)

    async def test_the_mini_app_serves_only_platform_sold_services(self):
        from fastapi import HTTPException

        from app.api.miniapp_pages import _owned_service_or_404

        async with self.Session() as session:
            customer = await session.get(BotUser, CUSTOMER_ID)
            platform = await session.get(UserService, PLATFORM_SVC)
            self.assertIs(await _owned_service_or_404(session, platform, customer), platform)
            for service_id in (SHOP_SVC, IMPORTED_SVC, BROKEN_SVC):
                with self.assertRaises(HTTPException) as caught:
                    await _owned_service_or_404(session, await session.get(UserService, service_id), customer)
                self.assertEqual(caught.exception.status_code, 404, service_id)
            with self.assertRaises(HTTPException):
                await _owned_service_or_404(session, None, customer)

    async def test_a_customer_attributed_to_a_shop_can_still_buy_packs_for_a_platform_service(self):
        from app.api.miniapp_pages import _addons_allowed

        async with self.Session() as session:
            customer = await session.get(BotUser, CUSTOMER_ID)
            self.assertEqual(customer.reseller_id, SHOP_ID)
            platform = await session.get(UserService, PLATFORM_SVC)
            self.assertTrue(_addons_allowed(customer, platform))
