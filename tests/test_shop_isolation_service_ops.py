"""Staff may act only on services their own shop sold."""

from __future__ import annotations



from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, Order, OrderStatus, UserService
from app.services.bot_user_admin import get_owned_service
from app.services.service_scope import SERVICE_OUTSIDE_SHOP_MESSAGE
from app.services.users_quick import quick_renew_user
from tests.test_update_owner_only import OWNER_SESSION, SHOP_SESSION, RoutesHarness

SHOP_ID = 5
CUSTOMER_ID = 7
PLATFORM_SVC, SHOP_SVC, IMPORTED_SVC, BROKEN_SVC = 11, 12, 13, 14


class ServiceShopGuardTests(RoutesHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        async with AsyncSession(bind=self.engine, expire_on_commit=False) as session:
            # The customer is attributed to the shop but bought from both places.
            session.add(BotUser(id=CUSTOMER_ID, telegram_id=107, referral_code="cust", role="user", reseller_id=SHOP_ID))
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

    async def load(self, service_id: int, shop_id: int | None):
        async with AsyncSession(bind=self.engine) as session:
            return await get_owned_service(
                session, bot_user_id=CUSTOMER_ID, service_id=service_id, shop_id=shop_id
            )

    async def test_a_shop_cannot_load_a_platform_sold_service_of_its_attributed_customer(self):
        with self.assertRaisesRegex(ValueError, SERVICE_OUTSIDE_SHOP_MESSAGE):
            await self.load(PLATFORM_SVC, SHOP_ID)

    async def test_the_platform_cannot_load_a_shop_sold_service(self):
        with self.assertRaisesRegex(ValueError, SERVICE_OUTSIDE_SHOP_MESSAGE):
            await self.load(SHOP_SVC, None)

    async def test_each_side_loads_the_services_it_sold(self):
        self.assertEqual((await self.load(PLATFORM_SVC, None)).id, PLATFORM_SVC)
        self.assertEqual((await self.load(SHOP_SVC, SHOP_ID)).id, SHOP_SVC)

    async def test_a_service_without_an_order_follows_the_customers_shop(self):
        self.assertEqual((await self.load(IMPORTED_SVC, SHOP_ID)).id, IMPORTED_SVC)
        with self.assertRaises(ValueError):
            await self.load(IMPORTED_SVC, None)

    async def test_a_service_under_review_is_refused_everywhere(self):
        for shop in (None, SHOP_ID):
            with self.assertRaises(ValueError):
                await self.load(BROKEN_SVC, shop)

    async def test_a_sibling_shop_cannot_load_the_service(self):
        with self.assertRaises(ValueError):
            await self.load(SHOP_SVC, 99)

    async def test_quick_renew_refuses_a_platform_service_from_the_shop(self):
        async with AsyncSession(bind=self.engine) as session:
            customer = await session.get(BotUser, CUSTOMER_ID)
            # The shop's service list does not contain the platform service at all.
            with self.assertRaises(ValueError):
                await quick_renew_user(session, customer, service_id=PLATFORM_SVC, shop_id=SHOP_ID)

    async def get_as(self, session_payload: dict, path: str):
        cookie = f"session={self.signer.dumps(session_payload)}; csrf=token"
        headers = {"cookie": cookie, "x-csrf-token": "token", "origin": "http://panel.test"}
        return await self.client.get(path, headers=headers)

    async def test_the_panel_will_not_give_a_shop_the_link_of_a_platform_service(self):
        response = await self.get_as(SHOP_SESSION, f"/users/{CUSTOMER_ID}/services/{PLATFORM_SVC}/link")
        self.assertEqual(response.status_code, 404)
        self.assertIn(SERVICE_OUTSIDE_SHOP_MESSAGE, response.text)
        self.assertNotIn(f"tok{PLATFORM_SVC}", response.text)
        self.assertNotIn("sub.example", response.text)

    async def test_the_owner_still_reaches_a_platform_service_link_of_a_platform_customer(self):
        async with AsyncSession(bind=self.engine) as session:
            customer = await session.get(BotUser, CUSTOMER_ID)
            customer.reseller_id = None
            await session.commit()
        response = await self.get_as(OWNER_SESSION, f"/users/{CUSTOMER_ID}/services/{PLATFORM_SVC}/link")
        self.assertIn(response.status_code, {200, 404, 502})
        self.assertNotIn(SERVICE_OUTSIDE_SHOP_MESSAGE, response.text)

    async def test_the_panel_client_is_chosen_by_the_services_shop_not_the_customers(self):
        from unittest.mock import AsyncMock, patch

        from app.services.bot_user_admin import _pg_client_for_bot_service

        picked: list[int] = []

        async def fake_for_reseller(_session, rid):
            picked.append(int(rid))
            return object()

        sentinel = object()
        with (
            patch("app.services.pasarguard.get_pg_for_reseller", new=AsyncMock(side_effect=fake_for_reseller)),
            patch("app.services.bot_user_admin.get_pg", return_value=sentinel),
        ):
            async with AsyncSession(bind=self.engine) as session:
                platform_client = await _pg_client_for_bot_service(session, await session.get(UserService, PLATFORM_SVC))
                await _pg_client_for_bot_service(session, await session.get(UserService, SHOP_SVC))
        self.assertIs(platform_client, sentinel)
        self.assertEqual(picked, [SHOP_ID])
