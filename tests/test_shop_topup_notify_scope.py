"""Shop wallet top-up staff notifies must follow payment.wallet_shop_id.

Regression: top-ups always forced platform notify (shop=False + resolve→None),
so shop staff who can approve never saw pending receipts, while platform
admins got cross-tenant top-up traffic.
"""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("WEB_SECRET", "test-web-secret-for-topup-notify-32chars")


class ResolveTopupShopScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_shop_topup_resolves_wallet_shop_id(self):
        from app.services.notifications import _resolve_shop_reseller_id

        session = AsyncMock()
        payment = SimpleNamespace(
            is_wallet_topup=True, wallet_shop_id=42, order_id=None
        )
        rid = await _resolve_shop_reseller_id(session, payment=payment)
        self.assertEqual(rid, 42)

    async def test_platform_topup_resolves_none(self):
        from app.services.notifications import _resolve_shop_reseller_id

        session = AsyncMock()
        payment = SimpleNamespace(
            is_wallet_topup=True, wallet_shop_id=None, order_id=None
        )
        rid = await _resolve_shop_reseller_id(session, payment=payment)
        self.assertIsNone(rid)

    async def test_sticky_user_reseller_not_used_for_topup(self):
        from app.services.notifications import _resolve_shop_reseller_id

        session = AsyncMock()
        session.get = AsyncMock(
            return_value=SimpleNamespace(id=99, reseller_id=77)
        )
        payment = SimpleNamespace(
            is_wallet_topup=True, wallet_shop_id=None, order_id=None, user_id=99
        )
        # Platform purse top-up must stay platform even if payer has sticky shop.
        rid = await _resolve_shop_reseller_id(
            session, payment=payment, ticket_user_id=99
        )
        self.assertIsNone(rid)


class PendingTopupNotifyRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_shop_topup_pending_notifies_shop_not_platform(self):
        from app.services.notifications import notify_pending_approval

        session = AsyncMock()
        session.get = AsyncMock(
            return_value=SimpleNamespace(
                id=5, full_name="Buyer", username="buyer", telegram_id=111
            )
        )
        payment = SimpleNamespace(
            id=901,
            user_id=5,
            amount=50_000,
            is_wallet_topup=True,
            wallet_shop_id=42,
            order_id=None,
            receipt_file_id="file-shop-topup",
        )
        bot = MagicMock()
        bot.token = "shop-bot-token"

        with patch(
            "app.services.notifications._dispatch_dual_notify",
            new=AsyncMock(return_value=1),
        ) as dispatch:
            await notify_pending_approval(bot, session, payment, 111)

        self.assertEqual(dispatch.await_count, 1)
        kwargs = dispatch.await_args.kwargs
        self.assertEqual(kwargs.get("payment"), payment)
        # Default shop=True — must not force platform-only routing.
        self.assertNotIn("shop", kwargs)
        self.assertEqual(kwargs.get("photo"), "file-shop-topup")

    async def test_dispatch_shop_topup_uses_shop_recipients(self):
        from app.services.notifications import _dispatch_dual_notify

        session = AsyncMock()
        payment = SimpleNamespace(
            id=902,
            is_wallet_topup=True,
            wallet_shop_id=42,
            order_id=None,
        )

        async def enabled(_session, key, *, reseller_id=None):
            if reseller_id is None:
                return True  # platform ON — must still not receive shop top-up
            return key == "notify_pending_approval"

        with (
            patch(
                "app.services.notifications.notify_enabled", side_effect=enabled
            ),
            patch(
                "app.services.notifications._send_to_chats", new=AsyncMock(return_value=1)
            ) as send,
            patch(
                "app.services.notifications._shop_recipient_chat_ids",
                new=AsyncMock(return_value=[555]),
            ),
            patch(
                "app.services.resellers.get_reseller_profile",
                new=AsyncMock(
                    return_value=SimpleNamespace(bot_token="shop-token")
                ),
            ),
            patch(
                "app.services.reseller_bots.open_notify_bot_for_reseller",
                new=AsyncMock(
                    return_value=(
                        MagicMock(
                            token="shop-token",
                            session=MagicMock(close=AsyncMock()),
                        ),
                        True,
                    )
                ),
            ),
        ):
            bot = MagicMock()
            bot.token = "main-token"
            await _dispatch_dual_notify(
                bot,
                session,
                "notify_pending_approval",
                "pending shop topup",
                payment=payment,
            )

        self.assertEqual(send.await_count, 1)
        self.assertEqual(send.await_args.args[1], [555])


if __name__ == "__main__":
    unittest.main()
