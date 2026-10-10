"""Admin signed wallet adjust (credit / debit) — fail-closed shortfall."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


class ParseAdminWalletAmountTests(unittest.TestCase):
    def test_signed_and_persian(self):
        from app.services.bot_user_admin import parse_admin_wallet_amount

        self.assertEqual(parse_admin_wallet_amount("50000"), 50000)
        self.assertEqual(parse_admin_wallet_amount("-50000"), -50000)
        self.assertEqual(parse_admin_wallet_amount("−۵۰۰۰۰"), -50000)
        self.assertEqual(parse_admin_wallet_amount("+1000"), 1000)

    def test_rejects_float_and_empty(self):
        from app.services.bot_user_admin import parse_admin_wallet_amount

        with self.assertRaises(ValueError):
            parse_admin_wallet_amount("12.5")
        with self.assertRaises(ValueError):
            parse_admin_wallet_amount("1e3")
        with self.assertRaises(ValueError):
            parse_admin_wallet_amount("")
        with self.assertRaises(ValueError):
            parse_admin_wallet_amount("-")


class AdminWalletAdjustServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_credit_rejects_bool(self):
        from app.services.bot_user_admin import admin_credit_user_wallet

        with self.assertRaises(ValueError):
            await admin_credit_user_wallet(AsyncMock(), MagicMock(), True, actor="a")

    async def test_debit_rejects_non_positive_and_over_cap(self):
        from app.services.bot_user_admin import (
            MAX_ADMIN_WALLET_DEBIT,
            admin_debit_user_wallet,
        )

        with self.assertRaises(ValueError):
            await admin_debit_user_wallet(AsyncMock(), MagicMock(), 0, actor="a")
        with self.assertRaises(ValueError):
            await admin_debit_user_wallet(AsyncMock(), MagicMock(), -1, actor="a")
        with self.assertRaises(ValueError):
            await admin_debit_user_wallet(
                AsyncMock(), MagicMock(), MAX_ADMIN_WALLET_DEBIT + 1, actor="a"
            )

    async def test_adjust_dispatches_credit_and_debit(self):
        from app.services.bot_user_admin import admin_adjust_user_wallet

        user = MagicMock(id=3, wallet_balance=10_000)
        session = AsyncMock()
        with (
            patch(
                "app.services.bot_user_admin.admin_credit_user_wallet",
                AsyncMock(return_value=user),
            ) as credit,
            patch(
                "app.services.bot_user_admin.admin_debit_user_wallet",
                AsyncMock(return_value=user),
            ) as debit,
        ):
            await admin_adjust_user_wallet(session, user, 5000, actor="admin")
            credit.assert_awaited_once()
            debit.assert_not_awaited()
            credit.reset_mock()
            await admin_adjust_user_wallet(session, user, -5000, actor="admin")
            debit.assert_awaited_once()
            self.assertEqual(debit.await_args.args[2], 5000)
            credit.assert_not_awaited()

    async def test_adjust_rejects_zero_and_below_floor(self):
        from app.services.bot_user_admin import admin_adjust_user_wallet

        with self.assertRaises(ValueError):
            await admin_adjust_user_wallet(AsyncMock(), MagicMock(), 0, actor="a")
        with self.assertRaises(ValueError):
            await admin_adjust_user_wallet(AsyncMock(), MagicMock(), 500, actor="a")
        with self.assertRaises(ValueError):
            await admin_adjust_user_wallet(AsyncMock(), MagicMock(), -500, actor="a")

    async def test_debit_calls_wallet_service(self):
        from app.services.bot_user_admin import admin_debit_user_wallet

        user = MagicMock(id=3, wallet_balance=20_000)
        with patch(
            "app.services.bot_user_admin.debit_wallet",
            AsyncMock(return_value=user),
        ) as debit:
            out = await admin_debit_user_wallet(
                AsyncMock(), user, 5000, actor="admin", note="fix"
            )
        self.assertIs(out, user)
        debit.assert_awaited()
        args = debit.await_args
        self.assertEqual(args.args[2], 5000)
        self.assertIn("کسر ادمین", args.args[3])
        self.assertIn("fix", args.args[3])


class AdminWalletDebitIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import Base
        import app.db.models  # noqa: F401

        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "wallet-adjust.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self._tmpdir.cleanup()

    async def _user(self, session, *, balance: int = 0):
        from app.db.models import BotUser, Role

        row = BotUser(
            telegram_id=900_001,
            role=Role.USER.value,
            wallet_balance=balance,
            referral_code="ref900001",
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row

    async def test_debit_reduces_balance_and_ledger(self):
        from app.db.models import WalletTransaction
        from app.services.bot_user_admin import admin_adjust_user_wallet
        from sqlalchemy import select

        async with self.Session() as session:
            user = await self._user(session, balance=50_000)
            await admin_adjust_user_wallet(
                session, user, -20_000, actor="test", note="fix"
            )
            await session.refresh(user)
            self.assertEqual(user.wallet_balance, 30_000)
            tx = (
                await session.execute(
                    select(WalletTransaction)
                    .where(WalletTransaction.user_id == user.id)
                    .order_by(WalletTransaction.id.desc())
                )
            ).scalars().first()
            self.assertIsNotNone(tx)
            self.assertEqual(tx.amount, -20_000)
            self.assertEqual(tx.balance_after, 30_000)
            self.assertIn("کسر ادمین", tx.reason)

    async def test_debit_fails_closed_on_shortfall(self):
        from app.services.bot_user_admin import admin_adjust_user_wallet

        async with self.Session() as session:
            user = await self._user(session, balance=5_000)
            with self.assertRaises(ValueError) as ctx:
                await admin_adjust_user_wallet(
                    session, user, -20_000, actor="test"
                )
            self.assertIn("موجودی", str(ctx.exception))
            await session.refresh(user)
            self.assertEqual(user.wallet_balance, 5_000)

    async def test_credit_still_works(self):
        from app.services.bot_user_admin import admin_adjust_user_wallet

        async with self.Session() as session:
            user = await self._user(session, balance=1_000)
            await admin_adjust_user_wallet(session, user, 4_000, actor="test")
            await session.refresh(user)
            self.assertEqual(user.wallet_balance, 5_000)


class AdminWalletAdjustUiContractTests(unittest.TestCase):
    def test_template_signed_copy(self):
        edit = Path("app/web/templates/_user_edit_body.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("تنظیم کیف پول", edit)
        self.assertIn("مبلغ منفی", edit)
        self.assertIn("wallet-credit", edit)
        self.assertNotIn("افزایش کیف پول", edit)

    def test_route_uses_adjust(self):
        pages = Path("app/api/user_pages.py").read_text(encoding="utf-8")
        self.assertIn("admin_adjust_user_wallet", pages)
        self.assertIn("parse_admin_wallet_amount", pages)

    def test_bot_prompt_mentions_negative(self):
        admin = Path("app/bot/handlers/admin.py").read_text(encoding="utf-8")
        fn = admin.split("async def adm_users_wallet_credit_ask", 1)[1].split(
            "async def ", 1
        )[0]
        self.assertIn("منفی", fn)
        save = admin.split("async def adm_users_wallet_credit_save", 1)[1].split(
            "async def ", 1
        )[0]
        self.assertIn("admin_adjust_user_wallet", save)
        kb = Path("app/bot/keyboards.py").read_text(encoding="utf-8")
        self.assertIn("تنظیم کیف پول", kb)


if __name__ == "__main__":
    unittest.main()
