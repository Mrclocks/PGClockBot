"""«بزن به کارت»: one remote order per attempt, settle only on a matching paid order, deliver once."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import httpx
import sqlalchemy as sa
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import Base
from app.db.models import BotUser, Payment, PaymentMethod, PaymentSettlement, PaymentStatus, Role, SettlementStatus
from app.services import bbc_settlement as flow
from app.services.payment_providers import bezanbecard as bbc
from app.services.users import clear_settings_cache, set_setting

ROOT = Path(__file__).resolve().parents[1]
KEY = "platform-key-123456"
SHOP_KEY = "shop-key-abcdefgh"
TOKEN = "tok_abcdefgh"


def _order(payment_id: int | None, *, status: str = "paid", base: int | None = 500_000, unique: int = 500_043):
    return bbc.BbcOrder(
        order_id="ord_1",
        payment_token=TOKEN,
        status=status,
        unique_amount_rial=unique,
        base_amount_rial=base,
        merchant_order_id=None if payment_id is None else str(payment_id),
        payment_url=f"https://bezanbecard.ir/pay/{TOKEN}",
    )


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def _with(self, handler) -> None:
        client = httpx.AsyncClient(base_url=bbc.API_BASE, transport=httpx.MockTransport(handler))
        patcher = patch.object(bbc, "_http", return_value=client)
        patcher.start()
        self.addAsyncCleanup(client.aclose)
        self.addCleanup(patcher.stop)

    def _request(self) -> bbc.BbcOrderRequest:
        return bbc.build_order_request(
            amount_toman=50_000,
            merchant_order_id="7",
            callback_url="https://bot.test/cb?t=x",
            return_url="https://bot.test/ret?t=x",
            description="سفارش #7",
            idempotency_key="bbc:7:aa",
        )

    async def test_create_sends_the_documented_request(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                200,
                json={
                    "order_id": "o1",
                    "payment_token": TOKEN,
                    "payment_url": "https://bezanbecard.ir/pay/x",
                    "unique_amount": 500_043,
                    "base_amount": 500_000,
                    "status": "pending",
                },
            )

        await self._with(handler)
        order = await bbc.create_order(api_key=KEY, request=self._request())
        sent = seen[0]
        self.assertEqual((sent.method, sent.url.path), ("POST", "/api/v1/public/orders"))
        self.assertEqual((sent.headers["x-api-key"], sent.headers["idempotency-key"]), (KEY, "bbc:7:aa"))
        body = json.loads(sent.content)
        self.assertEqual((body["amount"], body["merchant_order_id"]), (500_000, "7"))
        self.assertEqual((order.payment_token, order.unique_amount_rial), (TOKEN, 500_043))

    async def test_no_usable_answer_is_unreachable_not_a_refusal(self) -> None:
        answers = [
            httpx.Response(502, text="<html>bad gateway</html>"),
            httpx.Response(200, text="not json"),
            httpx.Response(429, json={"code": "rate_limited", "detail": "x"}),
            httpx.Response(200, json={"status": "pending"}),
        ]
        for answer in answers:
            with self.subTest(status=answer.status_code):
                await self._with(lambda _request, answer=answer: answer)
                with self.assertRaises(bbc.BbcUnreachable):
                    await bbc.create_order(api_key=KEY, request=self._request())

        def timeout(_request):
            raise httpx.ConnectTimeout("slow")

        await self._with(timeout)
        with self.assertRaises(bbc.BbcUnreachable):
            await bbc.create_order(api_key=KEY, request=self._request())

    async def test_a_refusal_shows_a_fixed_text_never_the_api_detail(self) -> None:
        await self._with(lambda _r: httpx.Response(401, json={"code": "unauthenticated", "detail": "<b>secret</b>"}))
        with self.assertRaises(bbc.BbcRejected) as caught:
            await bbc.get_order(api_key=KEY, payment_token=TOKEN)
        self.assertEqual(str(caught.exception), "کلید API بزن به کارت نامعتبر است.")
        self.assertNotIn("secret", str(caught.exception))

    def test_amounts_are_rial_within_the_documented_limits(self) -> None:
        self.assertEqual(bbc.toman_to_rial(1_000), 10_000)
        for bad in (True, 0, 999, 100_000_001, 1.5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                bbc.toman_to_rial(bad)  # type: ignore[arg-type]


class _Db(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        clear_settings_cache()
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 'bbc.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=1, telegram_id=101, referral_code="c1", role=Role.USER.value),
                BotUser(id=5, telegram_id=105, referral_code="s5", role=Role.RESELLER.value),
            ])
            await session.commit()
            for key, value in (("pay_bbc_enabled", "1"), ("bbc_api_key", KEY)):
                await set_setting(session, key, value)
                await set_setting(session, key, SHOP_KEY if key == "bbc_api_key" else value, reseller_id=5)
        clear_settings_cache()
        for patcher in (
            patch("app.services.public_https.env_public_base", return_value="https://bot.test"),
            patch("app.services.public_https.https_is_active", return_value=False),
            patch.object(flow, "notify_after_settlement", AsyncMock()),
            patch("app.services.notifications.notify_settlement_review", AsyncMock(return_value=1)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.delivered = flow.notify_after_settlement
        import app.services.notifications as notifications

        self.reviewed = notifications.notify_settlement_review

    async def asyncTearDown(self) -> None:
        clear_settings_cache()
        await self.engine.dispose()
        self._tmp.cleanup()

    async def _payment(self, session, *, shop: int | None = None, amount: int = 50_000) -> Payment:
        payment = Payment(
            user_id=1,
            amount=amount,
            method=PaymentMethod.BBC.value,
            status=PaymentStatus.PENDING.value,
            is_wallet_topup=True,
            wallet_shop_id=shop,
        )
        session.add(payment)
        await session.commit()
        return payment

    async def _checkout(self, session, payment: Payment) -> PaymentSettlement:
        create = AsyncMock(return_value=_order(payment.id, status="pending"))
        with patch.object(bbc, "create_order", create):
            await flow.start_bbc_checkout(session, payment)
        return await self._row(session, payment)

    async def _row(self, session, payment: Payment) -> PaymentSettlement:
        return (
            await session.execute(
                select(PaymentSettlement)
                .where(PaymentSettlement.payment_id == payment.id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()

    async def _verify(self, session, settlement, order: bbc.BbcOrder) -> AsyncMock:
        get = AsyncMock(return_value=order)
        with patch.object(bbc, "get_order", get):
            await flow.verify_bbc_settlement(session, settlement)
            await flow.deliver_once(session, settlement)
        return get


class CreateTests(_Db):
    async def test_the_request_is_saved_before_the_call_and_replayed_unchanged(self) -> None:
        async with self.Session() as session:
            payment = await self._payment(session)
            failing = AsyncMock(side_effect=bbc.BbcUnreachable(bbc.UNREACHABLE_TEXT))
            with patch.object(bbc, "create_order", failing), self.assertRaises(bbc.BbcUnreachable):
                await flow.start_bbc_checkout(session, payment)
            row = await self._row(session, payment)
            self.assertEqual(row.status, SettlementStatus.CREATED.value)
            working = AsyncMock(return_value=_order(payment.id, status="pending"))
            with patch.object(bbc, "create_order", working):
                checkout = await flow.start_bbc_checkout(session, payment)
            first, second = failing.await_args.kwargs["request"], working.await_args.kwargs["request"]
            self.assertEqual(first, second)
            self.assertEqual(checkout.unique_amount_rial, 500_043)

    async def test_an_open_link_is_shown_again_without_a_new_order(self) -> None:
        async with self.Session() as session:
            payment = await self._payment(session)
            await self._checkout(session, payment)
            create = AsyncMock()
            with patch.object(bbc, "create_order", create):
                again = await flow.start_bbc_checkout(session, payment)
            create.assert_not_awaited()
            self.assertTrue(again.payment_url.startswith("https://"))

    async def test_after_a_refusal_a_new_attempt_uses_a_new_key(self) -> None:
        async with self.Session() as session:
            payment = await self._payment(session)
            refused = AsyncMock(side_effect=bbc.BbcRejected("x", status_code=422, code="validation_error"))
            with patch.object(bbc, "create_order", refused), self.assertRaises(bbc.BbcRejected):
                await flow.start_bbc_checkout(session, payment)
            self.assertEqual((await self._row(session, payment)).status, SettlementStatus.FAILED.value)
            working = AsyncMock(return_value=_order(payment.id, status="pending"))
            with patch.object(bbc, "create_order", working):
                await flow.start_bbc_checkout(session, payment)
            self.assertNotEqual(
                refused.await_args.kwargs["request"].idempotency_key,
                working.await_args.kwargs["request"].idempotency_key,
            )

    async def test_an_idempotency_conflict_goes_to_review_not_to_a_new_order(self) -> None:
        async with self.Session() as session:
            payment = await self._payment(session)
            conflict = AsyncMock(side_effect=bbc.BbcRejected("x", status_code=409))
            with patch.object(bbc, "create_order", conflict), self.assertRaises(ValueError):
                await flow.start_bbc_checkout(session, payment)
            self.assertEqual((await self._row(session, payment)).error_message, "review:create_conflict")
            with self.assertRaises(ValueError):
                await flow.start_bbc_checkout(session, payment)
            self.reviewed.assert_awaited_once()

    async def test_each_shop_uses_its_own_key(self) -> None:
        async with self.Session() as session:
            payment = await self._payment(session, shop=5)
            create = AsyncMock(return_value=_order(payment.id, status="pending"))
            with patch.object(bbc, "create_order", create):
                await flow.start_bbc_checkout(session, payment)
            self.assertEqual(create.await_args.kwargs["api_key"], SHOP_KEY)
            with self.assertRaises(ValueError):
                await flow.start_bbc_checkout(session, payment, reseller_id=6)


class SettleTests(_Db):
    async def test_a_matching_paid_order_settles_and_delivers_once(self) -> None:
        async with self.Session() as session:
            payment = await self._payment(session)
            row = await self._checkout(session, payment)
            for _ in range(3):  # callback, return page reload, poll
                await self._verify(session, row, _order(payment.id))
            await session.refresh(payment)
        self.assertEqual(row.status, SettlementStatus.SETTLED.value)
        self.assertEqual(payment.status, PaymentStatus.APPROVED.value)
        self.delivered.assert_awaited_once()

    async def test_a_paid_order_that_does_not_match_is_reviewed_never_approved(self) -> None:
        cases = {
            "missing order id": lambda pid: _order(None),
            "other order id": lambda pid: _order(pid + 1000),
            "other base amount": lambda pid: _order(pid, base=600_000),
            "no base amount": lambda pid: _order(pid, base=None),
            "other unique amount": lambda pid: _order(pid, unique=500_044),
        }
        for name, make in cases.items():
            with self.subTest(name):
                async with self.Session() as session:
                    payment = await self._payment(session)
                    row = await self._checkout(session, payment)
                    await self._verify(session, row, make(payment.id))
                    await session.refresh(payment)
                self.assertEqual(payment.status, PaymentStatus.PENDING.value)
                self.assertTrue(str(row.error_message).startswith("review:"))
        self.delivered.assert_not_awaited()

    async def test_needs_review_tells_the_staff_once_and_keeps_watching(self) -> None:
        async with self.Session() as session:
            payment = await self._payment(session)
            row = await self._checkout(session, payment)
            for _ in range(3):
                await self._verify(session, row, _order(payment.id, status="needs_review"))
            self.assertEqual(row.status, SettlementStatus.AWAITING.value)
            self.reviewed.assert_awaited_once()
            await self._verify(session, row, _order(payment.id))
        self.assertEqual(row.status, SettlementStatus.SETTLED.value)

    async def test_a_late_payment_settles_while_the_payment_still_waits(self) -> None:
        async with self.Session() as session:
            payment = await self._payment(session)
            row = await self._checkout(session, payment)
            await self._verify(session, row, _order(payment.id, status="expired"))
            self.assertEqual(row.status, SettlementStatus.EXPIRED.value)
            with patch.object(bbc, "get_order", AsyncMock(return_value=_order(payment.id))):
                self.assertEqual(await flow.poll_bbc_settlements(session), 1)
        self.delivered.assert_awaited_once()

    async def test_a_late_payment_for_a_closed_payment_goes_to_review(self) -> None:
        async with self.Session() as session:
            payment = await self._payment(session)
            row = await self._checkout(session, payment)
            await self._verify(session, row, _order(payment.id, status="expired"))
            payment.status = PaymentStatus.REJECTED.value
            await session.commit()
            await self._verify(session, row, _order(payment.id))
        self.assertEqual(row.error_message, "review:late_payment")
        self.reviewed.assert_awaited_once()
        self.delivered.assert_not_awaited()


class LinkTests(_Db):
    async def test_the_link_needs_its_shop_and_token(self) -> None:
        async with self.Session() as session:
            payment = await self._payment(session, shop=5)
            row = await self._checkout(session, payment)
            token = json.loads(row.provider_payload)["link_token"]
            for tenant, given in ((0, token), (5, "x" * len(token)), (5, "")):
                with self.subTest(tenant=tenant), self.assertRaises(ValueError):
                    await flow.handle_bbc_link(session, settlement_id=row.id, tenant_key=tenant, token=given)
            with patch.object(bbc, "get_order", AsyncMock(return_value=_order(payment.id))) as get:
                await flow.handle_bbc_link(session, settlement_id=row.id, tenant_key=5, token=token)
                # A reload after settling still opens, without a second delivery.
                await flow.handle_bbc_link(session, settlement_id=row.id, tenant_key=5, token=token)
        self.assertEqual(get.await_args.kwargs["api_key"], SHOP_KEY)
        self.delivered.assert_awaited_once()

    async def test_an_unreachable_api_leaves_the_payment_waiting(self) -> None:
        async with self.Session() as session:
            payment = await self._payment(session)
            row = await self._checkout(session, payment)
            token = json.loads(row.provider_payload)["link_token"]
            with patch.object(bbc, "get_order", AsyncMock(side_effect=bbc.BbcUnreachable("x"))):
                await flow.handle_bbc_link(session, settlement_id=row.id, tenant_key=0, token=token)
        self.assertEqual(row.status, SettlementStatus.AWAITING.value)


class TopupReuseTests(_Db):
    async def test_an_open_topup_of_the_same_amount_and_shop_is_reused(self) -> None:
        async with self.Session() as session:
            payment = await self._payment(session)
            await self._checkout(session, payment)
            same = await flow.reusable_bbc_topup(session, 1, 50_000, shop_id=None)
            other_amount = await flow.reusable_bbc_topup(session, 1, 60_000, shop_id=None)
            other_shop = await flow.reusable_bbc_topup(session, 1, 50_000, shop_id=5)
        self.assertEqual((same.id, other_amount, other_shop), (payment.id, None, None))


class SecretFieldTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_bot_never_shows_the_key_and_removes_the_typed_one(self) -> None:
        from app.bot.handlers import admin_settings

        actor = patch.object(admin_settings, "_ensure_settings_actor", AsyncMock(return_value=True))
        actor.start()
        self.addCleanup(actor.stop)
        callback = MagicMock(data="adm:st:edit:bbc_api_key", answer=AsyncMock(), message=MagicMock())
        with (
            patch.object(admin_settings, "_is_admin", return_value=True),
            patch.object(admin_settings, "get_setting", AsyncMock(return_value="very-secret-key")),
            patch("app.bot.nav_input.ask_text", AsyncMock()) as asked,
        ):
            await admin_settings.settings_edit_ask(callback, AsyncMock(), MagicMock(), SimpleNamespace(id=1))
        self.assertNotIn("very-secret-key", asked.await_args.kwargs["prompt"])
        message = MagicMock(text="new-secret-key", delete=AsyncMock())
        state = AsyncMock(get_data=AsyncMock(return_value={"edit_key": "bbc_api_key"}))
        with (
            patch.object(admin_settings, "_is_admin", return_value=True),
            patch.object(admin_settings, "set_setting", AsyncMock()) as saved,
            patch.object(admin_settings, "_finish_settings_text_edit", AsyncMock()),
        ):
            await admin_settings.settings_edit_save(message, state, MagicMock(), SimpleNamespace(id=1))
        message.delete.assert_awaited_once()
        self.assertEqual(saved.await_args.args[1:], ("bbc_api_key", "new-secret-key"))


class MigrationTests(unittest.TestCase):
    def test_upgrade_twice_marks_old_settlements_and_downgrade_keeps_rows(self) -> None:
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        spec = importlib.util.spec_from_file_location("m0064", ROOT / "alembic/versions/0064_settlement_notified_at.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE payment_settlements (id INTEGER PRIMARY KEY, status TEXT, settled_at TIMESTAMP, updated_at TIMESTAMP)"
            ))
            conn.execute(text("INSERT INTO payment_settlements VALUES (1, 'settled', '2026-01-01', '2026-01-01'), (2, 'awaiting', NULL, '2026-01-01')"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                rows = conn.execute(text("SELECT id, notified_at IS NOT NULL FROM payment_settlements ORDER BY id")).all()
                self.assertEqual([tuple(r) for r in rows], [(1, 1), (2, 0)])
                migration.downgrade()
                self.assertEqual(conn.execute(text("SELECT COUNT(*) FROM payment_settlements")).scalar(), 2)
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
