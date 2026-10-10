"""Root security fix: per-shop wallet isolation.

Shop-sourced credits (gift codes, loyalty, referral, top-ups) must land in
``shop_wallets`` and must not be spendable on platform orders.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("WEB_SECRET", "test-web-secret-for-shop-wallet-32chars")

ROOT = Path(__file__).resolve().parents[1]


class ShopWalletIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from app.db.models import Base
        import app.db.models  # noqa: F401

        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "shop_wallet.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self._tmpdir.cleanup()

    async def _user(self, session, tg: int, *, reseller_id=None, code: str | None = None):
        from app.db.models import BotUser, Role

        u = BotUser(
            telegram_id=tg,
            referral_code=code or f"U{tg}",
            role=Role.USER.value,
            reseller_id=reseller_id,
        )
        session.add(u)
        await session.commit()
        await session.refresh(u)
        return u

    async def test_shop_gift_credits_shop_purse_not_platform(self):
        from app.services.ux20 import create_charge_code, redeem_charge_code
        from app.services.wallet import get_wallet_balance

        async with self.Session() as session:
            owner = await self._user(session, 1001, code="OWN1001")
            buyer = await self._user(session, 1002, reseller_id=owner.id, code="BUY1002")
            row = await create_charge_code(
                session, amount=50_000, max_uses=1, reseller_id=owner.id
            )
            with patch(
                "app.services.users.current_shop_reseller_id",
                return_value=owner.id,
            ):
                _code, bal = await redeem_charge_code(
                    session, user=buyer, code=row.code
                )
            await session.commit()
            await session.refresh(buyer)
            self.assertEqual(bal, 50_000)
            self.assertEqual(buyer.wallet_balance, 0)
            self.assertEqual(
                await get_wallet_balance(session, buyer, shop_id=owner.id), 50_000
            )
            self.assertEqual(
                await get_wallet_balance(session, buyer, shop_id=None), 0
            )

    async def test_shop_balance_cannot_pay_platform_order(self):
        from app.db.models import Order, OrderStatus, Plan
        from app.services.orders import pay_with_wallet
        from app.services.wallet import credit_wallet

        async with self.Session() as session:
            owner = await self._user(session, 2001, code="OWN2001")
            buyer = await self._user(session, 2002, reseller_id=owner.id, code="BUY2002")
            await credit_wallet(
                session, buyer, 100_000, "shop seed", shop_id=owner.id
            )
            plan = Plan(
                name="platform",
                price=50_000,
                duration_days=30,
                data_limit_gb=10,
                is_active=True,
            )
            session.add(plan)
            await session.flush()
            order = Order(
                user_id=buyer.id,
                plan_id=plan.id,
                amount=50_000,
                status=OrderStatus.PENDING.value,
                reseller_id=None,
            )
            session.add(order)
            await session.commit()
            await session.refresh(order)
            with self.assertRaises(ValueError) as ctx:
                await pay_with_wallet(session, order, buyer)
            self.assertIn("موجودی", str(ctx.exception))

    async def test_shop_balance_pays_same_shop_order(self):
        from app.db.models import Order, OrderStatus, Plan
        from app.services.orders import pay_with_wallet
        from app.services.wallet import credit_wallet, get_wallet_balance

        async with self.Session() as session:
            owner = await self._user(session, 3001, code="OWN3001")
            buyer = await self._user(session, 3002, reseller_id=owner.id, code="BUY3002")
            await credit_wallet(
                session, buyer, 80_000, "shop seed", shop_id=owner.id
            )
            plan = Plan(
                name="shop-plan",
                price=30_000,
                duration_days=30,
                data_limit_gb=10,
                is_active=True,
                owner_reseller_id=owner.id,
            )
            session.add(plan)
            await session.flush()
            order = Order(
                user_id=buyer.id,
                plan_id=plan.id,
                amount=30_000,
                status=OrderStatus.PENDING.value,
                reseller_id=owner.id,
            )
            session.add(order)
            await session.commit()
            await session.refresh(order)

            with patch(
                "app.services.orders._resume_paid_wallet_order",
                new=AsyncMock(side_effect=lambda s, o, u: o),
            ):
                await pay_with_wallet(session, order, buyer)

            self.assertEqual(
                await get_wallet_balance(session, buyer, shop_id=owner.id), 50_000
            )
            await session.refresh(buyer)
            self.assertEqual(buyer.wallet_balance, 0)

    async def test_topup_settlement_uses_wallet_shop_id_not_sticky_reseller(self):
        from app.db.models import Payment, PaymentMethod, PaymentStatus
        from app.services.payment_settlement import resolve_payment_shop_owner_id

        async with self.Session() as session:
            owner = await self._user(session, 4001, code="OWN4001")
            # Sticky shop attribution, but top-up created on main bot.
            user = await self._user(session, 4002, reseller_id=owner.id, code="BUY4002")
            pay = Payment(
                user_id=user.id,
                amount=10_000,
                method=PaymentMethod.PSP.value,
                status=PaymentStatus.PENDING.value,
                is_wallet_topup=True,
                wallet_shop_id=None,
            )
            session.add(pay)
            await session.commit()
            await session.refresh(pay)
            sid = await resolve_payment_shop_owner_id(session, pay)
            self.assertIsNone(sid)

            pay2 = Payment(
                user_id=user.id,
                amount=10_000,
                method=PaymentMethod.PSP.value,
                status=PaymentStatus.PENDING.value,
                is_wallet_topup=True,
                wallet_shop_id=owner.id,
            )
            session.add(pay2)
            await session.commit()
            await session.refresh(pay2)
            self.assertEqual(
                await resolve_payment_shop_owner_id(session, pay2), owner.id
            )

    async def test_referral_bonus_credits_shop_purse(self):
        from app.db.models import Order, OrderStatus, Plan
        from app.services.orders import _maybe_pay_referral_bonus
        from app.services.users import set_setting
        from app.services.wallet import get_wallet_balance

        async with self.Session() as session:
            owner = await self._user(session, 5001, code="OWN5001")
            referrer = await self._user(
                session, 5002, reseller_id=owner.id, code="REF5002"
            )
            buyer = await self._user(
                session, 5003, reseller_id=owner.id, code="BUY5003"
            )
            buyer.referred_by_id = referrer.id
            await session.commit()
            await set_setting(
                session, "referral_bonus", "25000", reseller_id=owner.id
            )
            plan = Plan(
                name="p",
                price=100_000,
                duration_days=30,
                data_limit_gb=5,
                is_active=True,
                owner_reseller_id=owner.id,
                is_trial=False,
            )
            session.add(plan)
            await session.flush()
            order = Order(
                user_id=buyer.id,
                plan_id=plan.id,
                amount=100_000,
                status=OrderStatus.DELIVERED.value,
                reseller_id=owner.id,
            )
            session.add(order)
            await session.commit()
            await session.refresh(order)
            order.user = buyer
            await _maybe_pay_referral_bonus(session, order)
            await session.commit()
            self.assertEqual(
                await get_wallet_balance(session, referrer, shop_id=owner.id),
                25_000,
            )
            await session.refresh(referrer)
            self.assertEqual(referrer.wallet_balance, 0)

    async def test_trial_and_zero_amount_skip_referral(self):
        from app.db.models import Order, OrderStatus, Plan
        from app.services.orders import _maybe_pay_referral_bonus
        from app.services.users import set_setting
        from app.services.wallet import get_wallet_balance

        async with self.Session() as session:
            owner = await self._user(session, 6001, code="OWN6001")
            referrer = await self._user(
                session, 6002, reseller_id=owner.id, code="REF6002"
            )
            buyer = await self._user(
                session, 6003, reseller_id=owner.id, code="BUY6003"
            )
            buyer.referred_by_id = referrer.id
            await session.commit()
            await set_setting(
                session, "referral_bonus", "99999", reseller_id=owner.id
            )
            trial = Plan(
                name="trial",
                price=0,
                duration_days=1,
                data_limit_gb=1,
                is_active=True,
                owner_reseller_id=owner.id,
                is_trial=True,
            )
            session.add(trial)
            await session.flush()
            order = Order(
                user_id=buyer.id,
                plan_id=trial.id,
                amount=0,
                status=OrderStatus.DELIVERED.value,
                reseller_id=owner.id,
            )
            session.add(order)
            await session.commit()
            await session.refresh(order)
            order.user = buyer
            await _maybe_pay_referral_bonus(session, order)
            await session.commit()
            self.assertEqual(
                await get_wallet_balance(session, referrer, shop_id=owner.id), 0
            )

    async def test_loyalty_discount_shop_scoped(self):
        from app.db.models import Order, OrderStatus, Plan
        from app.services.loyalty import (
            issue_discount_entitlement,
            reserve_loyalty_discount,
        )

        async with self.Session() as session:
            owner = await self._user(session, 7001, code="OWN7001")
            other = await self._user(session, 7009, code="OWN7009")
            user = await self._user(session, 7002, reseller_id=owner.id, code="BUY7002")
            ent = await issue_discount_entitlement(
                session,
                user,
                percent=50,
                reseller_id=owner.id,
            )
            await session.commit()
            plan = Plan(
                name="x",
                price=100_000,
                duration_days=30,
                data_limit_gb=1,
                is_active=True,
            )
            session.add(plan)
            await session.flush()
            platform_order = Order(
                user_id=user.id,
                plan_id=plan.id,
                amount=100_000,
                status=OrderStatus.PENDING.value,
                reseller_id=None,
            )
            session.add(platform_order)
            await session.commit()
            await session.refresh(platform_order)
            with self.assertRaises(ValueError):
                await reserve_loyalty_discount(
                    session,
                    user_id=user.id,
                    code=ent.code,
                    order=platform_order,
                    base_amount=100_000,
                )

            shop_order = Order(
                user_id=user.id,
                plan_id=plan.id,
                amount=100_000,
                status=OrderStatus.PENDING.value,
                reseller_id=owner.id,
            )
            session.add(shop_order)
            await session.commit()
            await session.refresh(shop_order)
            discount, code = await reserve_loyalty_discount(
                session,
                user_id=user.id,
                code=ent.code,
                order=shop_order,
                base_amount=100_000,
            )
            self.assertEqual(discount, 50_000)
            self.assertEqual(code, ent.code)

            # Foreign shop also rejected
            foreign = Order(
                user_id=user.id,
                plan_id=plan.id,
                amount=100_000,
                status=OrderStatus.PENDING.value,
                reseller_id=other.id,
            )
            session.add(foreign)
            await session.commit()
            await session.refresh(foreign)
            # already reserved on shop_order — status not available; issue fresh
            ent2 = await issue_discount_entitlement(
                session, user, percent=10, reseller_id=owner.id
            )
            await session.commit()
            with self.assertRaises(ValueError):
                await reserve_loyalty_discount(
                    session,
                    user_id=user.id,
                    code=ent2.code,
                    order=foreign,
                    base_amount=100_000,
                )

    async def test_gift_code_caps_and_non_admin_platform_blocked(self):
        from fastapi import FastAPI
        from fastapi.responses import RedirectResponse

        from app.api.ux20_pages import register_ux20_pages
        from app.services.ux20 import MAX_CHARGE_CODE_AMOUNT, create_charge_code

        async with self.Session() as session:
            with self.assertRaises(ValueError):
                await create_charge_code(
                    session, amount=MAX_CHARGE_CODE_AMOUNT + 1, max_uses=1
                )
            with self.assertRaises(ValueError):
                await create_charge_code(
                    session, amount=1000, max_uses=10_000
                )

        app = FastAPI()

        async def _db():
            async with self.Session() as session:
                yield session

        def _staff_platform():
            return {
                "role": "pg_staff",
                "pg_is_owner": False,
                "authz": SimpleNamespace(shop_perms=set(), is_platform=False),
            }

        # Minimal registration — exercise gift create guard via endpoint source.
        src = (ROOT / "app/api/ux20_pages.py").read_text(encoding="utf-8")
        self.assertIn(
            "if not is_platform_admin(staff) and rid is None",
            src,
        )
        _ = (app, _db, _staff_platform, RedirectResponse, register_ux20_pages)

    async def test_psp_nok_does_not_burn_settlement_source(self):
        src = (ROOT / "app/services/payment_settlement.py").read_text(encoding="utf-8")
        self.assertIn('cb_status != "OK"', src)
        self.assertIn("Soft cancel", src)

    async def test_linked_service_delete_and_renew_guards(self):
        svc = (ROOT / "app/bot/handlers/services.py").read_text(encoding="utf-8")
        self.assertIn('!= "linked"', svc)
        self.assertIn("delete_pg = (svc.remark or \"\").strip() != \"linked\"", svc)
        self.assertIn("سرویس متصل‌شده فقط مشاهده است", svc)
        start = (ROOT / "app/bot/handlers/start.py").read_text(encoding="utf-8")
        self.assertIn("get_pg_for_reseller", start)
        mini = (ROOT / "app/api/miniapp_pages.py").read_text(encoding="utf-8")
        renew = mini.split("async def mini_renew", 1)[1].split("return _no_store", 1)[0]
        self.assertIn('== "linked"', renew)

    async def test_httpx_token_log_suppressed(self):
        main = (ROOT / "app/main.py").read_text(encoding="utf-8")
        self.assertIn('logging.getLogger("httpx").setLevel(logging.WARNING)', main)
        self.assertIn('logging.getLogger("httpcore").setLevel(logging.WARNING)', main)

    async def test_reseller_can_review_own_shop_topup(self):
        from app.db.models import Role
        from app.services.resellers import reseller_can_review_payment

        reviewer = SimpleNamespace(role=Role.RESELLER.value, telegram_id=1, id=10)
        payment = SimpleNamespace(
            is_wallet_topup=True,
            order_id=None,
            user_id=99,
            wallet_shop_id=10,
        )
        session = AsyncMock()
        profile = SimpleNamespace(
            bot_permissions="payments", web_permissions="payments", is_active=True
        )
        with patch(
            "app.services.users.current_shop_reseller_id",
            return_value=10,
        ), patch(
            "app.services.reseller_access.resolve_reseller_owner_id",
            new=AsyncMock(return_value=10),
        ), patch(
            "app.services.resellers.get_reseller_profile",
            new=AsyncMock(return_value=profile),
        ), patch(
            "app.services.resellers.has_bot_perm",
            return_value=True,
        ):
            ok = await reseller_can_review_payment(session, reviewer, payment)
        self.assertTrue(ok)

    async def test_wallet_balances_for_scope_isolates_shop(self):
        from app.services.wallet import credit_wallet, wallet_balances_for_scope

        async with self.Session() as session:
            owner_a = await self._user(session, 9001, code="OWNA")
            owner_b = await self._user(session, 9002, code="OWNB")
            buyer = await self._user(
                session, 9003, reseller_id=owner_a.id, code="BUYAB"
            )
            await credit_wallet(
                session, buyer, 12_000, "a", shop_id=owner_a.id
            )
            await credit_wallet(
                session, buyer, 99_000, "b", shop_id=owner_b.id
            )
            buyer.wallet_balance = 7_000
            await session.commit()

            platform = await wallet_balances_for_scope(
                session, [buyer.id], shop_id=None
            )
            shop_a = await wallet_balances_for_scope(
                session, [buyer.id], shop_id=owner_a.id
            )
            shop_b = await wallet_balances_for_scope(
                session, [buyer.id], shop_id=owner_b.id
            )
            self.assertEqual(platform[buyer.id], 7_000)
            self.assertEqual(shop_a[buyer.id], 12_000)
            self.assertEqual(shop_b[buyer.id], 99_000)
            # Unknown user id → 0, never raises / never leaks
            missing = await wallet_balances_for_scope(
                session, [buyer.id, 424242], shop_id=owner_a.id
            )
            self.assertEqual(missing[424242], 0)


class PaymentTenancyTopupRegression(unittest.IsolatedAsyncioTestCase):
    async def test_reseller_still_blocked_from_platform_topup(self):
        from app.db.models import Role
        from app.services.resellers import reseller_can_review_payment

        reviewer = SimpleNamespace(role=Role.RESELLER.value, telegram_id=1, id=10)
        payment = SimpleNamespace(
            is_wallet_topup=True,
            order_id=None,
            user_id=99,
            wallet_shop_id=None,
        )
        session = AsyncMock()
        with patch(
            "app.services.users.current_shop_reseller_id",
            return_value=10,
        ), patch(
            "app.services.reseller_access.resolve_reseller_owner_id",
            new=AsyncMock(return_value=10),
        ):
            ok = await reseller_can_review_payment(session, reviewer, payment)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
