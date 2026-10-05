"""Regression tests for 2.0.2 final audit fixes."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


class InactiveResellerGrantGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_inactive_reseller_without_web_still_blocks_grant(self):
        from app.services.pg_staff_access import conflict_message_for_new_grant

        reseller = SimpleNamespace(
            id=1,
            user_id=10,
            web_username=None,
            web_password_hash=None,
            is_active=False,
            setup_completed_at=None,
            pg_admin_username="pg_inactive",
        )
        with (
            patch(
                "app.services.pg_staff_access.access_by_pg_username",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "app.services.pg_staff_access.reseller_by_pg_username",
                new=AsyncMock(return_value=reseller),
            ),
            patch(
                "app.services.pg_staff_access.load_web_admin",
                return_value={"username": "owner"},
            ),
            patch(
                "app.services.resellers.setup_is_complete",
                return_value=False,
            ),
        ):
            msg = await conflict_message_for_new_grant(AsyncMock(), "pg_inactive")
        self.assertIsNotNone(msg)
        self.assertIn("نماینده", msg)


class NotAdminRedirectTests(unittest.TestCase):
    def test_not_admin_accepts_live_redirect(self):
        from app.api.app import NotAdmin

        exc = NotAdmin(redirect="/pg/users")
        self.assertEqual(exc.redirect, "/pg/users")

    def test_require_pg_perm_raises_with_redirect(self):
        src = Path("app/api/app.py").read_text(encoding="utf-8")
        self.assertIn("raise NotAdmin(redirect=_live_pg_home(features))", src)
        self.assertIn("live = getattr(exc, \"redirect\", None)", src)


class ResetRevokePermissionTests(unittest.TestCase):
    def test_reset_revoke_not_widened_by_update(self):
        src = Path("app/api/pg_pages.py").read_text(encoding="utf-8")
        self.assertIn('can_reset=actions["reset_usage"]', src)
        self.assertIn('can_revoke=actions["revoke_sub"]', src)
        self.assertNotIn('can_reset=actions["reset_usage"] or actions["update"]', src)
        self.assertNotIn('acts["reset_usage"] or acts["update"]', src)
        self.assertNotIn('acts["revoke_sub"] or acts["update"]', src)


class RenewFallthroughTests(unittest.TestCase):
    def test_renew_missing_service_raises(self):
        src = Path("app/services/orders.py").read_text(encoding="utf-8")
        self.assertIn('raise ValueError("سفارش تمدید ناقص است")', src)
        self.assertIn('raise ValueError("سرویس یا پلن تمدید یافت نشد")', src)
        # Must not fall through to deliver_order after a renew: note
        tree = ast.parse(src)
        renew_guards = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
                renew_guards += 1
        self.assertGreaterEqual(renew_guards, 2)


class ApprovePaymentAtomicTests(unittest.TestCase):
    def test_approve_uses_update_where_pending(self):
        src = Path("app/services/orders.py").read_text(encoding="utf-8")
        self.assertIn("update(Payment)", src)
        self.assertIn("Payment.status == PaymentStatus.PENDING.value", src)
        self.assertIn("claim.rowcount != 1", src)
        self.assertIn("session.no_autoflush", src)


class WalletAtomicTests(unittest.TestCase):
    def test_debit_uses_conditional_update(self):
        src = Path("app/services/wallet.py").read_text(encoding="utf-8")
        self.assertIn("BotUser.wallet_balance >= int(amount)", src)
        self.assertIn("update(BotUser)", src)


class FreeDeliveryRevertTests(unittest.TestCase):
    def test_revert_helper_exported(self):
        from app.services.orders import revert_failed_free_delivery

        self.assertTrue(callable(revert_failed_free_delivery))

    def test_shop_calls_revert_on_deliver_failure(self):
        src = Path("app/bot/handlers/shop.py").read_text(encoding="utf-8")
        self.assertIn("revert_failed_free_delivery", src)
        self.assertGreaterEqual(src.count("revert_failed_free_delivery"), 2)


class DeliveryResellerSettingsTests(unittest.TestCase):
    def test_delivery_passes_reseller_id(self):
        src = Path("app/services/delivery.py").read_text(encoding="utf-8")
        self.assertIn("get_all_settings(session, reseller_id=shop_rid)", src)


class SubLinkClaimTests(unittest.TestCase):
    def test_start_rejects_token_bound_to_other_user(self):
        src = Path("app/bot/handlers/start.py").read_text(encoding="utf-8")
        self.assertIn("این اشتراک قبلاً به حساب دیگری وصل شده است", src)
        self.assertIn("UserService.bot_user_id != db_user.id", src)


class SchedulerPanelOnlyTests(unittest.TestCase):
    def test_scheduler_starts_only_when_bot_online(self):
        src = Path("app/main.py").read_text(encoding="utf-8")
        # start_scheduler must appear after successful get_me path
        self.assertIn("start_scheduler(bot)", src)
        idx_me = src.find("await bot.get_me()")
        idx_sched = src.find("start_scheduler(bot)")
        self.assertGreater(idx_sched, idx_me)


class OwnerSetFailureTests(unittest.TestCase):
    def test_create_does_not_use_owner_token_transfer(self):
        """Staff/reseller create must rely on `_staff_pg` credentials, not get_pg()+set_owner."""
        src = Path("app/api/pg_pages.py").read_text(encoding="utf-8")
        create_fn = src.split('async def pg_users_create', 1)[1].split(
            "async def pg_users_link", 1
        )[0]
        self.assertIn("await _staff_pg(", create_fn)
        self.assertNotIn("get_pg().set_owner_by_id", create_fn)
        self.assertNotIn("await get_pg().set_owner_by_id", create_fn)
        self.assertNotIn("await get_pg().delete_user_by_id", create_fn)
        self.assertNotIn("مالکیت ست نشد", create_fn)


class GroupEditAllowlistTests(unittest.TestCase):
    def test_edit_group_checks_allowlist(self):
        src = Path("app/api/pg_pages.py").read_text(encoding="utf-8")
        self.assertIn("groups_allowed_for_staff(staff, [eid])", src)


class RsetupRemovedTests(unittest.TestCase):
    def test_rsetup_module_removed(self):
        self.assertFalse(Path("app/api/reseller_setup.py").exists())
        self.assertFalse(Path("app/web/templates/reseller_setup.html").exists())
        app_src = Path("app/api/app.py").read_text(encoding="utf-8")
        self.assertNotIn("register_reseller_setup", app_src)
        self.assertNotIn("/rsetup/", app_src)


class VersionBumpTests(unittest.TestCase):
    def test_version_is_current(self):
        from app.version import __version__
        from app.services.updates import is_same_or_newer

        self.assertTrue(is_same_or_newer(__version__, "0.1.0"))
        self.assertEqual(Path("VERSION").read_text(encoding="utf-8").strip(), __version__)
        from app.services.release_notes import RELEASE_NOTES_FA

        self.assertEqual(list(RELEASE_NOTES_FA.keys())[0], "0.1.8")


class PayWithWalletRefundPaymentTests(unittest.IsolatedAsyncioTestCase):
    async def test_delivery_failure_rejects_wallet_payment(self):
        from app.db.models import OrderStatus, PaymentStatus
        from app.services.orders import pay_with_wallet

        order = MagicMock()
        order.id = 7
        order.user_id = 3
        order.amount = 1000
        order.status = OrderStatus.PENDING.value
        order.note = None
        order.service_id = None
        order.plan_id = None

        payment_holder: dict = {}

        user = MagicMock()
        user.id = 3

        claim_result = SimpleNamespace(rowcount=1)
        session = AsyncMock()
        session.add = MagicMock(side_effect=lambda obj: payment_holder.setdefault("p", obj))
        session.commit = AsyncMock()

        async def _refresh(obj):
            if obj is order:
                order.status = OrderStatus.PAID.value

        session.refresh = AsyncMock(side_effect=_refresh)
        session.get = AsyncMock(return_value=None)
        session.execute = AsyncMock(return_value=claim_result)
        session.no_autoflush = MagicMock()
        session.no_autoflush.__enter__ = MagicMock(return_value=session)
        session.no_autoflush.__exit__ = MagicMock(return_value=False)

        with (
            patch("app.services.orders.debit_wallet", new=AsyncMock()),
            patch(
                "app.services.orders.credit_wallet",
                new=AsyncMock(),
            ) as credit,
            patch(
                "app.services.orders.deliver_order",
                new=AsyncMock(side_effect=RuntimeError("pg down")),
            ),
            patch(
                "app.services.orders.Payment",
                side_effect=lambda **kw: SimpleNamespace(**kw, review_note=None),
            ),
        ):
            with self.assertRaises(RuntimeError):
                await pay_with_wallet(session, order, user)

        self.assertEqual(order.status, OrderStatus.PENDING.value)
        self.assertTrue(credit.await_count >= 1)
        p = payment_holder.get("p")
        self.assertIsNotNone(p)
        self.assertEqual(p.status, PaymentStatus.REJECTED.value)


if __name__ == "__main__":
    unittest.main()
