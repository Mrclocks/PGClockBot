"""Cleanup tab: delete orders and payments by status tag, from the root, within the shop only."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import httpx
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services.order_purge import (
    KEY_AUTO,
    KEY_DAYS,
    KEY_TAGS,
    PurgeRules,
    auto_purge_all_shops,
    count_purgeable,
    parse_days,
    parse_tags,
    purge_panel_context,
    purge_scope,
    run_purge,
    save_rules,
)

SHOP_A = 81
SHOP_B = 82
OLD = datetime.now(timezone.utc) - timedelta(days=60)
OWNER = {
    "role": "admin", "id": 1, "username": "root", "org_principal_id": 1,
    "org_depth": 0, "org_parent_id": None, "org_status": "active",
}


def _reseller(shop_id: int, perms: list[str] | None = None) -> dict:
    return {"role": "reseller", "bot_user_id": shop_id, "id": shop_id, "permissions": ["orders_purge"] if perms is None else perms}


class TagAndScopeTests(unittest.TestCase):
    def test_paid_delivering_and_unknown_tags_are_dropped(self):
        self.assertEqual(
            parse_tags("order:delivered, order:paid,order:delivering,payment:rejected,order:nonsense,x"),
            frozenset({"order:delivered", "payment:rejected"}),
        )

    def test_days(self):
        self.assertEqual(parse_days("30"), 30)
        for bad in ("0", "-1", "3651", "1.5", "", "abc", "۳۰", True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_days(bad)

    def test_scope(self):
        self.assertIsNone(purge_scope(OWNER))
        self.assertEqual(purge_scope(_reseller(SHOP_A)), SHOP_A)
        for staff in (_reseller(SHOP_A, ["orders", "shop_settings"]), {"role": "pg_staff", "permissions": ["orders_purge"]}, _reseller(0)):
            with self.subTest(staff=staff), self.assertRaises(PermissionError):
                purge_scope(staff)


class PurgeDbHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import Base
        from app.db.models import BotUser

        self._tmpdir = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{Path(self._tmpdir.name) / 'purge.db'}", connect_args={"timeout": 15}
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            for shop in (SHOP_A, SHOP_B):
                session.add(BotUser(id=shop, telegram_id=shop * 10, referral_code=f"s{shop}", role="reseller"))
            session.add(BotUser(id=500, telegram_id=5000, referral_code="c500"))
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        self._tmpdir.cleanup()

    async def _order(self, status: str, *, shop: int | None = SHOP_A, created=OLD, payment_status: str | None = "approved") -> tuple[int, int | None]:
        from app.db.models import Order, Payment

        async with self.Session() as session:
            order = Order(user_id=500, amount=1000, status=status, reseller_id=shop, created_at=created)
            session.add(order)
            await session.flush()
            payment_id = None
            if payment_status:
                payment = Payment(user_id=500, amount=1000, order_id=order.id, status=payment_status, method="card", created_at=created)
                session.add(payment)
                await session.flush()
                payment_id = int(payment.id)
            await session.commit()
            return int(order.id), payment_id

    async def _count(self, model, *where) -> int:
        async with self.Session() as session:
            return int((await session.execute(select(func.count()).select_from(model).where(*where))).scalar_one())

    async def _run(self, shop_id, tags, days=30):
        async with self.Session() as session:
            return await run_purge(session, shop_id=shop_id, tags=frozenset(tags), days=days)


class PurgeTests(PurgeDbHarness):
    async def test_a_delivered_order_goes_from_the_root_but_history_and_services_stay(self):
        from app.db.models import (
            ChargeCode, ChargeCodeUse, DeliveryFailure, Order, Payment, PaymentReceiptFingerprint,
            PaymentReviewMessage, PaymentSettlement, TrialClaim, UserService,
        )

        order_id, payment_id = await self._order("delivered")
        async with self.Session() as session:
            service = UserService(bot_user_id=500, pg_username="svc1", shop_id=SHOP_A)
            code = ChargeCode(code="USED1", amount=100, reseller_id=SHOP_A)
            session.add_all([service, code])
            await session.flush()
            session.add_all([
                PaymentSettlement(payment_id=payment_id, tenant_key="t", channel="psp", provider="x", amount=1000,
                                  idempotency_key="k1", status="settled"),
                PaymentReviewMessage(bot_id=1, chat_id=1, message_id=1, payment_id=payment_id, text="t", is_photo=False),
                PaymentReceiptFingerprint(payment_id=payment_id),
                DeliveryFailure(order_id=order_id, payment_id=payment_id, error="x", attempts=1, last_attempt_at=OLD),
                ChargeCodeUse(charge_code_id=code.id, user_id=500, order_id=order_id, status="consumed"),
                TrialClaim(user_id=500, shop_key=str(SHOP_A), order_id=order_id),
            ])
            await session.commit()
        done = await self._run(SHOP_A, {"order:delivered"})
        self.assertEqual((done.orders, done.payments), (1, 1))
        for model in (Order, Payment, PaymentSettlement, PaymentReviewMessage, PaymentReceiptFingerprint, DeliveryFailure):
            with self.subTest(model=model.__name__):
                self.assertEqual(await self._count(model), 0)
        self.assertEqual(await self._count(UserService), 1)
        self.assertEqual(await self._count(ChargeCodeUse, ChargeCodeUse.order_id.is_(None)), 1)
        self.assertEqual(await self._count(TrialClaim, TrialClaim.order_id.is_(None)), 1)

    async def test_only_old_rows_of_the_chosen_tags_in_this_shop(self):
        from app.db.models import Order

        await self._order("cancelled")
        await self._order("cancelled", created=datetime.now(timezone.utc))
        await self._order("rejected")
        await self._order("cancelled", shop=SHOP_B)
        await self._order("cancelled", shop=None)
        done = await self._run(SHOP_A, {"order:cancelled"})
        self.assertEqual(done.orders, 1)
        self.assertEqual(await self._count(Order), 4)
        await self._run(None, {"order:cancelled"})
        self.assertEqual(await self._count(Order, Order.reseller_id.is_(None)), 0)
        self.assertEqual(await self._count(Order, Order.reseller_id == SHOP_B), 1)

    async def test_orders_in_flight_are_never_deleted(self):
        from app.db.models import Order

        await self._order("paid")
        await self._order("delivering")
        async with self.Session() as session:
            done = await run_purge(
                session, shop_id=SHOP_A, tags=frozenset({"order:paid", "order:delivering"}), days=1
            )
        self.assertEqual(done.total, 0)
        self.assertEqual(await self._count(Order), 2)

    async def test_money_that_may_still_move_keeps_the_row(self):
        from app.db.models import Order, PaymentSettlement, PaymentVoid

        guarded = []
        for index, extra in enumerate(("awaiting", "review", "void")):
            order_id, payment_id = await self._order("cancelled", payment_status="rejected")
            guarded.append(order_id)
            async with self.Session() as session:
                if extra == "void":
                    session.add(PaymentVoid(payment_id=payment_id, actor="a", reason="r", status="processing"))
                else:
                    session.add(PaymentSettlement(
                        payment_id=payment_id, tenant_key="t", channel="psp", provider="x", amount=1000,
                        idempotency_key=f"g{index}", status="awaiting" if extra == "awaiting" else "failed",
                        error_message=None if extra == "awaiting" else "review:payment_not_pending",
                    ))
                await session.commit()
        free_order, _ = await self._order("cancelled", payment_status="rejected")
        done = await self._run(SHOP_A, {"order:cancelled", "payment:rejected"})
        self.assertEqual(done.orders, 1)
        self.assertEqual(await self._count(Order, Order.id.in_(guarded)), 3)
        self.assertEqual(await self._count(Order, Order.id == free_order), 0)

    async def test_an_unpaid_order_is_cancelled_and_released_before_it_goes(self):
        from app.db.models import Order

        order_id, _ = await self._order("awaiting_receipt", payment_status="pending")
        with mock.patch("app.services.order_purge.release_unpaid_order_holds") as release:
            done = await self._run(SHOP_A, {"order:awaiting_receipt"})
        self.assertEqual(done.orders, 1)
        release.assert_awaited_once()
        self.assertEqual(await self._count(Order), 0)

    async def test_payment_tags_never_touch_open_orders(self):
        from app.db.models import Order, Payment

        open_order, open_payment = await self._order("awaiting_receipt", payment_status="rejected")
        closed_order, closed_payment = await self._order("delivered", payment_status="rejected")
        async with self.Session() as session:
            session.add(Payment(user_id=500, amount=5, status="pending", method="card", is_wallet_topup=True,
                                wallet_shop_id=SHOP_A, created_at=OLD))
            session.add(Payment(user_id=500, amount=5, status="pending", method="card", is_wallet_topup=True,
                                wallet_shop_id=SHOP_B, created_at=OLD))
            await session.commit()
        done = await self._run(SHOP_A, {"payment:rejected", "payment:pending"})
        self.assertEqual(done.payments, 2)
        self.assertEqual(await self._count(Payment, Payment.id == open_payment), 1)
        self.assertEqual(await self._count(Payment, Payment.id == closed_payment), 0)
        self.assertEqual(await self._count(Order, Order.id.in_([open_order, closed_order])), 2)
        self.assertEqual(await self._count(Payment, Payment.wallet_shop_id == SHOP_B), 1)

    async def test_concurrent_runs_delete_each_row_once(self):
        from app.db.models import Order

        for _ in range(6):
            await self._order("cancelled", payment_status="rejected")
        results = await asyncio.gather(*(self._run(SHOP_A, {"order:cancelled"}) for _ in range(3)))
        self.assertEqual(sum(r.orders for r in results), 6)
        self.assertEqual(await self._count(Order), 0)

    async def test_preview_matches_the_run(self):
        await self._order("cancelled")
        await self._order("rejected", payment_status="rejected")
        async with self.Session() as session:
            preview = await count_purgeable(session, shop_id=SHOP_A, tags=frozenset({"order:cancelled", "order:rejected"}), days=30)
        done = await self._run(SHOP_A, {"order:cancelled", "order:rejected"})
        self.assertEqual((preview.orders, preview.payments), (done.orders, done.payments))
        self.assertEqual((done.orders, done.payments), (2, 2))

    async def test_automatic_pass_runs_only_where_it_is_switched_on(self):
        from app.db.models import Order

        await self._order("cancelled", shop=SHOP_A)
        await self._order("cancelled", shop=SHOP_B)
        async with self.Session() as session:
            await save_rules(session, shop_id=SHOP_A, rules=PurgeRules(tags=frozenset({"order:cancelled"}), auto=True, days=30))
            await save_rules(session, shop_id=SHOP_B, rules=PurgeRules(tags=frozenset({"order:cancelled"}), auto=False, days=30))
            self.assertEqual(await auto_purge_all_shops(session), 2)  # the order and its payment
        self.assertEqual(await self._count(Order, Order.reseller_id == SHOP_A), 0)
        self.assertEqual(await self._count(Order, Order.reseller_id == SHOP_B), 1)


class PurgeRouteTests(PurgeDbHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        from app.api.order_purge_pages import register_order_purge_pages

        self.staff = _reseller(SHOP_A)

        async def get_db():
            async with self.Session() as session:
                yield session

        app = FastAPI()
        register_order_purge_pages(app, require_staff=lambda: self.staff, get_db=get_db)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        await super().asyncTearDown()

    async def _settings(self, shop_id) -> dict:
        from app.services.users import get_all_settings

        async with self.Session() as session:
            return await get_all_settings(session, reseller_id=shop_id)

    async def test_save_rules_ignores_locked_tags_and_validates_days(self):
        response = await self.client.post("/finance/cleanup/purge-rules", data={
            "days": "10", "t_order:cancelled": "1", "t_order:paid": "1", "t_payment:rejected": "1", "auto": "1",
        })
        self.assertIn("ok=", response.headers["location"])
        values = await self._settings(SHOP_A)
        self.assertEqual(
            (values[KEY_TAGS], values[KEY_AUTO], values[KEY_DAYS]), ("order:cancelled,payment:rejected", "1", "10")
        )
        bad = await self.client.post("/finance/cleanup/purge-rules", data={"days": "0"})
        self.assertIn("err=", bad.headers["location"])
        platform = await self._settings(None)
        self.assertNotEqual(platform.get(KEY_TAGS), "order:cancelled,payment:rejected")

    async def test_run_now(self):
        from app.db.models import Order

        none_on = await self.client.post("/finance/cleanup/purge-run")
        self.assertIn("err=", none_on.headers["location"])
        await self._order("cancelled")
        await self._order("cancelled", shop=SHOP_B)
        await self.client.post("/finance/cleanup/purge-rules", data={"days": "30", "t_order:cancelled": "1"})
        done = await self.client.post("/finance/cleanup/purge-run?next=/evil.example")
        self.assertTrue(done.headers["location"].startswith("/finance?"))
        self.assertEqual(await self._count(Order, Order.reseller_id == SHOP_A), 0)
        self.assertEqual(await self._count(Order, Order.reseller_id == SHOP_B), 1)

    async def test_staff_without_the_key_go_home(self):
        from app.db.models import Order

        await self._order("cancelled")
        self.staff = _reseller(SHOP_A, ["orders", "shop_settings"])
        for path in ("/finance/cleanup/purge-rules", "/finance/cleanup/purge-run"):
            response = await self.client.post(path, data={"days": "30", "t_order:cancelled": "1"})
            self.assertEqual(response.headers["location"], "/home")
        self.assertEqual(await self._count(Order), 1)


class PurgePanelTemplateTests(PurgeDbHarness):
    async def test_one_switch_per_deletable_tag_shared_by_manual_and_automatic(self):
        from app.api.app import templates

        async with self.Session() as session:
            ctx = await purge_panel_context(session, OWNER, {KEY_TAGS: "order:cancelled", KEY_AUTO: "1"}, return_to="/finance?tab=orders")
            self.assertIsNone(await purge_panel_context(session, _reseller(SHOP_A, ["orders"]), {}, return_to="/x"))
        html = templates.env.get_template("_settings_order_purge.html").render(purge=ctx)
        self.assertEqual(html.count('name="t_'), 10)
        self.assertNotIn("t_order:paid", html)
        self.assertNotIn("t_order:delivering", html)
        self.assertIn('name="t_order:cancelled" value="1" aria-label="حذف سفارش‌ها لغوشده"\n                 checked', html)
        self.assertIn('name="auto" value="1" checked', html)

if __name__ == "__main__":
    unittest.main()
