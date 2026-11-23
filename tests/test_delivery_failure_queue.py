"""Failed-delivery list: dismissing rows, one row per order, and no phantom retries."""

from __future__ import annotations

from datetime import datetime, timezone


from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import BotUser, DeliveryFailure, Order
from app.services.delivery_failure_queue import dismiss_delivery_failures
from app.services.delivery_failures import DeliveryNotRetryable, list_open_delivery_failures, retry_delivery
from tests.test_update_owner_only import OWNER_SESSION, SHOP_SESSION, RoutesHarness

OWNER = OWNER_SESSION
SHOP = {"role": "reseller", "bot_user_id": 5}
OTHER_SHOP = {"role": "reseller", "bot_user_id": 9}


class QueueHarness(RoutesHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=1, telegram_id=101, referral_code="u1", role="user"),
                BotUser(id=6, telegram_id=106, referral_code="u6", role="user", reseller_id=5),
                BotUser(id=9, telegram_id=109, referral_code="shop9", role="reseller"),
            ])
            await session.flush()
            session.add_all([
                Order(id=10, user_id=1, amount=1, status="delivered", service_id=None),  # platform, service gone
                Order(id=11, user_id=1, amount=1, status="rejected"),                      # platform, never deliverable
                Order(id=20, user_id=6, reseller_id=5, amount=1, status="delivered"),    # shop 5
            ])
            await session.flush()
            now = datetime.now(timezone.utc)
            session.add_all([
                DeliveryFailure(order_id=10, error="a", last_attempt_at=now),
                DeliveryFailure(order_id=10, error="duplicate", last_attempt_at=now),
                DeliveryFailure(order_id=11, error="b", last_attempt_at=now),
                DeliveryFailure(order_id=20, reseller_id=5, error="c", last_attempt_at=now),
            ])
            await session.commit()

    async def open_orders(self) -> set[int]:
        async with self.Session() as session:
            rows = await session.scalars(select(DeliveryFailure.order_id).where(DeliveryFailure.resolved_at.is_(None)))
            return set(rows.all())

    async def failure_total(self) -> tuple[int, int]:
        async with self.Session() as session:
            row = (await session.execute(select(func.count(), func.sum(DeliveryFailure.attempts)))).one()
            return int(row[0]), int(row[1])

    async def post(self, session_payload: dict, path: str, data: dict | None = None):
        cookie = f"session={self.signer.dumps(session_payload)}; csrf=token"
        headers = {"cookie": cookie, "x-csrf-token": "token", "origin": "http://panel.test"}
        return await self.client.post(path, headers=headers, data=data or {})


class ServiceTests(QueueHarness):
    async def test_the_list_shows_one_row_per_order(self):
        async with self.Session() as session:
            rows = await list_open_delivery_failures(session, reseller_id=None)
        self.assertEqual(sorted(r.order_id for r in rows), [10, 11])

    async def test_dismissing_resolves_every_copy_of_the_order(self):
        async with self.Session() as session:
            self.assertEqual(await dismiss_delivery_failures(session, OWNER, [10]), (1, 0))
        self.assertEqual(await self.open_orders(), {11, 20})

    async def test_dismissing_twice_is_harmless(self):
        async with self.Session() as session:
            await dismiss_delivery_failures(session, OWNER, [10, 11])
            self.assertEqual(await dismiss_delivery_failures(session, OWNER, [10, 11]), (0, 2))
        self.assertEqual(await self.open_orders(), {20})

    async def test_a_shop_cannot_dismiss_another_shops_or_the_platforms_orders(self):
        async with self.Session() as session:
            self.assertEqual(await dismiss_delivery_failures(session, OTHER_SHOP, [10, 11, 20]), (0, 3))
            self.assertEqual(await dismiss_delivery_failures(session, SHOP, [10, 11]), (0, 2))
        self.assertEqual(await self.open_orders(), {10, 11, 20})

    async def test_the_owner_cannot_dismiss_a_shops_order_and_a_shop_can_dismiss_its_own(self):
        async with self.Session() as session:
            self.assertEqual(await dismiss_delivery_failures(session, OWNER, [20]), (0, 1))
            self.assertEqual(await dismiss_delivery_failures(session, SHOP, [20]), (1, 0))

    async def test_unknown_orders_are_skipped(self):
        async with self.Session() as session:
            self.assertEqual(await dismiss_delivery_failures(session, OWNER, [999]), (0, 1))

    async def test_an_order_that_can_never_be_retried_says_so_and_adds_no_failed_attempt(self):
        before = await self.failure_total()
        for order_id in (10, 11):
            async with self.Session() as session:
                with self.assertRaises(DeliveryNotRetryable):
                    await retry_delivery(session, order_id)
        self.assertEqual(await self.failure_total(), before)


class RouteTests(QueueHarness):
    async def test_the_row_button_removes_the_order_and_returns_to_the_list(self):
        response = await self.post(OWNER_SESSION, "/orders/10/dismiss-delivery-failure")
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers["location"].startswith("/finance?tab=delivery&ok="))
        self.assertEqual(await self.open_orders(), {11, 20})

    async def test_a_shop_cannot_remove_the_platforms_order(self):
        response = await self.post(SHOP_SESSION, "/orders/10/dismiss-delivery-failure")
        self.assertIn("err=", response.headers["location"])
        self.assertEqual(await self.open_orders(), {10, 11, 20})

    async def test_the_bulk_bar_removes_the_selected_orders(self):
        response = await self.post(OWNER_SESSION, "/finance/delivery/bulk-action", {"action": "dismiss", "ids": ["10", "11"]})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(await self.open_orders(), {20})

    async def test_the_list_page_offers_both_actions_for_a_failure_row(self):
        response = await self.client.get(
            "/finance?tab=delivery",
            headers={"cookie": f"session={self.signer.dumps(OWNER_SESSION)}; csrf=token"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("/orders/10/dismiss-delivery-failure", response.text)
        self.assertEqual(response.text.count("/orders/10/retry-delivery"), 1)
        self.assertEqual(response.text.count("/orders/10/dismiss-delivery-failure"), 1)
