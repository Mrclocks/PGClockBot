"""Checkout and wallet gift rules, including transactional and migration edges."""

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import (
    Base,
    BotUser,
    ChargeCode,
    ChargeCodeUse,
    Order,
    Plan,
    ServiceAddonPack,
)
from app.services.customer_backfill import backfill_legacy_customers, backfill_legacy_services
from app.services.gift_codes import parse_expiry, expiry_input, validate_rules
from app.services.orders import (
    apply_discount_to_order,
    cancel_order,
    _release_order_discount,
)
from app.services.ux20 import create_charge_code, redeem_charge_code


class GiftCodeRulesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{self.tmp.name}/test.db", connect_args={"timeout": 10}
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(text("PRAGMA foreign_keys=ON"))
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all(
                [
                    BotUser(id=1, telegram_id=1, referral_code="one"),
                    BotUser(id=2, telegram_id=2, referral_code="two"),
                    BotUser(id=3, telegram_id=3, referral_code="shop"),
                    Plan(id=1, name="paid", price=200_000, duration_days=30),
                    Plan(id=2, name="trial", price=0, duration_days=1, is_trial=True),
                ]
            )
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.tmp.cleanup()

    async def order(
        self,
        session,
        *,
        user_id=1,
        amount=200_000,
        note=None,
        shop=None,
        status="pending",
        plan_id=1,
    ):
        row = Order(
            user_id=user_id,
            amount=amount,
            note=note,
            reseller_id=shop,
            status=status,
            plan_id=plan_id,
        )
        session.add(row)
        await session.commit()
        return row

    async def gift(self, session, **rules):
        return await create_charge_code(
            session, kind="discount", percent=20, max_uses=None, **rules
        )

    async def assert_rejected(self, session, order, code, message):
        with self.assertRaisesRegex(ValueError, message):
            await apply_discount_to_order(session, order, code.code)
        # Caller may catch the error and commit: no use must have been burned.
        await session.commit()
        await session.refresh(code)
        self.assertEqual(code.used_count, 0)
        await session.refresh(order)
        self.assertIsNone(order.discount_code)

    async def test_percentage_cap_and_small_purchase(self):
        async with self.Session() as session:
            code = await self.gift(session, max_discount_toman=25_000)
            order = await apply_discount_to_order(
                session, await self.order(session), code.code.lower()
            )
            self.assertEqual((order.discount_amount, order.amount), (25_000, 175_000))
            small = await apply_discount_to_order(
                session, await self.order(session, amount=10_003), code.code
            )
            self.assertEqual((small.discount_amount, small.amount), (2000, 8003))

    async def test_preview_does_not_reserve_and_requires_context_for_rules(self):
        from app.services.orders import apply_discount

        async with self.Session() as session:
            code = await self.gift(
                session,
                max_discount_toman=25_000,
                max_uses_per_user=1,
                purchase_types=["renew"],
            )
            self.assertEqual(
                await apply_discount(session, code.code, 200_000), (0, None)
            )
            order = await self.order(session, note="renew:1")
            self.assertEqual(
                await apply_discount(session, code.code, order.amount, order=order),
                (25_000, code.code),
            )
            await session.refresh(code)
            self.assertEqual(code.used_count, 0)
            await apply_discount_to_order(session, order, code.code)
            next_order = await self.order(session, note="renew:1")
            self.assertEqual(
                await apply_discount(
                    session, code.code, next_order.amount, order=next_order
                ),
                (0, None),
            )
            global_code = await create_charge_code(
                session, kind="discount", percent=20, max_uses=1
            )
            await apply_discount_to_order(
                session, await self.order(session), global_code.code
            )
            self.assertEqual(
                await apply_discount(session, global_code.code, 200_000), (0, None)
            )

    async def test_expiry_and_inactive_do_not_consume(self):
        async with self.Session() as session:
            for field, value, message in [
                (
                    "expires_at",
                    datetime.now(timezone.utc) - timedelta(seconds=1),
                    "اعتبار",
                ),
                ("is_active", False, "غیرفعال"),
            ]:
                code = await self.gift(session)
                setattr(code, field, value)
                await session.commit()
                await self.assert_rejected(
                    session, await self.order(session), code, message
                )

    async def test_per_user_limit_cancel_and_consume(self):
        from app.services.loyalty import consume_loyalty_discount_for_order

        async with self.Session() as session:
            code = await self.gift(session, max_uses_per_user=1)
            order = await apply_discount_to_order(
                session, await self.order(session), code.code
            )
            another = await self.order(session)
            with self.assertRaisesRegex(ValueError, "سقف استفاده شما"):
                await apply_discount_to_order(session, another, code.code)
            await cancel_order(session, order)
            await _release_order_discount(session, order)
            await session.commit()
            await session.refresh(code)
            self.assertEqual(code.used_count, 0)
            another = await apply_discount_to_order(session, another, code.code)
            another.status = "delivered"
            await consume_loyalty_discount_for_order(session, another)
            await session.commit()
            with self.assertRaisesRegex(ValueError, "سقف استفاده شما"):
                await apply_discount_to_order(
                    session, await self.order(session), code.code
                )
            second_user = await apply_discount_to_order(
                session, await self.order(session, user_id=2), code.code
            )
            self.assertEqual(second_user.discount_amount, 40_000)

    async def test_purchase_types_and_addon_snapshot(self):
        async with self.Session() as session:
            session.add(
                ServiceAddonPack(
                    id=1, name="legacy", kind="duration", amount=5, price=10_000
                )
            )
            await session.commit()
            for kind, note in [
                ("new", "custom"),
                ("renew", "renew:1"),
                ("volume", "svc_addon:1:1:volume:10"),
                ("duration", "svc_addon:1:1"),
            ]:
                code = await self.gift(session, purchase_types=[kind])
                wrong_note = "renew:1" if kind == "new" else None
                await self.assert_rejected(
                    session, await self.order(session, note=wrong_note), code, "نوع"
                )
                correct = await apply_discount_to_order(
                    session, await self.order(session, note=note), code.code
                )
                self.assertEqual(correct.discount_amount, 40_000)
            both = await self.gift(session, purchase_types=["volume", "duration"])
            await apply_discount_to_order(
                session,
                await self.order(session, note="svc_addon:1:1:volume:5"),
                both.code,
            )
            await apply_discount_to_order(
                session,
                await self.order(session, note="svc_addon:1:1:duration:3"),
                both.code,
            )

    async def test_first_purchase_ignores_trial_cancel_and_other_shop(self):
        async with self.Session() as session:
            await self.order(session, amount=0, status="delivered", plan_id=2)
            await self.order(session, status="cancelled")
            await self.order(session, status="delivered", shop=3)
            code = await self.gift(session, first_purchase_only=True)
            order = await apply_discount_to_order(
                session, await self.order(session), code.code
            )
            other_code = await self.gift(session, first_purchase_only=True)
            await self.assert_rejected(
                session, await self.order(session), other_code, "اولین"
            )
            await cancel_order(session, order)
            await _release_order_discount(session, order)
            await session.commit()
            fresh = await apply_discount_to_order(
                session, await self.order(session), other_code.code
            )
            fresh.status = "paid"
            await session.commit()
            await self.assert_rejected(
                session, await self.order(session), code, "اولین"
            )

    async def test_first_purchase_cannot_apply_to_renewal(self):
        async with self.Session() as session:
            code = await self.gift(session, first_purchase_only=True)
            await self.assert_rejected(
                session, await self.order(session, note="renew:1"), code, "خرید جدید"
            )

    async def test_previous_addon_counts_as_purchase_and_paid_zero_coupon_counts(self):
        async with self.Session() as session:
            await self.order(
                session,
                note="svc_addon:1:1:volume:10",
                plan_id=None,
                status="delivered",
            )
            code = await self.gift(session, first_purchase_only=True)
            await self.assert_rejected(
                session, await self.order(session), code, "اولین"
            )
            paid = await self.order(session, user_id=2, amount=0, status="paid")
            paid.discount_amount = 200_000
            await session.commit()
            await self.assert_rejected(
                session, await self.order(session, user_id=2), code, "اولین"
            )

    async def test_create_order_applies_rules_and_tracks_reservation(self):
        from app.services.orders import create_order

        async with self.Session() as session:
            code = await self.gift(session, first_purchase_only=True)
            order = await create_order(
                session, user_id=1, plan_id=1, discount_code=code.code
            )
            self.assertEqual((order.amount, order.discount_code), (160_000, code.code))
            use = (
                await session.execute(
                    select(ChargeCodeUse).where(ChargeCodeUse.order_id == order.id)
                )
            ).scalar_one()
            self.assertEqual((use.user_id, use.status), (1, "reserved"))
            key = code.code
            with self.assertRaisesRegex(ValueError, "اولین"):
                await create_order(session, user_id=1, plan_id=1, discount_code=key)
            await session.commit()
            self.assertEqual(
                (await session.execute(select(func.count(Order.id)))).scalar_one(), 1
            )

    async def test_delete_buyer_with_reserved_and_consumed_codes(self):
        from app.services.users import delete_bot_user
        from app.services.loyalty import consume_loyalty_discount_for_order

        async with self.Session() as session:
            code = await self.gift(session)
            consumed = await apply_discount_to_order(
                session, await self.order(session), code.code
            )
            consumed.status = "delivered"
            await consume_loyalty_discount_for_order(session, consumed)
            await session.commit()
            await apply_discount_to_order(session, await self.order(session), code.code)
            await delete_bot_user(session, 1, delete_pg_services=False)
            self.assertIsNone(await session.get(BotUser, 1))
            await session.refresh(code)
            self.assertEqual(code.used_count, 1)
            self.assertEqual(
                (
                    await session.execute(select(func.count(ChargeCodeUse.id)))
                ).scalar_one(),
                0,
            )

    async def test_scope_and_wallet_code_cannot_discount(self):
        async with self.Session() as session:
            code = await self.gift(session, reseller_id=3)
            await self.assert_rejected(
                session, await self.order(session), code, "فروشگاه"
            )
            await apply_discount_to_order(
                session, await self.order(session, shop=3), code.code
            )
            wallet = await create_charge_code(session, amount=50_000)
            await self.assert_rejected(
                session, await self.order(session), wallet, "شارژ کیف پول"
            )
            trial_code = await self.gift(session)
            await self.assert_rejected(
                session, await self.order(session, plan_id=2), trial_code, "نوع"
            )

    async def test_wallet_user_limit_expiry_and_discount_instructions(self):
        async with self.Session() as session:
            buyer = await session.get(BotUser, 1)
            code = await create_charge_code(
                session, amount=50_000, max_uses=10, max_uses_per_user=1
            )
            _, balance = await redeem_charge_code(session, user=buyer, code=code.code)
            await session.commit()
            self.assertEqual(balance, 50_000)
            with self.assertRaisesRegex(ValueError, "سقف استفاده شما"):
                await redeem_charge_code(session, user=buyer, code=code.code)
            expired = await create_charge_code(session, amount=1000)
            expired.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await session.commit()
            with self.assertRaisesRegex(ValueError, "اعتبار"):
                await redeem_charge_code(session, user=buyer, code=expired.code)
            discount = await self.gift(session)
            with self.assertRaisesRegex(ValueError, "هنگام پرداخت"):
                await redeem_charge_code(session, user=buyer, code=discount.code)
            await session.commit()
            await session.refresh(buyer)
            self.assertEqual(buyer.wallet_balance, 50_000)

    async def test_wallet_credit_failure_rolls_back_use(self):
        async with self.Session() as session:
            code = await create_charge_code(session, amount=1000)
            user = await session.get(BotUser, 1)
            with patch(
                "app.services.wallet.credit_wallet",
                new=AsyncMock(side_effect=ValueError("failed credit")),
            ):
                with self.assertRaisesRegex(ValueError, "failed credit"):
                    await redeem_charge_code(session, user=user, code=code.code)
            await session.commit()
            await session.refresh(code)
            self.assertEqual(code.used_count, 0)
            self.assertEqual(
                (
                    await session.execute(select(func.count(ChargeCodeUse.id)))
                ).scalar_one(),
                0,
            )

    async def test_concurrent_personal_and_global_limits(self):
        async def attempt(order_id, key):
            async with self.Session() as session:
                order = await session.get(Order, order_id)
                try:
                    await apply_discount_to_order(session, order, key)
                    return True
                except ValueError:
                    await session.rollback()
                    return False

        async with self.Session() as session:
            personal = await self.gift(session, max_uses_per_user=1)
            ids = [(await self.order(session)).id for _ in range(2)]
            key = personal.code
        self.assertEqual(
            sum(await asyncio.gather(*(attempt(oid, key) for oid in ids))), 1
        )

        async with self.Session() as session:
            first_codes = [
                (await self.gift(session, first_purchase_only=True)).code
                for _ in range(2)
            ]
            first_ids = [(await self.order(session)).id for _ in range(2)]
        self.assertEqual(
            sum(
                await asyncio.gather(
                    *(attempt(oid, key) for oid, key in zip(first_ids, first_codes))
                )
            ),
            1,
        )

        async with self.Session() as session:
            codes = [(await self.gift(session)).code for _ in range(2)]
            order_id = (await self.order(session)).id
        self.assertEqual(
            sum(await asyncio.gather(*(attempt(order_id, key) for key in codes))), 1
        )
        async with self.Session() as session:
            global_code = await create_charge_code(
                session, kind="discount", percent=20, max_uses=1
            )
            ids = [(await self.order(session, user_id=uid)).id for uid in (1, 2)]
            key = global_code.code
        self.assertEqual(
            sum(await asyncio.gather(*(attempt(oid, key) for oid in ids))), 1
        )

    async def test_edit_route_validation_and_shop_authorization(self):
        import httpx
        from fastapi import FastAPI
        from app.api.ux20_pages import register_ux20_pages

        staff = {
            "role": "admin",
            "id": 1,
            "org_principal_id": 1,
            "org_depth": 0,
            "org_parent_id": None,
            "org_status": "active",
        }

        async def get_db():
            async with self.Session() as session:
                yield session

        app = FastAPI()
        register_ux20_pages(
            app,
            render=lambda *a, **k: None,
            require_staff=lambda: staff,
            require_admin=lambda: staff,
            get_db=get_db,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/plans/gift-codes",
                data={
                    "kind": "discount",
                    "percent": "20",
                    "max_discount_toman": "50000",
                    "max_uses_per_user": "2",
                    "purchase_types": ["renew", "volume"],
                    "code": "TEST20",
                },
            )
            self.assertIn("ok=", response.headers["location"])
            async with self.Session() as session:
                code = (
                    await session.execute(
                        select(ChargeCode).where(ChargeCode.code == "TEST20")
                    )
                ).scalar_one()
                code_id = code.id
                self.assertEqual(
                    (code.percent, code.max_discount_toman, code.purchase_types),
                    (20, 50_000, "renew,volume"),
                )
            response = await client.post(
                f"/plans/gift-codes/{code_id}/edit",
                data={"kind": "discount", "percent": "30", "max_uses_per_user": "1"},
            )
            self.assertIn("ok=", response.headers["location"])
            staff.clear()
            staff.update(
                {
                    "role": "reseller",
                    "bot_user_id": 3,
                    "permissions": ["plans", "orders"],
                }
            )
            await client.post(
                f"/plans/gift-codes/{code_id}/edit",
                data={"kind": "discount", "percent": "99"},
            )
            async with self.Session() as session:
                code = await session.get(ChargeCode, code_id)
                self.assertEqual(code.percent, 30)
            response = await client.post(
                "/plans/gift-codes", data={"kind": "discount", "percent": "101"}
            )
            self.assertIn("err=", response.headers["location"])


class GiftCodeValidationTests(unittest.TestCase):
    def test_tehran_expiry_roundtrip(self):
        self.assertEqual(
            parse_expiry("2030-01-01T12:00"),
            datetime(2030, 1, 1, 8, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(
            parse_expiry("2030-01-01 12:00"),
            datetime(2030, 1, 1, 8, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(
            expiry_input(parse_expiry("2030-01-01T12:00")), "2030-01-01 12:00"
        )
        self.assertIsNone(parse_expiry(""))

    def test_conflicting_and_invalid_rules(self):
        for rules in [
            dict(kind="discount", percent=0),
            dict(kind="discount", percent=101),
            dict(amount=1, max_uses_per_user=0),
            dict(amount=1, max_uses=-1),
            dict(kind="discount", percent=20, max_discount_toman=0),
            dict(kind="wallet", amount=100, first_purchase_only=True),
            dict(kind="discount", percent=20, purchase_types=["unknown"]),
            dict(
                kind="discount",
                percent=20,
                first_purchase_only=True,
                purchase_types=["renew"],
            ),
        ]:
            with self.subTest(rules=rules), self.assertRaises(ValueError):
                validate_rules(**rules)

    def test_migration_preserves_codes_and_backfills_usage(self):
        from sqlalchemy import create_engine, inspect
        from alembic import command
        from app.db.alembic_runner import alembic_config

        with tempfile.TemporaryDirectory() as temp:
            url = f"sqlite:///{temp}/old.db"
            engine = create_engine(url)
            Base.metadata.create_all(engine)
            cfg = alembic_config(url)
            # Stamp gift tip then drop only 0037 — avoid SQLite ALTER on 0036 uniques.
            command.stamp(cfg, "0037_gift_code_rules")
            command.downgrade(cfg, "0036_limits_guides_receipts")
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO bot_users (id, telegram_id, referral_code, role, wallet_balance, points_balance, is_blocked) VALUES (1, 1, 'one', 'user', 50000, 0, false)"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO charge_codes (code, amount, max_uses, used_count, is_active) VALUES ('OLD', 50000, 10, 1, true)"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO wallet_transactions (user_id, amount, balance_after, reason) VALUES (1, 50000, 50000, 'کد هدیه OLD')"
                    )
                )
            command.upgrade(cfg, "0074_customer_identity")

            async def backfill_fixture() -> None:
                async_engine = create_async_engine(url.replace("sqlite:", "sqlite+aiosqlite:"))
                try:
                    sessions = async_sessionmaker(async_engine, expire_on_commit=False)
                    async with sessions() as session:
                        await backfill_legacy_customers(session)
                        await session.commit()
                        await backfill_legacy_services(session)
                        await session.commit()
                finally:
                    await async_engine.dispose()

            asyncio.run(backfill_fixture())
            command.upgrade(cfg, "head")
            with engine.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT kind, percent, amount, used_count, max_uses_per_user FROM charge_codes"
                    )
                ).one()
                self.assertEqual(tuple(row), ("wallet", 0, 50000, 1, None))
                self.assertEqual(
                    conn.execute(
                        text("SELECT user_id, status FROM charge_code_uses")
                    ).one(),
                    (1, "consumed"),
                )
                self.assertIn(
                    "expires_at",
                    {c["name"] for c in inspect(conn).get_columns("charge_codes")},
                )
            command.upgrade(cfg, "head")
            with engine.connect() as conn:
                self.assertEqual(
                    conn.execute(
                        text("SELECT COUNT(*) FROM charge_code_uses")
                    ).scalar_one(),
                    1,
                )
            engine.dispose()
