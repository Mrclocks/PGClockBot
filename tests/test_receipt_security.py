"""Security checks for finance receipt proxy scoping."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("WEB_SECRET", "test-web-secret-for-receipt-scope-32ch")

from app.api.finance_pages import _authorize_receipt_access, _safe_telegram_file_path


class ReceiptPathSafetyTests(unittest.TestCase):
    def test_accepts_normal_telegram_paths(self):
        self.assertEqual(
            _safe_telegram_file_path("photos/file_123.jpg"),
            "photos/file_123.jpg",
        )
        self.assertEqual(
            _safe_telegram_file_path("/photos/file_123.jpg"),
            "photos/file_123.jpg",
        )

    def test_rejects_traversal_and_schemes(self):
        self.assertIsNone(_safe_telegram_file_path("../etc/passwd"))
        self.assertIsNone(_safe_telegram_file_path("photos/../../etc/passwd"))
        self.assertIsNone(_safe_telegram_file_path("https://evil.example/a.jpg"))
        self.assertIsNone(_safe_telegram_file_path("file:foo"))
        self.assertIsNone(_safe_telegram_file_path("photos\\file.jpg"))
        self.assertIsNone(_safe_telegram_file_path("photos/file.jpg?x=1"))
        self.assertIsNone(_safe_telegram_file_path(""))
        self.assertIsNone(_safe_telegram_file_path(None))


class ReceiptSourceGuardsTests(unittest.TestCase):
    def test_receipt_route_has_tenant_guards(self):
        src = open("app/api/finance_pages.py", encoding="utf-8").read()
        self.assertIn("_authorize_receipt_access", src)
        self.assertIn("wallet_shop_id", src)
        self.assertIn('startswith("image/")', src)
        self.assertIn("no-store", src)
        # Must not fall back from reseller token to platform token.
        token_fn = src.split("async def _payment_bot_token", 1)[1].split(
            "async def _safe_telegram_file_path", 1
        )[0]
        self.assertNotIn("or platform", token_fn.lower())
        # Platform payments list scopes top-ups by purse, not sticky BotUser.
        self.assertIn("Payment.wallet_shop_id.is_(None)", src)


class ReceiptTopupTenantAuthzTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from app.db.models import Base
        import app.db.models  # noqa: F401

        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "receipt_authz.db"
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

    async def _topup(
        self,
        session,
        *,
        user_id: int,
        wallet_shop_id: int | None,
    ):
        from app.db.models import Payment, PaymentMethod, PaymentStatus

        pay = Payment(
            user_id=user_id,
            amount=50_000,
            method=PaymentMethod.CARD.value,
            status=PaymentStatus.PENDING.value,
            is_wallet_topup=True,
            wallet_shop_id=wallet_shop_id,
            receipt_file_id="file-abc",
        )
        session.add(pay)
        await session.commit()
        await session.refresh(pay)
        return pay

    async def test_platform_topup_ignores_sticky_shop_for_receipt(self):
        """Sticky BotUser.reseller_id must not make a shop own a platform top-up receipt."""
        async with self.Session() as session:
            shop_a = await self._user(session, 1001, code="SA")
            buyer = await self._user(
                session, 1002, reseller_id=shop_a.id, code="BUY"
            )
            pay = await self._topup(
                session, user_id=buyer.id, wallet_shop_id=None
            )

            platform = {
                "org_principal_id": 1,
                "org_depth": 0,
                "org_status": "active",
            }
            shop_staff = {"role": "reseller", "bot_user_id": shop_a.id}

            allowed = await _authorize_receipt_access(session, platform, pay)
            self.assertIsNotNone(allowed)
            self.assertEqual(allowed[1], None)

            denied = await _authorize_receipt_access(session, shop_staff, pay)
            self.assertIsNone(denied)

    async def test_shop_topup_receipt_only_for_matching_shop(self):
        async with self.Session() as session:
            shop_a = await self._user(session, 2001, code="A")
            shop_b = await self._user(session, 2002, code="B")
            # Sticky attribution is shop A, but top-up purse is shop B.
            buyer = await self._user(
                session, 2003, reseller_id=shop_a.id, code="BUYB"
            )
            pay = await self._topup(
                session, user_id=buyer.id, wallet_shop_id=shop_b.id
            )

            staff_a = {"role": "reseller", "bot_user_id": shop_a.id}
            staff_b = {"role": "reseller", "bot_user_id": shop_b.id}
            platform = {
                "org_principal_id": 1,
                "org_depth": 0,
                "org_status": "active",
            }

            self.assertIsNone(
                await _authorize_receipt_access(session, staff_a, pay)
            )
            allowed = await _authorize_receipt_access(session, staff_b, pay)
            self.assertIsNotNone(allowed)
            self.assertEqual(allowed[1], shop_b.id)
            self.assertIsNone(
                await _authorize_receipt_access(session, platform, pay)
            )


if __name__ == "__main__":
    unittest.main()
