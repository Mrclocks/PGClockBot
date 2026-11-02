"""Manual reseller wallet changes: never below zero, once per form, PAYG through the Billing ledger."""

from __future__ import annotations

import os
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.reseller_pages import register_reseller_pages
from app.db import Base
from app.db.models import BotUser, ResellerBillingTransaction, ResellerProfile, WalletTransaction
from app.services import reseller_wallet
from app.services.billing import KIND_ADJUSTMENT, debit_adjustment
from app.services.reseller_wallet import WalletChange, adjust_reseller_wallet, change_notice

PAYG_SHOP = 5
FIXED_SHOP = 6


class _Db:
    async def setup(self) -> None:
        self.engine = create_async_engine(
            "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=PAYG_SHOP, telegram_id=105, referral_code="p", role="reseller", wallet_balance=50_000),
                BotUser(id=FIXED_SHOP, telegram_id=106, referral_code="f", role="reseller", wallet_balance=50_000),
            ])
            await session.flush()
            session.add_all([
                ResellerProfile(user_id=PAYG_SHOP, billing_mode="payg", payg_wallet_linked=True, billing_balance=50_000),
                ResellerProfile(user_id=FIXED_SHOP, billing_mode="fixed"),
            ])
            await session.commit()

    async def balance(self, user_id: int) -> int:
        async with self.Session() as session:
            return int((await session.get(BotUser, user_id)).wallet_balance)

    async def ledger(self) -> list[ResellerBillingTransaction]:
        async with self.Session() as session:
            return list((await session.execute(select(ResellerBillingTransaction))).scalars())

    async def wallet_ledger(self) -> list[WalletTransaction]:
        async with self.Session() as session:
            return list((await session.execute(select(WalletTransaction))).scalars())


class DebitAdjustmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db = _Db()
        await self.db.setup()

    async def asyncTearDown(self) -> None:
        await self.db.engine.dispose()

    async def debit(self, amount, key: str = "k1", shop: int = PAYG_SHOP):
        async with self.db.Session() as session:
            return await debit_adjustment(session, shop, amount, created_by="owner", idempotency_key=key)

    async def test_decrease_lowers_the_wallet_and_records_a_negative_entry(self) -> None:
        tx = await self.debit(20_000)
        self.assertEqual(await self.db.balance(PAYG_SHOP), 30_000)
        self.assertEqual((tx.kind, tx.amount, tx.balance_after), (KIND_ADJUSTMENT, -20_000, 30_000))
        async with self.db.Session() as session:
            profile = (await session.execute(select(ResellerProfile).where(ResellerProfile.user_id == PAYG_SHOP))).scalar_one()
        self.assertEqual(profile.billing_balance, 30_000)

    async def test_decrease_above_the_balance_is_refused_and_changes_nothing(self) -> None:
        with self.assertRaises(ValueError):
            await self.debit(60_000)
        self.assertEqual(await self.db.balance(PAYG_SHOP), 50_000)
        self.assertEqual(await self.db.ledger(), [])

    async def test_the_same_form_submitted_twice_decreases_once(self) -> None:
        await self.debit(10_000, key="same")
        await self.debit(10_000, key="same")
        self.assertEqual(await self.db.balance(PAYG_SHOP), 40_000)
        self.assertEqual(len(await self.db.ledger()), 1)

    async def test_a_fixed_plan_reseller_wallet_is_not_touched(self) -> None:
        with self.assertRaises(ValueError):
            await self.debit(10_000, shop=FIXED_SHOP)
        self.assertEqual(await self.db.balance(FIXED_SHOP), 50_000)

    async def test_non_positive_or_non_integer_amounts_are_refused(self) -> None:
        for amount in (0, -1000, True, 1000.0):
            with self.assertRaises(ValueError):
                await self.debit(amount)
        self.assertEqual(await self.db.balance(PAYG_SHOP), 50_000)


class WalletFormTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db = _Db()
        await self.db.setup()

        async def get_db():
            async with self.db.Session() as session:
                yield session

        app = FastAPI()
        register_reseller_pages(app, render=None, require_admin=lambda: {"username": "owner"}, get_db=get_db)
        self.client = TestClient(app)
        self.followups = mock.patch.object(reseller_wallet, "schedule_followups")
        self.scheduled = self.followups.start()
        self.addCleanup(self.followups.stop)

    async def asyncTearDown(self) -> None:
        await self.db.engine.dispose()

    def submit(self, shop: int = PAYG_SHOP, **form) -> dict[str, list[str]]:
        response = self.client.post(f"/resellers/{shop}/billing-topup", data=form, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        return parse_qs(urlparse(response.headers["location"]).query)

    async def test_the_decrease_button_lowers_the_wallet(self) -> None:
        query = self.submit(amount="15000", mode="debit", nonce="n" * 16)
        self.assertIn("ok", query)
        self.assertEqual(await self.db.balance(PAYG_SHOP), 35_000)

    async def test_a_form_without_a_mode_is_still_a_charge(self) -> None:
        self.submit(amount="15000", nonce="n" * 16)
        self.assertEqual(await self.db.balance(PAYG_SHOP), 65_000)

    async def test_a_decrease_too_large_shows_an_error(self) -> None:
        query = self.submit(amount="90000", mode="debit", nonce="n" * 16)
        self.assertIn("کافی نیست", query["err"][0])
        self.assertEqual(await self.db.balance(PAYG_SHOP), 50_000)

    async def test_a_fixed_plan_reseller_wallet_changes_like_a_user_wallet(self) -> None:
        self.assertIn("ok", self.submit(shop=FIXED_SHOP, amount="20000", mode="debit", nonce="n" * 16))
        self.assertEqual(await self.db.balance(FIXED_SHOP), 30_000)
        self.assertEqual(await self.db.ledger(), [])
        self.assertEqual([tx.amount for tx in await self.db.wallet_ledger()], [-20_000])
        self.scheduled.assert_called_once()

    async def test_a_signed_or_tiny_amount_is_refused(self) -> None:
        for amount in ("-5000", "500"):
            self.assertIn("err", self.submit(amount=amount, mode="debit", nonce="n" * 16))
        self.assertEqual(await self.db.balance(PAYG_SHOP), 50_000)



class ResellerWalletServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db = _Db()
        await self.db.setup()

    async def asyncTearDown(self) -> None:
        await self.db.engine.dispose()

    async def adjust(self, shop: int, amount: int, mode: str, key: str | None = "k") -> WalletChange:
        async with self.db.Session() as session:
            user = await session.get(BotUser, shop)
            return await adjust_reseller_wallet(session, user, amount, mode=mode, actor="owner", idempotency_key=key)

    async def test_a_payg_change_always_goes_through_the_billing_ledger(self) -> None:
        change = await self.adjust(PAYG_SHOP, 20_000, "debit")
        self.assertEqual((change.is_payg, change.balance), (True, 30_000))
        self.assertEqual([tx.amount for tx in await self.db.ledger()], [-20_000])
        self.assertEqual(await self.db.wallet_ledger(), [])

    async def test_emptying_a_payg_wallet_is_flagged_for_suspension(self) -> None:
        self.assertTrue((await self.adjust(PAYG_SHOP, 50_000, "debit")).emptied_payg)
        self.assertFalse((await self.adjust(FIXED_SHOP, 50_000, "debit")).emptied_payg)

    async def test_a_replayed_form_is_not_announced_again(self) -> None:
        first = await self.adjust(PAYG_SHOP, 10_000, "credit", key="same")
        again = await self.adjust(PAYG_SHOP, 10_000, "credit", key="same")
        self.assertFalse(first.replayed)
        self.assertTrue(again.replayed)
        self.assertEqual(await self.db.balance(PAYG_SHOP), 60_000)
        with mock.patch.object(reseller_wallet.asyncio, "get_running_loop") as loop:
            reseller_wallet.schedule_followups(again)
        loop.assert_not_called()

    async def test_a_legacy_billing_pot_is_merged_once_and_kept(self) -> None:
        async with self.db.Session() as session:
            profile = (await session.execute(select(ResellerProfile).where(ResellerProfile.user_id == PAYG_SHOP))).scalar_one()
            profile.payg_wallet_linked = False
            profile.billing_balance = 7_000
            await session.commit()
        change = await self.adjust(PAYG_SHOP, 5_000, "debit")
        self.assertEqual(change.balance, 52_000)
        await self.adjust(PAYG_SHOP, 2_000, "debit", key="k2")
        self.assertEqual(await self.db.balance(PAYG_SHOP), 50_000)

    async def test_followups_suspend_an_emptied_wallet_and_tell_the_reseller(self) -> None:
        change = WalletChange(PAYG_SHOP, "debit", 50_000, 0, True, None)
        suspend, notify = mock.AsyncMock(), mock.AsyncMock()
        with mock.patch("app.db.session.SessionLocal", self.db.Session), \
                mock.patch("app.services.billing_suspend.suspend_payg_reseller", suspend), \
                mock.patch("app.services.billing_suspend.send_reseller_notice", notify):
            await reseller_wallet._run_followups(change)
        suspend.assert_awaited_once()
        notify.assert_awaited_once()

    async def test_a_failed_suspension_still_tells_the_reseller(self) -> None:
        change = WalletChange(PAYG_SHOP, "debit", 50_000, 0, True, None)
        notify = mock.AsyncMock()
        with mock.patch("app.db.session.SessionLocal", self.db.Session), \
                mock.patch("app.services.billing_suspend.suspend_payg_reseller", mock.AsyncMock(side_effect=RuntimeError)), \
                mock.patch("app.services.billing_suspend.send_reseller_notice", notify):
            await reseller_wallet._run_followups(change)
        notify.assert_awaited_once()

    def test_the_notice_escapes_the_admin_note(self) -> None:
        text = change_notice(WalletChange(PAYG_SHOP, "debit", 5_000, 45_000, True, "<b>x</b>"))
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", text)
        self.assertNotIn("<b>x</b>", text)



class UserPageResellerWalletTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from app.api.user_pages import register_user_pages

        self.db = _Db()
        await self.db.setup()

        async def get_db():
            async with self.db.Session() as session:
                yield session

        owner = {"role": "admin", "org_principal_id": 1, "org_depth": 0, "org_parent_id": None, "username": "owner"}
        app = FastAPI()
        register_user_pages(app, render=None, require_admin=lambda: owner, get_db=get_db)
        self.client = TestClient(app)
        patcher = mock.patch.object(reseller_wallet, "schedule_followups")
        self.scheduled = patcher.start()
        self.addCleanup(patcher.stop)

    async def asyncTearDown(self) -> None:
        await self.db.engine.dispose()

    async def test_a_payg_reseller_changed_from_the_user_page_uses_the_billing_ledger(self) -> None:
        response = self.client.post(
            f"/users/{PAYG_SHOP}/wallet-credit", data={"amount": "5000", "mode": "debit", "nonce": "n" * 16},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(await self.db.balance(PAYG_SHOP), 45_000)
        self.assertEqual([tx.amount for tx in await self.db.ledger()], [-5_000])
        self.assertEqual(await self.db.wallet_ledger(), [])
        self.scheduled.assert_called_once()


def _render_reseller_page(*, payg: bool) -> str:
    from types import SimpleNamespace

    from app.api.app import templates

    return templates.env.get_template("_reseller_edit_body.html").render(
        request=SimpleNamespace(url=SimpleNamespace(path="/resellers"), state=SimpleNamespace(), query_params={}),
        user=SimpleNamespace(id=PAYG_SHOP, wallet_balance=50_000, full_name="r", username="r", telegram_id=1, role="reseller"),
        profile=SimpleNamespace(billing_suspended_at=None, billing_watermark_bytes=0, is_active=True,
                                pg_admin_username="x", billing_mode="payg" if payg else "fixed"),
        is_payg=payg, topup_nonce="n" * 16, wallet_balance=50_000, billing_rate=1000, staff={"role": "admin"},
    )


class ResellerPageFormTests(unittest.TestCase):
    def test_both_reseller_kinds_get_one_wallet_form_with_both_buttons(self) -> None:
        for payg in (True, False):
            html = _render_reseller_page(payg=payg)
            self.assertEqual(html.count('name="mode" value="debit"'), 1)
            self.assertEqual(html.count('name="mode" value="credit"'), 1)
            self.assertIn(f'action="/resellers/{PAYG_SHOP}/billing-topup"', html)

    def test_only_payg_warns_that_an_empty_wallet_cuts_the_reseller_off(self) -> None:
        self.assertIn("همان لحظه قطع", _render_reseller_page(payg=True))
        self.assertNotIn("همان لحظه قطع", _render_reseller_page(payg=False))


class BotConfirmTextTests(unittest.TestCase):
    def text(self, amount: int, mode: str, *, payg: bool) -> str:
        from types import SimpleNamespace

        from app.bot.handlers.admin import _reseller_wallet_confirm_text

        user = SimpleNamespace(full_name="<i>shop</i>", telegram_id=1, wallet_balance=50_000)
        return _reseller_wallet_confirm_text(user, amount, mode, payg=payg)

    def test_emptying_a_payg_wallet_warns_before_applying(self) -> None:
        self.assertIn("قطع می‌شوند", self.text(50_000, "debit", payg=True))
        self.assertNotIn("قطع می‌شوند", self.text(50_000, "debit", payg=False))

    def test_a_shortfall_is_shown_before_applying(self) -> None:
        self.assertIn("موجودی کافی نیست", self.text(60_000, "debit", payg=True))

    def test_the_reseller_name_is_escaped(self) -> None:
        self.assertIn("&lt;i&gt;shop&lt;/i&gt;", self.text(1_000, "credit", payg=False))


if __name__ == "__main__":
    unittest.main()
