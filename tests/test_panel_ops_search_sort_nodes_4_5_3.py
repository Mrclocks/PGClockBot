"""Panel ops: search, sort, order/payment actions, node traffic (4.5.3)."""

from __future__ import annotations

import unittest
from tests.postgres_support import scoped_async_engine


class ListQueryTests(unittest.TestCase):
    def test_case_insensitive_substring(self):
        from app.services.list_query import filter_by_search, text_matches

        self.assertTrue(text_matches("AliReza", needle="ali"))
        self.assertTrue(text_matches("علی‌رضا", needle="علی"))
        self.assertTrue(text_matches("User01", needle="ER0"))
        self.assertFalse(text_matches("abc", needle="xyz"))
        rows = [{"name": "Alpha"}, {"name": "betaTest"}, {"name": "GAMMA"}]
        out = filter_by_search(rows, "eta", lambda r: (r["name"],))
        self.assertEqual([r["name"] for r in out], ["betaTest"])
        self.assertEqual(filter_by_search(rows, "", lambda r: (r["name"],)), rows)

    def test_ilike_escapes_wildcards(self):
        from app.services.list_query import ilike_pattern

        self.assertEqual(ilike_pattern("a%b_c"), r"%a\%b\_c%")


class NodeTrafficTests(unittest.TestCase):
    def test_enrich_merges_realtime_and_formats(self):
        from app.services.node_traffic import enrich_nodes_with_traffic

        nodes = [{"id": 1, "name": "n1"}, {"id": 2, "name": "n2", "upload": 100}]
        rt = {"1": {"uplink": 1024, "downlink": 2048}, "nodes": []}
        # id-keyed map path
        out = enrich_nodes_with_traffic(nodes, {"1": {"uplink": 1024, "downlink": 2048}})
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["_traffic_up"], 1024)
        self.assertEqual(out[0]["_traffic_down"], 2048)
        self.assertEqual(out[0]["_traffic_total"], 3072)
        self.assertNotEqual(out[0]["_traffic_total_text"], "—")
        self.assertEqual(out[1]["_traffic_up"], 100)

    def test_list_payload_shape(self):
        from app.services.node_traffic import enrich_nodes_with_traffic

        out = enrich_nodes_with_traffic(
            [{"id": 7, "name": "x"}],
            {"nodes": [{"node_id": 7, "upload": 10, "download": 20}]},
        )
        self.assertEqual(out[0]["_traffic_total"], 30)


class OrdersCancelServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_order_rejects_pending_payment(self):
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from app.db.models import Base, BotUser, Order, OrderStatus, Payment, PaymentStatus, Role
        from app.services.orders import cancel_order

        engine = scoped_async_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = BotUser(telegram_id=111, role=Role.USER.value, username="u1", referral_code="ref111")
            session.add(user)
            await session.commit()
            await session.refresh(user)
            order = Order(user_id=user.id, amount=1000, status=OrderStatus.PENDING.value)
            session.add(order)
            await session.commit()
            await session.refresh(order)
            pay = Payment(
                order_id=order.id,
                user_id=user.id,
                amount=1000,
                status=PaymentStatus.PENDING.value,
            )
            session.add(pay)
            await session.commit()
            await session.refresh(pay)
            await cancel_order(session, order, note="test cancel")
            await session.refresh(order)
            await session.refresh(pay)
            self.assertEqual(order.status, OrderStatus.CANCELLED.value)
            self.assertEqual(pay.status, PaymentStatus.REJECTED.value)

    async def test_cancel_blocks_delivered(self):
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from app.db.models import Base, BotUser, Order, OrderStatus, Role
        from app.services.orders import cancel_order

        engine = scoped_async_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = BotUser(telegram_id=222, role=Role.USER.value, referral_code="ref222")
            session.add(user)
            await session.commit()
            await session.refresh(user)
            order = Order(user_id=user.id, amount=1, status=OrderStatus.DELIVERED.value)
            session.add(order)
            await session.commit()
            await session.refresh(order)
            with self.assertRaises(ValueError):
                await cancel_order(session, order)


class SingleDeliveryGuaranteeTests(unittest.IsolatedAsyncioTestCase):
    async def _session(self):
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from app.db.models import Base

        engine = scoped_async_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        return async_sessionmaker(engine, expire_on_commit=False)

    async def test_second_payment_rejected_when_service_already_linked(self):
        """Even if status was reset away from delivered, do not mint again."""
        from app.db.models import (
            BotUser,
            Order,
            OrderStatus,
            Payment,
            PaymentStatus,
            Role,
            UserService,
        )
        from app.services.orders import approve_payment

        Session = await self._session()
        async with Session() as session:
            user = BotUser(telegram_id=301, role=Role.USER.value, referral_code="r301")
            session.add(user)
            await session.commit()
            await session.refresh(user)
            order = Order(
                user_id=user.id,
                amount=5000,
                status=OrderStatus.AWAITING_APPROVAL.value,  # tampered
            )
            session.add(order)
            await session.commit()
            await session.refresh(order)
            svc = UserService(
                bot_user_id=user.id,
                pg_username="already",
                remark=f"order:{order.id}",
            )
            session.add(svc)
            await session.commit()
            await session.refresh(svc)
            order.service_id = svc.id
            await session.commit()
            pay = Payment(
                order_id=order.id,
                user_id=user.id,
                amount=5000,
                status=PaymentStatus.PENDING.value,
            )
            session.add(pay)
            await session.commit()
            await session.refresh(pay)
            with self.assertRaises(ValueError) as ctx:
                await approve_payment(session, pay, reviewer_tg=0)
            self.assertIn("قبلاً تحویل", str(ctx.exception))
            await session.refresh(pay)
            self.assertEqual(pay.status, PaymentStatus.REJECTED.value)

    async def test_deliver_order_idempotent_with_prior_remark(self):
        from app.db.models import BotUser, Order, OrderStatus, Role, UserService
        from app.services.orders import deliver_order

        Session = await self._session()
        async with Session() as session:
            user = BotUser(telegram_id=302, role=Role.USER.value, referral_code="r302")
            session.add(user)
            await session.commit()
            await session.refresh(user)
            order = Order(user_id=user.id, amount=1, status=OrderStatus.PAID.value)
            session.add(order)
            await session.commit()
            await session.refresh(order)
            svc = UserService(
                bot_user_id=user.id,
                pg_username="prior",
                remark=f"order:{order.id}",
            )
            session.add(svc)
            await session.commit()
            await session.refresh(svc)
            out = await deliver_order(session, order)
            await session.refresh(order)
            self.assertEqual(out.status, OrderStatus.DELIVERED.value)
            self.assertEqual(order.service_id, svc.id)
            # second call still no-op
            out2 = await deliver_order(session, order)
            self.assertEqual(out2.service_id, svc.id)

    async def test_cancel_blocked_after_approved_payment(self):
        from app.db.models import (
            BotUser,
            Order,
            OrderStatus,
            Payment,
            PaymentStatus,
            Role,
        )
        from app.services.orders import cancel_order

        Session = await self._session()
        async with Session() as session:
            user = BotUser(telegram_id=303, role=Role.USER.value, referral_code="r303")
            session.add(user)
            await session.commit()
            await session.refresh(user)
            order = Order(
                user_id=user.id,
                amount=100,
                status=OrderStatus.AWAITING_APPROVAL.value,
            )
            session.add(order)
            await session.commit()
            await session.refresh(order)
            session.add(
                Payment(
                    order_id=order.id,
                    user_id=user.id,
                    amount=100,
                    status=PaymentStatus.APPROVED.value,
                )
            )
            await session.commit()
            with self.assertRaises(ValueError):
                await cancel_order(session, order)

    async def test_cancel_blocked_when_service_linked(self):
        from app.db.models import BotUser, Order, OrderStatus, Role, UserService
        from app.services.orders import cancel_order

        Session = await self._session()
        async with Session() as session:
            user = BotUser(telegram_id=304, role=Role.USER.value, referral_code="r304")
            session.add(user)
            await session.commit()
            await session.refresh(user)
            order = Order(
                user_id=user.id,
                amount=100,
                status=OrderStatus.AWAITING_APPROVAL.value,
            )
            session.add(order)
            await session.commit()
            await session.refresh(order)
            svc = UserService(bot_user_id=user.id, pg_username="x", remark=f"order:{order.id}")
            session.add(svc)
            await session.commit()
            await session.refresh(svc)
            order.service_id = svc.id
            await session.commit()
            with self.assertRaises(ValueError):
                await cancel_order(session, order)


if __name__ == "__main__":
    unittest.main()
