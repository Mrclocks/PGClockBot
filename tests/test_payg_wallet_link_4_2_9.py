"""PAYG linked to shop wallet + auto-unsuspend on sufficient credit (v4.2.9)."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.billing import BILLING_MODE_PAYG


class PaygWalletLinkTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_link_merges_wallet_and_billing(self):
        from app.services.billing import ensure_payg_shop_wallet

        profile = MagicMock()
        profile.user_id = 5
        profile.billing_balance = -379
        profile.payg_wallet_linked = False

        user = MagicMock()
        user.id = 5
        user.wallet_balance = 9000

        session = AsyncMock()
        session.get = AsyncMock(return_value=user)
        # claim link (rowcount=1) then wallet += billing; refresh applies merge
        session.execute = AsyncMock(return_value=MagicMock(rowcount=1))

        async def _refresh(obj):
            if obj is user:
                user.wallet_balance = 8621

        session.refresh = AsyncMock(side_effect=_refresh)
        session.no_autoflush = MagicMock()
        session.no_autoflush.__enter__ = MagicMock(return_value=None)
        session.no_autoflush.__exit__ = MagicMock(return_value=False)

        out_user, bal = await ensure_payg_shop_wallet(session, profile)
        self.assertEqual(bal, 8621)
        self.assertEqual(user.wallet_balance, 8621)
        self.assertEqual(profile.billing_balance, 8621)
        self.assertTrue(profile.payg_wallet_linked)
        self.assertIs(out_user, user)
        self.assertEqual(session.execute.await_count, 2)

    async def test_first_link_loser_mirrors_wallet_only(self):
        from app.services.billing import ensure_payg_shop_wallet

        profile = MagicMock()
        profile.user_id = 5
        profile.billing_balance = -379
        profile.payg_wallet_linked = False

        user = MagicMock()
        user.id = 5
        user.wallet_balance = 5000

        session = AsyncMock()
        session.get = AsyncMock(return_value=user)
        session.execute = AsyncMock(return_value=MagicMock(rowcount=0))

        async def _refresh(obj):
            if obj is profile:
                profile.payg_wallet_linked = True
                profile.billing_balance = 5000
            if obj is user:
                user.wallet_balance = 5000

        session.refresh = AsyncMock(side_effect=_refresh)
        session.no_autoflush = MagicMock()
        session.no_autoflush.__enter__ = MagicMock(return_value=None)
        session.no_autoflush.__exit__ = MagicMock(return_value=False)

        _u, bal = await ensure_payg_shop_wallet(session, profile)
        self.assertEqual(bal, 5000)
        self.assertEqual(user.wallet_balance, 5000)
        self.assertEqual(session.execute.await_count, 1)

    async def test_linked_mirrors_wallet_only(self):
        from app.services.billing import ensure_payg_shop_wallet

        profile = MagicMock()
        profile.user_id = 5
        profile.billing_balance = 100
        profile.payg_wallet_linked = True

        user = MagicMock()
        user.id = 5
        user.wallet_balance = 5000

        session = AsyncMock()
        session.get = AsyncMock(return_value=user)

        _u, bal = await ensure_payg_shop_wallet(session, profile)
        self.assertEqual(bal, 5000)
        self.assertEqual(profile.billing_balance, 5000)
        self.assertEqual(user.wallet_balance, 5000)


class AutoUnsuspendAfterCreditTests(unittest.IsolatedAsyncioTestCase):
    async def test_restore_when_credit_meets_2x(self):
        from app.services.billing_suspend import maybe_restore_after_wallet_credit

        profile = MagicMock()
        profile.user_id = 9
        profile.billing_mode = BILLING_MODE_PAYG
        profile.billing_suspended_at = object()
        profile.billing_balance = 0
        profile.payg_wallet_linked = True

        session = AsyncMock()
        session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=profile))
        )

        with (
            patch(
                "app.services.billing_suspend.min_topup_to_unsuspend",
                AsyncMock(return_value=20_000),
            ),
            patch(
                "app.services.billing.ensure_payg_shop_wallet",
                AsyncMock(return_value=(MagicMock(wallet_balance=20_000), 20_000)),
            ),
            patch(
                "app.services.billing_suspend.restore_payg_reseller",
                AsyncMock(return_value={"users_restored": 1}),
            ) as restore,
            patch("app.services.billing_suspend.is_payg", return_value=True),
        ):
            stats = await maybe_restore_after_wallet_credit(
                session, 9, 20_000, commit=False
            )
        self.assertEqual(stats["restored"], 1)
        restore.assert_awaited()

    async def test_no_restore_when_credit_below_2x(self):
        from app.services.billing_suspend import maybe_restore_after_wallet_credit

        profile = MagicMock()
        profile.user_id = 9
        profile.billing_mode = BILLING_MODE_PAYG
        profile.billing_suspended_at = object()
        profile.billing_balance = 0
        profile.payg_wallet_linked = True

        session = AsyncMock()
        session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=profile))
        )

        with (
            patch(
                "app.services.billing_suspend.min_topup_to_unsuspend",
                AsyncMock(return_value=20_000),
            ),
            patch(
                "app.services.billing.ensure_payg_shop_wallet",
                AsyncMock(return_value=(MagicMock(wallet_balance=5_000), 5_000)),
            ),
            patch(
                "app.services.billing_suspend.restore_payg_reseller",
                AsyncMock(return_value={"users_restored": 1}),
            ) as restore,
            patch("app.services.billing_suspend.is_payg", return_value=True),
        ):
            stats = await maybe_restore_after_wallet_credit(
                session, 9, 5_000, commit=False
            )
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["restored"], 0)
        restore.assert_not_awaited()


class SchemaLinkFlagTests(unittest.TestCase):
    def test_model_and_migrate(self):
        from app.db.models import ResellerProfile

        self.assertTrue(hasattr(ResellerProfile, "payg_wallet_linked"))
        session_src = Path("app/db/session.py").read_text(encoding="utf-8")
        self.assertIn("payg_wallet_linked", session_src)
        alem = Path("alembic/versions/0006_payg_wallet_linked.py").read_text(encoding="utf-8")
        self.assertIn("payg_wallet_linked", alem)


if __name__ == "__main__":
    unittest.main()
