"""Bot user admin (web + telegram) — wallet & services (v4.3.0)."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch


class BotUserAdminServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_credit_rejects_non_positive(self):
        from app.services.bot_user_admin import admin_credit_user_wallet

        with self.assertRaises(ValueError):
            await admin_credit_user_wallet(
                AsyncMock(), MagicMock(), 0, actor="a"
            )

    async def test_credit_calls_wallet(self):
        from app.services.bot_user_admin import admin_credit_user_wallet

        user = MagicMock(id=3, wallet_balance=1000)
        with patch(
            "app.services.bot_user_admin.credit_wallet",
            AsyncMock(return_value=user),
        ) as credit:
            out = await admin_credit_user_wallet(
                AsyncMock(), user, 5000, actor="admin", note="test"
            )
        self.assertIs(out, user)
        credit.assert_awaited()
        args = credit.await_args
        self.assertEqual(args.args[2], 5000)
        self.assertIn("شارژ ادمین", args.args[3])

    async def test_get_owned_service_guards(self):
        from app.services.bot_user_admin import get_owned_service
        from unittest.mock import MagicMock

        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=result)
        with self.assertRaises(ValueError):
            await get_owned_service(session, bot_user_id=1, service_id=9, shop_id=None)

        svc = MagicMock(bot_user_id=2)
        result.scalar_one_or_none.return_value = svc
        session.execute = AsyncMock(return_value=result)
        with self.assertRaises(ValueError):
            await get_owned_service(session, bot_user_id=1, service_id=9, shop_id=None)


if __name__ == "__main__":
    unittest.main()
