"""App-layer residual security fixes (N1/N2/N3, H1, M10, M11, Low) + select scroll."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


ROOT = Path(__file__).resolve().parents[1]


def _enable_sqlite_fk(dbapi_conn, _connection_record):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


class SourceContractTests(unittest.TestCase):
    def test_n1_delete_rejects_pending_shop_topups(self):
        src = (ROOT / "app/services/users.py").read_text(encoding="utf-8")
        block = src.split("async def delete_bot_user", 1)[1].split(
            "\ndef friendly_user_delete_error", 1
        )[0]
        self.assertIn(
            "delete(LoyaltyDiscountEntitlement).where(\n"
            "            LoyaltyDiscountEntitlement.reseller_id == user_id",
            block,
        )
        self.assertIn("Payment.status == PaymentStatus.PENDING.value", block)
        self.assertIn('status=PaymentStatus.REJECTED.value', block)
        # Must not NULL reseller_id on entitlements (promotes to platform).
        self.assertNotIn(
            "LoyaltyDiscountEntitlement.reseller_id == user_id)\n"
            "        .values(reseller_id=None)",
            block,
        )

    def test_h1_mini_renew_blocks_linked(self):
        src = (ROOT / "app/api/miniapp_pages.py").read_text(encoding="utf-8")
        fn = src.split("async def mini_renew", 1)[1].split("return _no_store", 1)[0]
        self.assertIn('== "linked"', fn)
        self.assertIn("سرویس متصل‌شده فقط مشاهده است", fn)

    def test_m11_view_and_msg_require_dashboard(self):
        src = (ROOT / "app/bot/handlers/reseller.py").read_text(encoding="utf-8")
        view = src.split("async def res_user_view", 1)[1].split(
            "async def res_user_message_start", 1
        )[0]
        msg = src.split("async def res_user_message_start", 1)[1].split(
            "async def res_user_message_send", 1
        )[0]
        send = src.split("async def res_user_message_send", 1)[1].split(
            "async def res_user_quick_renew", 1
        )[0]
        for block, name in ((view, "view"), (msg, "msg"), (send, "send")):
            self.assertIn(
                'has_bot_perm(profile, "dashboard")',
                block,
                msg=f"missing dashboard check in {name}",
            )

    def test_m10_import_uses_shop_allowlist_and_plan_gates(self):
        src = (ROOT / "app/services/ux20.py").read_text(encoding="utf-8")
        self.assertIn("def shop_bundle_allowed_setting_keys", src)
        self.assertIn("async def _validate_import_plan", src)
        self.assertIn("plan_template_denial", src)
        self.assertIn("plan_groups_denial", src)
        self.assertIn("assert_user_plan_within_limits", src)
        fn = src.split("async def import_shop_bundle", 1)[1].split(
            "\ndef capacity_used_pct", 1
        )[0]
        self.assertIn("shop_bundle_allowed_setting_keys()", fn)
        self.assertIn("staff=staff", (ROOT / "app/api/ux20_pages.py").read_text())

    def test_low_bulk_order_approve_needs_payments(self):
        src = (ROOT / "app/services/table_bulk.py").read_text(encoding="utf-8")
        fn = src.split("async def bulk_order_action", 1)[1].split(
            "async def bulk_payment_action", 1
        )[0]
        self.assertIn('"payments" not in perms', fn)

    def test_select_scroll_allows_ported_menu_under_modal_guard(self):
        js = (ROOT / "app/web/static/panel.js").read_text(encoding="utf-8")
        self.assertIn("function portedOverlayScroller", js)
        self.assertIn("portedOverlayScroller(e.target)", js)
        self.assertIn("set('overflow-y', 'auto')", js)
        place = js.split("function placeUiSelectMenu", 1)[1].split(
            "function clearUiSelectMenuPos", 1
        )[0]
        self.assertIn("overflow-y", place)
        css = (ROOT / "app/web/static/panel.css").read_text(encoding="utf-8")
        menu = css.split(".ui-select-menu {", 1)[1].split("}", 1)[0]
        self.assertIn("overflow-y: auto", menu)
        self.assertIn("max-height:", menu)

    def test_alembic_0030_revision_chain(self):
        mig = (
            ROOT / "alembic/versions/0030_legacy_wallet_isolation_repair.py"
        ).read_text(encoding="utf-8")
        self.assertIn('down_revision: str = "0029_shop_wallets_isolation"', mig)
        self.assertIn("repair_legacy_wallet_isolation_sync", mig)


class WalletIsolationRepairTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import Base
        import app.db.models  # noqa: F401

        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "repair.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        event.listen(self.engine.sync_engine, "connect", _enable_sqlite_fk)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self._tmpdir.cleanup()

    async def _user(self, session, tg: int, *, reseller_id=None, wallet=0):
        from app.db.models import BotUser, Role

        u = BotUser(
            telegram_id=tg,
            role=Role.USER.value,
            wallet_balance=wallet,
            reseller_id=reseller_id,
            referral_code=f"U{tg}",
        )
        session.add(u)
        await session.flush()
        return u

    async def test_n2_claws_shop_gift_leaves_platform_gift(self):
        from app.db.models import ChargeCode, ShopWallet, WalletTransaction
        from app.services.wallet_isolation_migrate import repair_legacy_wallet_isolation

        async with self.Session() as session:
            owner = await self._user(session, 100, wallet=0)
            customer = await self._user(session, 200, reseller_id=owner.id, wallet=15_000)
            platform_user = await self._user(session, 300, wallet=8_000)
            session.add(
                ChargeCode(
                    code="SHOPGIFT",
                    amount=10_000,
                    max_uses=1,
                    used_count=1,
                    is_active=True,
                    reseller_id=owner.id,
                )
            )
            session.add(
                ChargeCode(
                    code="PLATGIFT",
                    amount=5_000,
                    max_uses=1,
                    used_count=1,
                    is_active=True,
                    reseller_id=None,
                )
            )
            session.add(
                WalletTransaction(
                    user_id=customer.id,
                    amount=10_000,
                    balance_after=15_000,
                    reason="کد هدیه SHOPGIFT",
                    reseller_id=None,
                )
            )
            session.add(
                WalletTransaction(
                    user_id=platform_user.id,
                    amount=5_000,
                    balance_after=8_000,
                    reason="کد هدیه PLATGIFT",
                    reseller_id=None,
                )
            )
            # Oversized shop code must be deactivated.
            session.add(
                ChargeCode(
                    code="HUGE",
                    amount=60_000_000,
                    max_uses=None,
                    used_count=0,
                    is_active=True,
                    reseller_id=owner.id,
                )
            )
            await session.commit()

            stats = await repair_legacy_wallet_isolation(session)
            await session.commit()
            await session.refresh(customer)
            await session.refresh(platform_user)
            huge = (
                await session.execute(
                    select(ChargeCode).where(ChargeCode.code == "HUGE")
                )
            ).scalar_one()
            self.assertFalse(huge.is_active)
            self.assertEqual(int(customer.wallet_balance), 5_000)
            self.assertEqual(int(platform_user.wallet_balance), 8_000)
            self.assertGreaterEqual(stats["clawback"]["clawed"], 1)
            shop_bal = (
                await session.execute(
                    select(ShopWallet.balance).where(
                        ShopWallet.user_id == customer.id,
                        ShopWallet.reseller_id == owner.id,
                    )
                )
            ).scalar_one_or_none()
            self.assertIn(shop_bal, (None, 0))

    async def test_n3_moves_shop_topup_to_shop_wallet(self):
        from app.db.models import Payment, PaymentMethod, PaymentStatus, ShopWallet, WalletTransaction
        from app.services.wallet_isolation_migrate import repair_legacy_wallet_isolation

        async with self.Session() as session:
            owner = await self._user(session, 110, wallet=0)
            customer = await self._user(session, 210, reseller_id=owner.id, wallet=20_000)
            pay = Payment(
                order_id=None,
                user_id=customer.id,
                amount=20_000,
                method=PaymentMethod.CARD.value,
                status=PaymentStatus.APPROVED.value,
                is_wallet_topup=True,
                wallet_shop_id=None,
            )
            session.add(pay)
            await session.flush()
            session.add(
                WalletTransaction(
                    user_id=customer.id,
                    amount=20_000,
                    balance_after=20_000,
                    reason=f"شارژ کیف پول #{pay.id}",
                    reseller_id=None,
                )
            )
            await session.commit()

            stats = await repair_legacy_wallet_isolation(session)
            await session.commit()
            await session.refresh(customer)
            self.assertEqual(int(customer.wallet_balance), 0)
            shop_bal = (
                await session.execute(
                    select(ShopWallet.balance).where(
                        ShopWallet.user_id == customer.id,
                        ShopWallet.reseller_id == owner.id,
                    )
                )
            ).scalar_one()
            self.assertEqual(int(shop_bal), 20_000)
            self.assertGreaterEqual(stats["migrate"]["moved"], 1)

    async def test_repair_is_idempotent(self):
        from app.db.models import ChargeCode, WalletTransaction
        from app.services.wallet_isolation_migrate import repair_legacy_wallet_isolation

        async with self.Session() as session:
            owner = await self._user(session, 120, wallet=0)
            customer = await self._user(session, 220, reseller_id=owner.id, wallet=4_000)
            session.add(
                ChargeCode(
                    code="ONCE",
                    amount=4_000,
                    max_uses=1,
                    used_count=1,
                    is_active=True,
                    reseller_id=owner.id,
                )
            )
            session.add(
                WalletTransaction(
                    user_id=customer.id,
                    amount=4_000,
                    balance_after=4_000,
                    reason="کد هدیه ONCE",
                    reseller_id=None,
                )
            )
            await session.commit()
            await repair_legacy_wallet_isolation(session)
            await session.commit()
            await session.refresh(customer)
            bal1 = int(customer.wallet_balance)
            s2 = await repair_legacy_wallet_isolation(session)
            await session.commit()
            await session.refresh(customer)
            self.assertEqual(int(customer.wallet_balance), bal1)
            self.assertEqual(s2["clawback"]["clawed"], 0)


class ImportShopBundleAllowlistTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import Base
        import app.db.models  # noqa: F401

        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "import.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self._tmpdir.cleanup()

    async def test_reseller_import_drops_platform_only_keys(self):
        from app.db.models import BotUser, ResellerSetting, Role
        from app.services.ux20 import import_shop_bundle

        async with self.Session() as session:
            owner = BotUser(
                telegram_id=1, role=Role.RESELLER.value, referral_code="OWN1"
            )
            session.add(owner)
            await session.flush()
            staff = {
                "role": "reseller",
                "pg_access": {"allowed_template_ids": [], "allowed_group_ids": []},
            }
            with patch(
                "app.services.ux20._validate_import_plan",
                new=AsyncMock(return_value=None),
            ):
                stats = await import_shop_bundle(
                    session,
                    {
                        "format": "pgclock-shop-bundle",
                        "settings": {
                            "welcome_text": "hi",
                            "billing_enabled": "1",
                            "backup_schedule_enabled": "1",
                        },
                        "plans": [],
                    },
                    reseller_id=owner.id,
                    staff=staff,
                )
            rows = {
                r.key: r.value
                for r in (
                    await session.execute(
                        select(ResellerSetting).where(
                            ResellerSetting.reseller_user_id == owner.id
                        )
                    )
                ).scalars()
            }
            self.assertIn("welcome_text", rows)
            self.assertNotIn("billing_enabled", rows)
            self.assertNotIn("backup_schedule_enabled", rows)
            self.assertEqual(stats["settings"], 1)


class BulkOrderPaymentsPermTests(unittest.IsolatedAsyncioTestCase):
    async def test_approve_pending_without_payments_fails(self):
        from app.db.models import (
            BotUser,
            Order,
            OrderStatus,
            Payment,
            PaymentMethod,
            PaymentStatus,
            Plan,
            Role,
        )
        from app.services.table_bulk import bulk_order_action

        tmp = tempfile.TemporaryDirectory()
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{Path(tmp.name) / 'bulk.db'}"
        )
        from app.db import Base
        import app.db.models  # noqa: F401

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as session:
                user = BotUser(
                    telegram_id=9, role=Role.USER.value, referral_code="U9"
                )
                session.add(user)
                await session.flush()
                plan = Plan(
                    name="p",
                    price=1000,
                    duration_days=30,
                    is_active=True,
                    is_trial=False,
                )
                session.add(plan)
                await session.flush()
                order = Order(
                    user_id=user.id,
                    plan_id=plan.id,
                    amount=1000,
                    status=OrderStatus.AWAITING_APPROVAL.value,
                )
                session.add(order)
                await session.flush()
                session.add(
                    Payment(
                        order_id=order.id,
                        user_id=user.id,
                        amount=1000,
                        method=PaymentMethod.CARD.value,
                        status=PaymentStatus.PENDING.value,
                    )
                )
                await session.commit()
                staff = {
                    "role": "reseller",
                    "permissions": ["orders"],
                    "shop_owner_id": None,
                }
                with patch(
                    "app.services.shop_scope.assert_order_in_scope",
                    return_value=None,
                ):
                    ok, fail = await bulk_order_action(
                        session, staff, [order.id], "approve"
                    )
                self.assertEqual(ok, 0)
                self.assertEqual(fail, 1)
        finally:
            await engine.dispose()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
