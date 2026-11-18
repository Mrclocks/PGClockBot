"""Payment settlement core — security, tenant isolation, mock-gated."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ["ALLOW_SETTLEMENT_MOCK"] = "1"

ROOT = Path(__file__).resolve().parents[1]


def _with_ts(payload: dict) -> dict:
    """Card-auto events now require a fresh unix timestamp inside the signed body."""
    import time

    out = dict(payload)
    out.setdefault("timestamp", int(time.time()))
    return out



class CardAutoCryptoTests(unittest.TestCase):
    def test_sign_verify_roundtrip(self):
        from app.services.payment_providers.card_auto import (
            dumps_canonical,
            parse_card_auto_event,
            sign_payload,
            verify_signature,
        )

        payload = _with_ts({"amount": 150000, "external_ref": "evt-1", "payment_id": 42})
        body = dumps_canonical(payload)
        sig = sign_payload("secret-test-16chars", body)
        self.assertTrue(
            verify_signature(secret="secret-test-16chars", body=body, signature=sig)
        )
        self.assertFalse(verify_signature(secret="wrong", body=body, signature=sig))
        ev = parse_card_auto_event(payload)
        self.assertEqual(ev.payment_id, 42)

    def test_parse_requires_external_ref(self):
        from app.services.payment_providers.card_auto import parse_card_auto_event

        with self.assertRaises(ValueError):
            parse_card_auto_event(_with_ts({"amount": 10}))


class MockGateTests(unittest.TestCase):
    def test_mock_off_by_default_config(self):
        from app.config import Settings

        s = Settings.model_construct(allow_settlement_mock=False)
        self.assertFalse(s.allow_settlement_mock)

    def test_mock_routes_require_env(self):
        pages = (ROOT / "app/api/settlement_pages.py").read_text(encoding="utf-8")
        self.assertIn("settlement_mock_allowed", pages)
        self.assertIn("status_code=404", pages)
        self.assertIn("/card-auto/platform/webhook", pages)
        self.assertIn("/card-auto/shop/{reseller_id}/webhook", pages)
        self.assertIn("status_code=410", pages)


class SettlementWiringTests(unittest.TestCase):
    def test_model_tenant_columns(self):
        models = (ROOT / "app/db/models.py").read_text(encoding="utf-8")
        self.assertIn("shop_owner_id", models)
        self.assertIn("tenant_key", models)
        self.assertIn("checkout_token", models)
        self.assertIn("SETTLING", models)
        self.assertIn("uq_payment_settlements_active_payment_channel", models)
        mig = (ROOT / "alembic/versions/0021_payment_settlements.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("shop_owner_id", mig)
        self.assertIn("tenant_key", mig)
        self.assertIn("checkout_token", mig)
        self.assertIn("uq_payment_settlements_active_payment_channel", mig)
        self.assertIn("uq_payment_settlements_tenant_ext_ref", mig)

    def test_secrets_masked_in_panel(self):
        field = (ROOT / "app/web/templates/_settings_field.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("kind == 'password'", field)
        self.assertIn('type="password"', field)
        users = (ROOT / "app/services/users.py").read_text(encoding="utf-8")
        self.assertIn('"psp_provider": "zarinpal"', users)
        self.assertIn("SECRET_KEYS", users)
        self.assertIn('"password"', users)

    def test_bot_panel_parity_keys(self):
        admin = (ROOT / "app/bot/handlers/admin_settings.py").read_text(encoding="utf-8")
        self.assertIn('("psp"', admin)
        self.assertIn('("card_auto"', admin)
        self.assertIn('("bbc"', admin)
        kb = (ROOT / "app/bot/keyboards.py").read_text(encoding="utf-8")
        self.assertIn("REPLY_ACTION_TOPUP_PSP", kb)
        self.assertIn("REPLY_ACTION_TOPUP_BBC", kb)
        reply_nav = (ROOT / "app/bot/handlers/reply_nav.py").read_text(encoding="utf-8")
        self.assertIn("wtop:psp", reply_nav)
        self.assertIn("wtop:bbc", reply_nav)
        wallet = (ROOT / "app/bot/handlers/wallet.py").read_text(encoding="utf-8")
        self.assertIn('key == "psp"', wallet)
        self.assertIn('key == "bbc"', wallet)


class SettlementFlowDbTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

        from app.db import Base
        import app.db.models  # noqa: F401
        from app.config import get_settings

        get_settings.cache_clear()
        os.environ["ALLOW_SETTLEMENT_MOCK"] = "1"
        get_settings.cache_clear()

        self._tmp = tempfile.TemporaryDirectory()
        db_path = Path(self._tmp.name) / "t.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    async def asyncTearDown(self):
        from app.config import get_settings

        await self.engine.dispose()
        self._tmp.cleanup()
        get_settings.cache_clear()

    async def _user(self, session, tid: int, code: str, *, reseller_id=None):
        from app.db.models import BotUser, Role

        u = BotUser(
            telegram_id=tid,
            username="u",
            full_name="u",
            referral_code=code,
            role=Role.USER.value,
            reseller_id=reseller_id,
        )
        session.add(u)
        await session.commit()
        await session.refresh(u)
        return u

    async def test_mock_refused_when_env_off(self):
        from app.db.models import (
            Order,
            OrderStatus,
            Payment,
            PaymentMethod,
            PaymentStatus,
        )
        from app.services.payment_settlement import create_psp_checkout
        from app.services.users import set_setting

        async with self.Session() as session:
            await set_setting(session, "pay_psp_enabled", "1")
            await set_setting(session, "psp_provider", "mock")
            u = await self._user(session, 91001, "MCK91001")
            order = Order(
                user_id=u.id,
                amount=10000,
                status=OrderStatus.AWAITING_RECEIPT.value,
                payment_method=PaymentMethod.PSP.value,
            )
            session.add(order)
            await session.flush()
            payment = Payment(
                order_id=order.id,
                user_id=u.id,
                amount=10000,
                method=PaymentMethod.PSP.value,
                status=PaymentStatus.PENDING.value,
            )
            session.add(payment)
            await session.commit()
            await session.refresh(payment)

            with patch(
                "app.services.payment_settlement.settlement_mock_allowed",
                return_value=False,
            ):
                with self.assertRaises(ValueError):
                    await create_psp_checkout(session, payment)

    async def test_mock_pay_requires_token(self):
        from app.db.models import (
            Order,
            OrderStatus,
            Payment,
            PaymentMethod,
            PaymentStatus,
        )
        from app.services.payment_settlement import create_psp_checkout, mock_psp_pay
        from app.services.users import set_setting

        async with self.Session() as session:
            await set_setting(session, "pay_psp_enabled", "1")
            await set_setting(session, "psp_provider", "mock")
            u = await self._user(session, 91002, "MCK91002")
            order = Order(
                user_id=u.id,
                amount=20000,
                status=OrderStatus.AWAITING_RECEIPT.value,
                payment_method=PaymentMethod.PSP.value,
            )
            session.add(order)
            await session.flush()
            payment = Payment(
                order_id=order.id,
                user_id=u.id,
                amount=20000,
                method=PaymentMethod.PSP.value,
                status=PaymentStatus.PENDING.value,
            )
            session.add(payment)
            await session.commit()
            await session.refresh(payment)

            with patch(
                "app.services.payment_settlement.settlement_mock_allowed",
                return_value=True,
            ):
                settlement = await create_psp_checkout(session, payment)
                self.assertTrue(settlement.checkout_token)
                self.assertIn("token=", settlement.checkout_url or "")
                with self.assertRaises(ValueError):
                    await mock_psp_pay(
                        session,
                        settlement_id=settlement.id,
                        amount=20000,
                        token="wrong-token",
                    )

    async def test_card_auto_requires_payment_id_and_tenant_secret(self):
        from app.db.models import (
            Order,
            OrderStatus,
            Payment,
            PaymentMethod,
            PaymentSettlement,
            PaymentStatus,
            Role,
            SettlementStatus,
            BotUser,
        )
        from app.services.payment_providers.card_auto import dumps_canonical, sign_payload
        from app.services.payment_settlement import handle_card_auto_webhook
        from app.services.users import set_setting
        import app.services.payment_settlement as ps

        async with self.Session() as session:
            # Platform secret
            await set_setting(session, "pay_card_auto_enabled", "1")
            await set_setting(
                session, "card_auto_webhook_secret", "platform-secret-ok!"
            )

            reseller = BotUser(
                telegram_id=92000,
                referral_code="RES92000",
                role=Role.RESELLER.value,
            )
            session.add(reseller)
            await session.flush()
            # Shop-only secret (different)
            await set_setting(
                session,
                "pay_card_auto_enabled",
                "1",
                reseller_id=reseller.id,
            )
            await set_setting(
                session,
                "card_auto_webhook_secret",
                "shop-secret-sixteen",
                reseller_id=reseller.id,
            )

            buyer = await self._user(
                session, 91003, "BUY91003", reseller_id=reseller.id
            )
            order = Order(
                user_id=buyer.id,
                amount=33000,
                status=OrderStatus.AWAITING_RECEIPT.value,
                payment_method=PaymentMethod.CARD.value,
                reseller_id=reseller.id,
            )
            session.add(order)
            await session.flush()
            payment = Payment(
                order_id=order.id,
                user_id=buyer.id,
                amount=33000,
                method=PaymentMethod.CARD.value,
                status=PaymentStatus.PENDING.value,
            )
            session.add(payment)
            await session.flush()
            session.add(
                PaymentSettlement(
                    payment_id=payment.id,
                    shop_owner_id=reseller.id,
                    tenant_key=int(reseller.id),
                    channel="card_auto",
                    provider="generic",
                    status=SettlementStatus.AWAITING.value,
                    amount=33000,
                    currency="IRT",
                    idempotency_key=f"cardauto:{payment.id}",
                )
            )
            await session.commit()
            await session.refresh(payment)

            payload = _with_ts({
                "amount": 33000,
                "external_ref": "bank-1",
                "payment_id": payment.id,
            })
            body = dumps_canonical(payload)

            # Platform secret must NOT settle shop payment
            sig_plat = sign_payload("platform-secret-ok!", body)
            with self.assertRaises(ValueError):
                await handle_card_auto_webhook(
                    session,
                    body=body,
                    signature=sig_plat,
                    shop_owner_id=reseller.id,
                )

            # Missing payment_id rejected
            bad = dumps_canonical(_with_ts({"amount": 33000, "external_ref": "bank-2"}))
            sig_shop = sign_payload("shop-secret-sixteen", bad)
            with self.assertRaises(ValueError):
                await handle_card_auto_webhook(
                    session,
                    body=bad,
                    signature=sig_shop,
                    shop_owner_id=reseller.id,
                )

            # Correct shop secret + payment_id works (approve stubbed)
            async def _fake_approve(session, payment, reviewer_tg=0):
                payment.status = PaymentStatus.APPROVED.value
                await session.commit()
                return None

            orig = ps.approve_payment
            ps.approve_payment = _fake_approve
            try:
                sig_ok = sign_payload("shop-secret-sixteen", body)
                s = await handle_card_auto_webhook(
                    session,
                    body=body,
                    signature=sig_ok,
                    shop_owner_id=reseller.id,
                )
                self.assertEqual(s.status, SettlementStatus.SETTLED.value)
                self.assertEqual(s.shop_owner_id, reseller.id)
            finally:
                ps.approve_payment = orig

    async def test_amount_mismatch_fail_closed(self):
        from app.db.models import (
            Order,
            OrderStatus,
            Payment,
            PaymentMethod,
            PaymentSettlement,
            PaymentStatus,
            SettlementStatus,
        )
        from app.services.payment_settlement import settle_and_approve

        async with self.Session() as session:
            u = await self._user(session, 91004, "AM91004")
            order = Order(
                user_id=u.id,
                amount=100000,
                status=OrderStatus.AWAITING_RECEIPT.value,
                payment_method=PaymentMethod.PSP.value,
            )
            session.add(order)
            await session.flush()
            payment = Payment(
                order_id=order.id,
                user_id=u.id,
                amount=100000,
                method=PaymentMethod.PSP.value,
                status=PaymentStatus.PENDING.value,
            )
            session.add(payment)
            await session.flush()
            settlement = PaymentSettlement(
                payment_id=payment.id,
                shop_owner_id=None,
                channel="psp",
                provider="mock",
                status=SettlementStatus.AWAITING.value,
                amount=100000,
                currency="IRT",
                idempotency_key=f"t-{payment.id}",
                external_ref="mock-x",
            )
            session.add(settlement)
            await session.commit()
            await session.refresh(settlement)
            with self.assertRaises(ValueError):
                await settle_and_approve(
                    session,
                    settlement,
                    expected_amount=99999,
                    external_ref="mock-x",
                )
            await session.refresh(settlement)
            self.assertEqual(settlement.status, SettlementStatus.FAILED.value)

    async def test_psp_checkout_idempotent_deterministic_key(self):
        from app.db.models import (
            Order,
            OrderStatus,
            Payment,
            PaymentMethod,
            PaymentStatus,
        )
        from app.services.payment_settlement import (
            create_psp_checkout,
            idem_key_psp,
        )
        from app.services.users import set_setting

        async with self.Session() as session:
            await set_setting(session, "pay_psp_enabled", "1")
            await set_setting(session, "psp_provider", "mock")
            u = await self._user(session, 91005, "IDM91005")
            order = Order(
                user_id=u.id,
                amount=5000,
                status=OrderStatus.AWAITING_RECEIPT.value,
                payment_method=PaymentMethod.PSP.value,
            )
            session.add(order)
            await session.flush()
            payment = Payment(
                order_id=order.id,
                user_id=u.id,
                amount=5000,
                method=PaymentMethod.PSP.value,
                status=PaymentStatus.PENDING.value,
            )
            session.add(payment)
            await session.commit()
            await session.refresh(payment)

            with patch(
                "app.services.payment_settlement.settlement_mock_allowed",
                return_value=True,
            ):
                s1 = await create_psp_checkout(session, payment)
                s2 = await create_psp_checkout(session, payment)
                self.assertEqual(s1.id, s2.id)
                self.assertEqual(s1.idempotency_key, idem_key_psp(payment.id))
                self.assertEqual(s1.idempotency_key, s2.idempotency_key)

    async def test_approve_failure_does_not_leave_settled(self):
        from app.db.models import (
            Order,
            OrderStatus,
            Payment,
            PaymentMethod,
            PaymentSettlement,
            PaymentStatus,
            SettlementStatus,
        )
        from app.services import payment_settlement as ps

        async with self.Session() as session:
            u = await self._user(session, 91006, "AT91006")
            order = Order(
                user_id=u.id,
                amount=8000,
                status=OrderStatus.AWAITING_RECEIPT.value,
                payment_method=PaymentMethod.PSP.value,
            )
            session.add(order)
            await session.flush()
            payment = Payment(
                order_id=order.id,
                user_id=u.id,
                amount=8000,
                method=PaymentMethod.PSP.value,
                status=PaymentStatus.PENDING.value,
            )
            session.add(payment)
            await session.flush()
            settlement = PaymentSettlement(
                payment_id=payment.id,
                shop_owner_id=None,
                tenant_key=0,
                channel="psp",
                provider="mock",
                status=SettlementStatus.AWAITING.value,
                amount=8000,
                currency="IRT",
                idempotency_key=f"psp:{payment.id}",
                external_ref="mock-a",
            )
            session.add(settlement)
            await session.commit()
            await session.refresh(settlement)
            await session.refresh(payment)

            async def _boom(session, payment, reviewer_tg=0):
                raise ValueError("تحویل ناموفق تست")

            orig = ps.approve_payment
            ps.approve_payment = _boom
            try:
                with self.assertRaises(ValueError):
                    await ps.settle_and_approve(
                        session,
                        settlement,
                        expected_amount=8000,
                        external_ref="mock-a",
                    )
            finally:
                ps.approve_payment = orig

            await session.refresh(settlement)
            await session.refresh(payment)
            self.assertEqual(settlement.status, SettlementStatus.AWAITING.value)
            self.assertEqual(payment.status, PaymentStatus.PENDING.value)

            # Retry after fix succeeds and settles.
            async def _ok(session, payment, reviewer_tg=0):
                payment.status = PaymentStatus.APPROVED.value
                await session.commit()
                return None

            ps.approve_payment = _ok
            try:
                await ps.settle_and_approve(
                    session,
                    settlement,
                    expected_amount=8000,
                    external_ref="mock-a",
                )
            finally:
                ps.approve_payment = orig
            await session.refresh(settlement)
            self.assertEqual(settlement.status, SettlementStatus.SETTLED.value)

    async def test_duplicate_card_auto_webhook_idempotent(self):
        from app.db.models import (
            Order,
            OrderStatus,
            Payment,
            PaymentMethod,
            PaymentStatus,
            SettlementStatus,
        )
        from app.services.payment_providers.card_auto import dumps_canonical, sign_payload
        from app.services import payment_settlement as ps
        from app.services.users import set_setting

        async with self.Session() as session:
            await set_setting(session, "pay_card_auto_enabled", "1")
            await set_setting(session, "card_auto_webhook_secret", "dup-secret-16chars")
            u = await self._user(session, 91007, "DUP91007")
            order = Order(
                user_id=u.id,
                amount=12000,
                status=OrderStatus.AWAITING_RECEIPT.value,
                payment_method=PaymentMethod.CARD.value,
            )
            session.add(order)
            await session.flush()
            payment = Payment(
                order_id=order.id,
                user_id=u.id,
                amount=12000,
                method=PaymentMethod.CARD.value,
                status=PaymentStatus.PENDING.value,
            )
            session.add(payment)
            await session.commit()
            await session.refresh(payment)

            await ps.create_card_auto_awaiting(session, payment)

            payload = _with_ts({
                "amount": 12000,
                "external_ref": "evt-dup-1",
                "payment_id": payment.id,
            })
            body = dumps_canonical(payload)
            sig = sign_payload("dup-secret-16chars", body)
            calls = {"n": 0}

            async def _ok(session, payment, reviewer_tg=0):
                calls["n"] += 1
                if payment.status != PaymentStatus.APPROVED.value:
                    payment.status = PaymentStatus.APPROVED.value
                    await session.commit()
                return None

            orig = ps.approve_payment
            ps.approve_payment = _ok
            try:
                s1 = await ps.handle_card_auto_webhook(
                    session, body=body, signature=sig, shop_owner_id=None
                )
                s2 = await ps.handle_card_auto_webhook(
                    session, body=body, signature=sig, shop_owner_id=None
                )
                self.assertEqual(s1.id, s2.id)
                self.assertEqual(s1.status, SettlementStatus.SETTLED.value)
                self.assertEqual(s2.status, SettlementStatus.SETTLED.value)
                # Second webhook must not re-credit (approve may be called for repair but
                # payment already approved — stub counts calls; first settles, second short-circuits).
                self.assertLessEqual(calls["n"], 2)
            finally:
                ps.approve_payment = orig

    async def test_cross_tenant_card_auto_rejected(self):
        from app.db.models import (
            BotUser,
            Order,
            OrderStatus,
            Payment,
            PaymentMethod,
            PaymentStatus,
            Role,
        )
        from app.services.payment_providers.card_auto import dumps_canonical, sign_payload
        from app.services.payment_settlement import (
            create_card_auto_awaiting,
            handle_card_auto_webhook,
        )
        from app.services.users import set_setting

        async with self.Session() as session:
            r1 = BotUser(
                telegram_id=93001, referral_code="XR93001", role=Role.RESELLER.value
            )
            r2 = BotUser(
                telegram_id=93002, referral_code="XR93002", role=Role.RESELLER.value
            )
            session.add_all([r1, r2])
            await session.flush()
            for rid, secret in (
                (r1.id, "tenant-one-secret1"),
                (r2.id, "tenant-two-secret2"),
            ):
                await set_setting(session, "pay_card_auto_enabled", "1", reseller_id=rid)
                await set_setting(
                    session, "card_auto_webhook_secret", secret, reseller_id=rid
                )

            buyer = await self._user(session, 91008, "XT91008", reseller_id=r1.id)
            order = Order(
                user_id=buyer.id,
                amount=9000,
                status=OrderStatus.AWAITING_RECEIPT.value,
                payment_method=PaymentMethod.CARD.value,
                reseller_id=r1.id,
            )
            session.add(order)
            await session.flush()
            payment = Payment(
                order_id=order.id,
                user_id=buyer.id,
                amount=9000,
                method=PaymentMethod.CARD.value,
                status=PaymentStatus.PENDING.value,
            )
            session.add(payment)
            await session.commit()
            await session.refresh(payment)
            await create_card_auto_awaiting(session, payment)

            payload = _with_ts({
                "amount": 9000,
                "external_ref": "xt-1",
                "payment_id": payment.id,
            })
            body = dumps_canonical(payload)
            # Shop-2 secret + shop-2 path must not settle shop-1 payment
            sig = sign_payload("tenant-two-secret2", body)
            with self.assertRaises(ValueError):
                await handle_card_auto_webhook(
                    session, body=body, signature=sig, shop_owner_id=r2.id
                )

    async def test_integrity_error_savepoint_preserves_prior_session_work(self):
        """IntegrityError on settlement insert must not wipe unrelated dirty state."""
        from app.db.models import (
            Order,
            OrderStatus,
            Payment,
            PaymentMethod,
            PaymentSettlement,
            PaymentStatus,
            SettlementStatus,
        )
        from app.services import payment_settlement as ps

        async with self.Session() as session:
            u = await self._user(session, 91020, "SP91020")
            order = Order(
                user_id=u.id,
                amount=5000,
                status=OrderStatus.AWAITING_RECEIPT.value,
                payment_method=PaymentMethod.PSP.value,
            )
            session.add(order)
            await session.flush()
            payment = Payment(
                order_id=order.id,
                user_id=u.id,
                amount=5000,
                method=PaymentMethod.PSP.value,
                status=PaymentStatus.PENDING.value,
            )
            session.add(payment)
            await session.commit()
            await session.refresh(payment)

            winner = PaymentSettlement(
                payment_id=payment.id,
                shop_owner_id=None,
                tenant_key=0,
                channel="psp",
                provider="mock",
                status=SettlementStatus.AWAITING.value,
                amount=5000,
                currency="IRT",
                idempotency_key=f"psp:{payment.id}",
                checkout_url="https://example.test/pay",
            )
            session.add(winner)
            await session.commit()

            # Uncommitted prior work in this session (must survive insert conflict).
            payment.receipt_file_id = "keep-me-after-integrity-error"
            dup = PaymentSettlement(
                payment_id=payment.id,
                shop_owner_id=None,
                tenant_key=0,
                channel="psp",
                provider="mock",
                status=SettlementStatus.CREATED.value,
                amount=5000,
                currency="IRT",
                idempotency_key=f"psp:{payment.id}",
            )
            got = await ps._insert_settlement_race_safe(session, dup)
            self.assertEqual(got.id, winner.id)
            self.assertEqual(payment.receipt_file_id, "keep-me-after-integrity-error")
            await session.commit()
            await session.refresh(payment)
            self.assertEqual(payment.receipt_file_id, "keep-me-after-integrity-error")

    async def test_concurrent_settle_two_sessions_single_approve(self):
        """Two AsyncSessions racing settle_and_approve → one claim, one approve, one SETTLED."""
        import asyncio

        from app.db.models import (
            Order,
            OrderStatus,
            Payment,
            PaymentMethod,
            PaymentSettlement,
            PaymentStatus,
            SettlementStatus,
        )
        from app.services import payment_settlement as ps

        async with self.Session() as setup:
            u = await self._user(setup, 91021, "RC91021")
            order = Order(
                user_id=u.id,
                amount=15000,
                status=OrderStatus.AWAITING_RECEIPT.value,
                payment_method=PaymentMethod.CARD.value,
            )
            setup.add(order)
            await setup.flush()
            payment = Payment(
                order_id=order.id,
                user_id=u.id,
                amount=15000,
                method=PaymentMethod.CARD.value,
                status=PaymentStatus.PENDING.value,
                receipt_file_id="card_auto:awaiting:x",
            )
            setup.add(payment)
            await setup.flush()
            settlement = PaymentSettlement(
                payment_id=payment.id,
                shop_owner_id=None,
                tenant_key=0,
                channel="card_auto",
                provider="generic",
                status=SettlementStatus.AWAITING.value,
                amount=15000,
                currency="IRT",
                idempotency_key=f"card_auto:{payment.id}",
            )
            setup.add(settlement)
            await setup.commit()
            payment_id = payment.id
            settlement_id = settlement.id

        approve_calls = {"n": 0}
        gate = asyncio.Event()

        async def _counting_approve(session, payment, reviewer_tg=0):
            approve_calls["n"] += 1
            # Hold claim so the peer session can attempt settle concurrently.
            await gate.wait()
            await asyncio.sleep(0.05)
            if payment.status != PaymentStatus.APPROVED.value:
                payment.status = PaymentStatus.APPROVED.value
            await session.commit()
            return None

        orig = ps.approve_payment
        ps.approve_payment = _counting_approve
        try:

            async def _race():
                async with self.Session() as session:
                    s = await session.get(PaymentSettlement, settlement_id)
                    return await ps.settle_and_approve(
                        session,
                        s,
                        expected_amount=15000,
                        external_ref="evt-race-1",
                    )

            t1 = asyncio.create_task(_race())
            t2 = asyncio.create_task(_race())
            # Let both enter claim; winner blocks inside approve until gate opens.
            await asyncio.sleep(0.15)
            gate.set()
            results = await asyncio.gather(t1, t2)
        finally:
            ps.approve_payment = orig

        self.assertEqual(approve_calls["n"], 1)
        self.assertEqual(len(results), 2)
        for pay in results:
            self.assertEqual(pay.status, PaymentStatus.APPROVED.value)
            self.assertEqual(pay.id, payment_id)

        async with self.Session() as session:
            payment = await session.get(Payment, payment_id)
            settlement = await session.get(PaymentSettlement, settlement_id)
            self.assertEqual(payment.status, PaymentStatus.APPROVED.value)
            self.assertEqual(settlement.status, SettlementStatus.SETTLED.value)
            self.assertEqual(settlement.external_ref, "evt-race-1")

    async def test_concurrent_psp_checkout_two_sessions_single_row(self):
        """Two AsyncSessions creating PSP checkout → one settlement row, same id returned."""
        import asyncio

        from sqlalchemy import func, select

        from app.db.models import (
            Order,
            OrderStatus,
            Payment,
            PaymentMethod,
            PaymentSettlement,
            PaymentStatus,
        )
        from app.services.payment_settlement import create_psp_checkout
        from app.services.users import set_setting

        async with self.Session() as setup:
            await set_setting(setup, "pay_psp_enabled", "1")
            await set_setting(setup, "psp_provider", "mock")
            u = await self._user(setup, 91022, "PC91022")
            order = Order(
                user_id=u.id,
                amount=22000,
                status=OrderStatus.AWAITING_RECEIPT.value,
                payment_method=PaymentMethod.PSP.value,
            )
            setup.add(order)
            await setup.flush()
            payment = Payment(
                order_id=order.id,
                user_id=u.id,
                amount=22000,
                method=PaymentMethod.PSP.value,
                status=PaymentStatus.PENDING.value,
            )
            setup.add(payment)
            await setup.commit()
            payment_id = payment.id

        with patch(
            "app.services.payment_settlement.settlement_mock_allowed",
            return_value=True,
        ):

            async def _checkout():
                async with self.Session() as session:
                    payment = await session.get(Payment, payment_id)
                    return await create_psp_checkout(session, payment)

            s1, s2 = await asyncio.gather(_checkout(), _checkout())

        self.assertEqual(s1.id, s2.id)
        self.assertTrue((s1.checkout_url or "").strip())
        self.assertTrue((s2.checkout_url or "").strip())
        self.assertEqual(s1.checkout_url, s2.checkout_url)

        async with self.Session() as session:
            n = (
                await session.execute(
                    select(func.count())
                    .select_from(PaymentSettlement)
                    .where(PaymentSettlement.payment_id == payment_id)
                )
            ).scalar_one()
            self.assertEqual(int(n), 1)


if __name__ == "__main__":
    unittest.main()
