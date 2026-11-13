"""Voiding a renewal or add-on: the service gets back the quota and plan it had just before it."""

from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import sqlalchemy as sa
from sqlalchemy import func, select

from app.db.models import Order, Payment, PaymentVoid, Plan, UserService
from app.services.orders import pay_with_wallet
from app.services.payment_voids import (
    offers_void,
    void_and_notify,
    void_approved_payment,
    void_customer_notice,
    void_effect,
)
from app.services.service_addons import _apply_pack_to_service, format_addon_note
from app.services.service_restore import encode_change, quota_from_panel, saved_change
from tests.test_renewal_preview import DAY, GB, RenewalFixture

ROOT = Path(__file__).resolve().parents[1]
PANEL_USER = 10


class SavedChangeTests(unittest.TestCase):
    def test_the_record_keeps_what_the_write_replaced_and_what_it_wrote(self) -> None:
        before = {"id": 1, "status": "limited", "data_limit": 10 * GB, "used_traffic": 10 * GB, "expire": 1_900_000_000}
        raw = encode_change(service_id=4, plan_id=2, before=before, payload={"status": "active", "data_limit": 20 * GB})
        change = saved_change(raw)
        self.assertEqual((change.service_id, change.plan_id), (4, 2))
        self.assertEqual(change.before.payload(), {"status": "active", "data_limit": 10 * GB, "expire": 1_900_000_000})
        self.assertEqual((change.after.data_limit, change.after.expire), (20 * GB, 1_900_000_000))

    def test_an_on_hold_service_keeps_its_hold_time(self) -> None:
        before = {"status": "on_hold", "data_limit": GB, "expire": 0, "on_hold_expire_duration": 7 * DAY}
        quota = quota_from_panel(before)
        self.assertEqual(quota.payload(), {"status": "on_hold", "data_limit": GB, "expire": 0, "on_hold_expire_duration": 7 * DAY})

    def test_a_damaged_record_reads_as_none(self) -> None:
        good = json.loads(encode_change(service_id=4, plan_id=None, before={"status": "active"}, payload={}))
        for damaged in (
            "not json",
            json.dumps({**good, "version": 2}),
            json.dumps({**good, "before": {**good["before"], "status": "limited"}}),
            json.dumps({**good, "after": {**good["after"], "data_limit": -1}}),
            json.dumps({**good, "after": {**good["after"], "expire": True}}),
            json.dumps({**good, "service_id": "x"}),
        ):
            with self.subTest(damaged=damaged):
                self.assertIsNone(saved_change(damaged))
        self.assertIsNone(saved_change(None))

    def test_an_unknown_panel_status_is_not_recorded(self) -> None:
        with self.assertRaises(ValueError):
            encode_change(service_id=4, plan_id=None, before={"status": "deleted"}, payload={})


class ExtensionVoidCase(RenewalFixture):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.pg.info["id"] = PANEL_USER
        self.session.add(Plan(id=2, name="Old", price=100, data_limit_gb=5, duration_days=10))
        self.service.plan_id = 2
        await self.session.commit()
        for target in ("app.services.payment_voids.get_pg", "app.services.pasarguard.get_pg"):
            patcher = patch(target, return_value=self.pg)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def paid_by_card(self, order: Order) -> Payment:
        payment = await self.session.scalar(select(Payment).where(Payment.order_id == order.id))
        payment.method = "card"
        await self.session.commit()
        return payment

    async def renewed(self) -> tuple[Payment, Order, dict]:
        before = dict(self.pg.info)
        order = await self.create()
        await pay_with_wallet(self.session, order, self.user)
        return await self.paid_by_card(order), order, before

    async def void(self, payment: Payment, *, shop_id: int | None = None, refund: bool = False) -> PaymentVoid:
        return await void_approved_payment(
            self.session, payment.id, shop_id=shop_id, actor="staff", reason="رسید جعلی", refund_to_wallet=refund
        )

    async def void_count(self) -> int:
        return int(await self.session.scalar(select(func.count(PaymentVoid.id))))


class RenewalVoidTests(ExtensionVoidCase):
    async def test_a_renewal_saves_the_quota_it_replaced_before_writing(self) -> None:
        _payment, order, before = await self.renewed()
        change = saved_change(order.service_restore)
        self.assertEqual((change.service_id, change.plan_id), (self.service.id, 2))
        self.assertEqual((change.before.data_limit, change.before.expire), (before["data_limit"], before["expire"]))
        self.assertEqual(change.after.data_limit, self.pg.info["data_limit"])

    async def test_a_void_puts_the_service_back_and_keeps_later_usage(self) -> None:
        payment, order, before = await self.renewed()
        self.pg.info["used_traffic"] += GB
        void = await self.void(payment)
        self.assertEqual(void.status, "done")
        self.assertEqual(
            (self.pg.info["status"], self.pg.info["data_limit"], self.pg.info["expire"]),
            ("active", before["data_limit"], before["expire"]),
        )
        self.assertEqual(self.pg.info["used_traffic"], before["used_traffic"] + GB)
        service = await self.session.get(UserService, self.service.id, populate_existing=True)
        self.assertEqual(service.plan_id, 2)
        self.assertFalse(service.is_cancelled, "the service the customer had before stays theirs")
        self.assertEqual(service.quota_data_limit_bytes, before["data_limit"])
        order = await self.session.get(Order, order.id, populate_existing=True)
        self.assertEqual((order.status, order.service_mutation_pending), ("cancelled", False))
        self.assertEqual((await self.session.get(Payment, payment.id, populate_existing=True)).status, "voided")

    async def test_voiding_twice_writes_the_panel_once(self) -> None:
        payment, _order, _before = await self.renewed()
        await self.void(payment)
        await self.void(payment)
        self.assertEqual(len(self.pg.writes), 2, "one renewal and one put-back")
        self.assertEqual(await self.void_count(), 1)

    async def test_a_service_changed_after_the_renewal_is_left_for_a_person(self) -> None:
        payment, order, _before = await self.renewed()
        self.pg.info["data_limit"] += 5 * GB
        with self.assertRaisesRegex(ValueError, "تغییر کرده"):
            await self.void(payment)
        self.assertEqual(len(self.pg.writes), 1)
        self.assertEqual(await self.void_count(), 0)
        self.assertEqual((await self.session.get(Payment, payment.id, populate_existing=True)).status, "approved")
        order = await self.session.get(Order, order.id, populate_existing=True)
        self.assertEqual((order.status, order.service_mutation_pending), ("delivered", False))

    async def test_a_lost_panel_response_is_reviewed_and_the_retry_does_not_write_again(self) -> None:
        payment, order, before = await self.renewed()
        self.pg.fail_after_write = True
        with self.assertRaises(ValueError):
            await self.void(payment, refund=True)
        void = await self.session.scalar(
            select(PaymentVoid).where(PaymentVoid.payment_id == payment.id).execution_options(populate_existing=True)
        )
        self.assertEqual(void.status, "review")
        order = await self.session.get(Order, order.id, populate_existing=True)
        self.assertTrue(order.service_mutation_pending, "no other renewal or add-on while the void is open")
        balance = (await self.session.get(type(self.user), self.user.id, populate_existing=True)).wallet_balance
        self.pg.fail_after_write = False
        await self.void(payment, refund=True)
        self.assertEqual(len(self.pg.writes), 2)
        self.assertEqual(self.pg.info["data_limit"], before["data_limit"])
        refunded = (await self.session.get(type(self.user), self.user.id, populate_existing=True)).wallet_balance
        self.assertEqual(refunded - balance, payment.amount, "the refund is paid once, after the put-back")

    async def test_another_shop_cannot_void_it(self) -> None:
        payment, _order, _before = await self.renewed()
        with self.assertRaisesRegex(ValueError, "یافت نشد"):
            await self.void(payment, shop_id=self.other.id)
        self.assertEqual(len(self.pg.writes), 1)

    async def test_it_is_offered_only_with_a_saved_quota(self) -> None:
        payment, order, _before = await self.renewed()
        self.assertTrue(offers_void(payment, order))
        order.service_restore = None
        await self.session.commit()
        self.assertFalse(offers_void(payment, order))
        with self.assertRaisesRegex(ValueError, "دستی"):
            await self.void(payment)
        self.assertEqual(await self.void_count(), 0)

    async def test_staff_and_the_customer_are_told_what_it_does(self) -> None:
        payment, order, _before = await self.renewed()
        self.assertIn("پیش از این تمدید", void_effect(order))
        self.assertIn("تمدید یا افزونه", void_customer_notice(payment, 0, order))
        self.assertIn("غیرفعال", void_effect(Order(note=None)))
        with patch("app.services.payment_voids.notify_payer") as notify:
            await void_and_notify(self.session, payment.id, shop_id=None, actor="staff", reason="رسید جعلی")
        self.assertIn("برداشته شد", notify.await_args.kwargs["body"])


class AddonVoidTests(ExtensionVoidCase):
    async def added(self, kind: str, amount: float) -> tuple[Payment, Order]:
        order = Order(
            user_id=self.user.id, amount=50, status="delivering", service_id=self.service.id,
            note=format_addon_note(1, self.service.id, kind=kind, amount=amount),
        )
        self.session.add(order)
        await self.session.commit()
        await _apply_pack_to_service(
            self.session, self.service, kind=kind, amount=amount, order_reseller_id=None, order_id=order.id
        )
        await self.session.refresh(order)
        order.status = "delivered"
        self.session.add(Payment(order_id=order.id, user_id=self.user.id, amount=50, method="card", status="approved"))
        await self.session.commit()
        return await self.session.scalar(select(Payment).where(Payment.order_id == order.id)), order

    async def test_a_volume_add_on_is_taken_back(self) -> None:
        before = dict(self.pg.info)
        payment, _order = await self.added("volume", 5)
        self.assertEqual(self.pg.info["data_limit"], before["data_limit"] + 5 * GB)
        await self.void(payment)
        self.assertEqual(self.pg.info["data_limit"], before["data_limit"])
        self.assertEqual(self.pg.info["expire"], before["expire"])

    async def test_a_time_add_on_is_taken_back(self) -> None:
        before = dict(self.pg.info)
        payment, _order = await self.added("duration", 10)
        self.assertEqual(self.pg.info["expire"], before["expire"] + 10 * DAY)
        await self.void(payment)
        self.assertEqual((self.pg.info["expire"], self.pg.info["data_limit"]), (before["expire"], before["data_limit"]))

    async def test_a_later_renewal_blocks_the_add_on_void(self) -> None:
        payment, _order = await self.added("volume", 5)
        renewal = await self.create()
        await pay_with_wallet(self.session, renewal, self.user)
        with self.assertRaisesRegex(ValueError, "تغییر کرده"):
            await self.void(payment)


class ServiceRestoreMigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade_keep_existing_orders(self) -> None:
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        path = ROOT / "alembic/versions/0114_order_service_restore.py"
        spec = importlib.util.spec_from_file_location("order_service_restore_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE orders (id INTEGER PRIMARY KEY, note TEXT)"))
            conn.execute(sa.text("INSERT INTO orders (id, note) VALUES (1, 'renew:3')"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                self.assertIsNone(conn.execute(sa.text("SELECT service_restore FROM orders")).scalar())
                migration.downgrade()
                columns = {column["name"] for column in sa.inspect(conn).get_columns("orders")}
                self.assertNotIn("service_restore", columns)
                self.assertEqual(conn.execute(sa.text("SELECT note FROM orders")).scalar(), "renew:3")
                migration.downgrade()
        engine.dispose()
