"""Voiding an approved payment: scope, idempotency, unknown panel outcomes and permissions."""
import asyncio
import importlib.util
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.payment_void_pages import register_payment_void_pages
from app.db.models import (
    Base,
    BotUser,
    Order,
    Payment,
    PaymentVoid,
    Plan,
    ServiceAutomation,
    ShopWallet,
    UserService,
    WalletTransaction,
)
from app.services.payment_voids import void_approved_payment

ROOT = Path(__file__).resolve().parents[1]
OWNER = {"role": "admin", "org_principal_id": 1, "org_depth": 0, "org_status": "active", "permissions": ["payment_void"]}


class FakePanel:
    def __init__(self):
        self.status = {500: "active", 501: "active", 502: "active"}
        self.writes = 0
        self.lose_response = False

    async def get_user_by_id(self, user_id):
        return {"id": user_id, "status": self.status[user_id]}

    async def set_disabled_by_id(self, user_id, disabled):
        self.writes += 1
        self.status[user_id] = "disabled"
        if self.lose_response:
            raise TimeoutError("lost response")
        return {"id": user_id, "status": "disabled"}


class VoidCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.tmp.name) / 'void.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.session = self.Session()
        self.panel = FakePanel()
        for target in ("get_pg", "get_pg_for_reseller"):
            patcher = patch(f"app.services.payment_voids.{target}", AsyncMock(return_value=self.panel) if target.endswith("reseller") else (lambda: self.panel))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.session.add_all([
            BotUser(id=2, telegram_id=1002, referral_code="shop2", role="reseller"),
            BotUser(id=3, telegram_id=1003, referral_code="shop3", role="reseller"),
        ])
        await self.session.flush()
        self.session.add_all([
            BotUser(id=1, telegram_id=1001, referral_code="platform-customer", wallet_balance=0),
            BotUser(id=4, telegram_id=1004, referral_code="shop-customer", reseller_id=2),
            Plan(id=1, name="Paid", price=100, duration_days=30, data_limit_gb=10),
        ])
        await self.session.flush()
        self.session.add_all([
            Order(id=10, user_id=1, plan_id=1, amount=100, status="delivered", reseller_id=None),
            Order(id=11, user_id=4, plan_id=1, amount=200, status="delivered", reseller_id=2),
            Order(id=12, user_id=4, plan_id=1, amount=300, status="delivered", reseller_id=2),
            Order(id=13, user_id=1, plan_id=1, amount=100, status="delivered", reseller_id=None, note="renew:20"),
        ])
        await self.session.flush()
        self.session.add_all([
            UserService(id=20, bot_user_id=1, plan_id=1, pg_user_id=500, pg_username="plat", remark="order:10", shop_id=None),
            UserService(id=21, bot_user_id=4, plan_id=1, pg_user_id=501, pg_username="shop-a", remark="order:11", shop_id=2),
            UserService(id=22, bot_user_id=4, plan_id=1, pg_user_id=502, pg_username="shop-b", remark="order:12", shop_id=2),
        ])
        await self.session.flush()
        await self.session.execute(text("UPDATE orders SET service_id = 20 WHERE id = 10"))
        await self.session.execute(text("UPDATE orders SET service_id = 21 WHERE id = 11"))
        self.session.add_all([
            Payment(id=100, order_id=10, user_id=1, amount=100, method="card", status="approved"),
            Payment(id=101, order_id=11, user_id=4, amount=200, method="card", status="approved"),
            Payment(id=102, order_id=12, user_id=4, amount=300, method="card", status="approved"),
            Payment(id=103, order_id=13, user_id=1, amount=100, method="card", status="approved"),
            Payment(id=104, order_id=10, user_id=1, amount=100, method="gateway", status="approved"),
            Payment(id=105, order_id=11, user_id=4, amount=200, method="card", status="pending"),
        ])
        await self.session.commit()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()
        self.tmp.cleanup()

    async def void(self, payment_id=101, *, shop_id=2, refund=False, reason="رسید جعلی"):
        return await void_approved_payment(
            self.session, payment_id, shop_id=shop_id, actor="staff-1", reason=reason, refund_to_wallet=refund
        )

    async def shop_balance(self, user_id=4, shop_id=2):
        return int(await self.session.scalar(select(ShopWallet.balance).where(ShopWallet.user_id == user_id, ShopWallet.reseller_id == shop_id)) or 0)


class PaymentVoidTests(VoidCase):
    async def test_void_disables_service_cancels_order_and_keeps_money_by_default(self):
        void = await self.void()
        self.assertEqual(void.status, "done")
        self.assertEqual((await self.session.get(Payment, 101)).status, "voided")
        self.assertEqual((await self.session.get(Order, 11)).status, "cancelled")
        self.assertTrue((await self.session.get(UserService, 21)).is_cancelled)
        self.assertEqual(self.panel.status[501], "disabled")
        self.assertEqual(await self.shop_balance(), 0)

    async def test_repeated_void_does_not_refund_or_disable_twice(self):
        await self.void(refund=True)
        await self.void(refund=True)
        self.assertEqual(await self.shop_balance(), 200)
        self.assertEqual(self.panel.writes, 1)
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(PaymentVoid)), 1)

    async def test_refund_goes_to_the_shop_wallet_not_the_platform_wallet(self):
        await self.void(refund=True)
        self.assertEqual(await self.shop_balance(), 200)
        self.assertEqual((await self.session.get(BotUser, 4)).wallet_balance, 0)

    async def test_unknown_panel_outcome_keeps_target_and_pays_nothing_until_retry(self):
        self.panel.lose_response = True
        with self.assertRaises(ValueError):
            await self.void(refund=True)
        void = await self.session.scalar(select(PaymentVoid).where(PaymentVoid.payment_id == 101))
        self.assertEqual(void.status, "review")
        self.assertEqual((await self.session.get(Payment, 101)).status, "voided")
        self.assertEqual(await self.shop_balance(), 0)
        self.assertFalse((await self.session.get(UserService, 21)).is_cancelled)
        self.panel.lose_response = False
        retried = await self.void(refund=True)
        self.assertEqual(retried.status, "done")
        self.assertEqual(await self.shop_balance(), 200)
        order = await self.session.get(Order, 11)
        await self.session.refresh(order)
        self.assertEqual(order.status, "cancelled")

    async def test_retry_cannot_change_the_saved_refund_choice(self):
        self.panel.lose_response = True
        with self.assertRaises(ValueError):
            await self.void(refund=False)
        self.panel.lose_response = False
        with self.assertRaises(ValueError):
            await self.void(refund=True)
        self.assertEqual(await self.shop_balance(), 0)

    async def test_live_lease_blocks_a_second_run(self):
        self.session.add(PaymentVoid(
            payment_id=101, shop_id=2, actor="other", reason="x", status="processing", lock_token="t",
            locked_until=datetime.now(timezone.utc) + timedelta(minutes=3), service_ids="[21]",
        ))
        await self.session.execute(text("UPDATE payments SET status = 'voided' WHERE id = 101"))
        await self.session.commit()
        with self.assertRaises(ValueError):
            await self.void()
        self.assertEqual(self.panel.writes, 0)

    async def test_concurrent_calls_disable_the_service_once(self):
        async def run():
            async with self.Session() as session:
                try:
                    await void_approved_payment(session, 101, shop_id=2, actor="a", reason="dup", refund_to_wallet=True)
                except ValueError:
                    return "refused"
                return "ok"
        results = await asyncio.gather(run(), run())
        self.assertIn("ok", results)
        self.assertEqual(await self.shop_balance(), 200)
        self.assertEqual(self.panel.writes, 1)

    async def test_other_shop_and_platform_cannot_void_the_payment(self):
        for shop_id in (3, None):
            with self.assertRaises(ValueError) as caught:
                await self.void(101, shop_id=shop_id)
            self.assertEqual(str(caught.exception), "پرداخت یافت نشد")
        with self.assertRaises(ValueError):
            await self.void(100, shop_id=2)
        self.assertEqual((await self.session.get(Payment, 101)).status, "approved")
        self.assertEqual((await self.session.get(Payment, 100)).status, "approved")
        self.assertEqual(self.panel.writes, 0)

    async def test_only_approved_manual_payments_of_new_orders_can_be_voided(self):
        for payment_id in (105, 104, 103):
            with self.assertRaises(ValueError):
                await self.void(payment_id, shop_id=2 if payment_id == 105 else None)
        self.assertEqual(self.panel.writes, 0)

    async def test_reason_is_required(self):
        with self.assertRaises(ValueError):
            await self.void(reason="   ")
        self.assertEqual((await self.session.get(Payment, 101)).status, "approved")

    async def test_wholesale_order_disables_every_service(self):
        await self.session.execute(text("UPDATE user_services SET remark = 'order:11' WHERE id = 22"))
        await self.session.commit()
        await self.void()
        self.assertEqual({self.panel.status[501], self.panel.status[502]}, {"disabled"})
        self.assertTrue((await self.session.get(UserService, 22)).is_cancelled)

    async def test_void_turns_off_automatic_renewal(self):
        self.session.add(ServiceAutomation(service_id=21, renew_enabled=True))
        await self.session.commit()
        await self.void()
        automation = await self.session.scalar(select(ServiceAutomation).where(ServiceAutomation.service_id == 21))
        self.assertFalse(automation.renew_enabled)


class TopupVoidTests(VoidCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.session.add_all([
            Payment(id=200, user_id=1, amount=500, method="card", status="approved", is_wallet_topup=True),
            Payment(id=201, user_id=1, amount=700, method="card", status="approved", is_wallet_topup=True),
        ])
        self.session.add(WalletTransaction(user_id=1, amount=500, balance_after=500, reason="شارژ کیف پول #200", reseller_id=None))
        await self.session.execute(text("UPDATE bot_users SET wallet_balance = 800 WHERE id = 1"))
        await self.session.commit()

    async def test_topup_void_debits_the_credited_amount(self):
        void = await self.void(200, shop_id=None)
        self.assertEqual(void.status, "done")
        self.assertEqual((await self.session.get(BotUser, 1)).wallet_balance, 300)
        await self.void(200, shop_id=None)
        self.assertEqual((await self.session.get(BotUser, 1)).wallet_balance, 300)

    async def test_topup_void_is_refused_when_the_customer_spent_it(self):
        await self.session.execute(text("UPDATE bot_users SET wallet_balance = 100 WHERE id = 1"))
        await self.session.commit()
        with self.assertRaises(ValueError):
            await self.void(200, shop_id=None)
        self.assertEqual((await self.session.get(Payment, 200)).status, "approved")
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(PaymentVoid)), 0)

    async def test_topup_that_was_never_credited_cannot_be_voided(self):
        with self.assertRaises(ValueError):
            await self.void(201, shop_id=None)
        self.assertEqual((await self.session.get(BotUser, 1)).wallet_balance, 800)

    async def test_topup_has_no_refund_option(self):
        with self.assertRaises(ValueError):
            await self.void(200, shop_id=None, refund=True)

    async def test_shop_cannot_void_platform_topup(self):
        with self.assertRaises(ValueError):
            await self.void(200, shop_id=2)


class PaymentVoidRouteTests(VoidCase):
    def panel_app(self, staff):
        app = FastAPI()

        async def db():
            yield self.session

        async def auth():
            return staff

        register_payment_void_pages(app, require_staff=auth, get_db=db)
        return app

    async def post(self, staff, payment_id, **form):
        transport = httpx.ASGITransport(app=self.panel_app(staff))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(f"/payments/{payment_id}/void", data=form, follow_redirects=False)

    async def test_missing_permission_or_scope_is_forbidden(self):
        for staff in [
            {"role": "reseller", "bot_user_id": 2, "permissions": ["payments", "orders"]},
            {"role": "admin", "permissions": ["payment_void"]},
            {"role": "pg_staff", "permissions": ["payment_void"]},
        ]:
            self.assertEqual((await self.post(staff, 101, reason="x")).status_code, 403)
        self.assertEqual((await self.session.get(Payment, 101)).status, "approved")

    async def test_reseller_with_permission_voids_only_own_shop(self):
        staff = {"role": "reseller", "bot_user_id": 2, "permissions": ["payment_void"]}
        with patch("app.api.payment_void_pages.notify_payer", AsyncMock(return_value=True)) as notify:
            denied = await self.post(staff, 100, reason="x")
            self.assertIn("err=", denied.headers["location"])
            self.assertEqual((await self.session.get(Payment, 100)).status, "approved")
            ok = await self.post(staff, 101, reason="رسید جعلی", refund_to_wallet="1")
            self.assertIn("ok=", ok.headers["location"])
            await self.post(staff, 101, reason="رسید جعلی", refund_to_wallet="1")
        self.assertEqual(await self.shop_balance(), 200)
        notify.assert_awaited_once()
        self.assertEqual(notify.await_args.kwargs["shop_id"], 2)

    async def test_owner_voids_platform_payment(self):
        with patch("app.api.payment_void_pages.notify_payer", AsyncMock(return_value=True)):
            response = await self.post(OWNER, 100, reason="اشتباه")
        self.assertIn("ok=", response.headers["location"])
        self.assertEqual((await self.session.get(Payment, 100)).status, "voided")

    async def test_owner_cannot_void_a_shop_payment(self):
        response = await self.post(OWNER, 101, reason="x")
        self.assertIn("err=", response.headers["location"])
        self.assertEqual((await self.session.get(Payment, 101)).status, "approved")


class PaymentVoidTemplateTests(unittest.TestCase):
    def render(self, **overrides):
        from types import SimpleNamespace as NS

        from app.api.app import templates

        def payment(pid, method, status):
            return NS(id=pid, user_id=1, amount=1000, method=method, status=status,
                      is_wallet_topup=False, order_id=pid, receipt_file_id=None)

        request = NS(url=NS(path="/finance"), query_params={}, state=NS(), cookies={}, headers={})
        context = dict(
            request=request, staff={"role": "admin", "username": "owner"}, finance_tab="payments",
            can_orders=True, can_payments=True, can_void_payment=True, payers={}, csrf_token="TOK",
            q="", pager=None, receipt_matches={}, flash_ok=None, flash_err=None, can_finance_settings=False,
            payments=[payment(7, "card", "approved"), payment(8, "card", "voided"), payment(9, "gateway", "approved")],
            payment_voids={8: NS(status="review", reason="<b>x</b>", refund_amount=0)},
        )
        context.update(overrides)
        return templates.env.get_template("finance.html").render(**context)

    def test_void_button_only_for_approved_manual_payments_with_permission(self):
        html = self.render()
        self.assertIn('data-payment-void-open="7"', html)
        self.assertNotIn('data-payment-void-open="8"', html)
        self.assertNotIn('data-payment-void-open="9"', html)
        denied = self.render(can_void_payment=False)
        self.assertNotIn("data-payment-void-open", denied)
        self.assertNotIn("modal-payment-void", denied)

    def test_review_void_offers_retry_and_escapes_the_saved_reason(self):
        html = self.render()
        self.assertIn('action="/payments/8/void"', html)
        self.assertIn('value="&lt;b&gt;x&lt;/b&gt;"', html)
        self.assertNotIn('value="<b>x</b>"', html)


class PaymentVoidMigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade_keep_payments(self):
        import sqlalchemy as sa
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        spec = importlib.util.spec_from_file_location("void_migration", ROOT / "alembic/versions/0072_payment_voids.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE bot_users (id INTEGER PRIMARY KEY)"))
            conn.execute(text("CREATE TABLE payments (id INTEGER PRIMARY KEY, status TEXT)"))
            conn.execute(text("INSERT INTO payments VALUES (1, 'voided')"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                self.assertIn("payment_voids", sa.inspect(conn).get_table_names())
                migration.downgrade()
                self.assertNotIn("payment_voids", sa.inspect(conn).get_table_names())
                self.assertEqual(conn.execute(text("SELECT status FROM payments")).scalar(), "voided")
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
