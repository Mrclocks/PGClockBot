"""Shop staff change a customer's shop wallet by hand: own shop's purse only, capped, once per request."""

from __future__ import annotations

import asyncio
import importlib.util
import secrets
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.shop_wallet_adjust as adjust_module
from app.services.shop_wallet_adjust import (
    ShopWalletAdjustError,
    adjust_shop_wallet,
    parse_request,
    shop_wallet_form_context,
    wallet_adjust_shop,
)

ROOT = Path(__file__).resolve().parents[1]
SHOP_A = 81
SHOP_B = 82
STAFF_TG = 990_001
OWNER = {
    "role": "admin", "id": 1, "username": "root", "org_principal_id": 1,
    "org_depth": 0, "org_parent_id": None, "org_status": "active",
}


def _reseller(shop_id: int, perms: list[str] | None = None) -> dict:
    return {
        "role": "reseller", "bot_user_id": shop_id, "id": shop_id, "username": f"shop{shop_id}",
        "permissions": ["wallet_adjust"] if perms is None else perms,
    }


def _key() -> str:
    return secrets.token_hex(16)


def _request(customer_id: int, amount: int = 10_000, direction: str = "credit", key: str | None = None):
    return parse_request(
        customer_id=customer_id, raw_amount=str(amount), direction=direction, reason="refund for outage", request_key=key or _key()
    )


class PermissionAndInputTests(unittest.TestCase):
    def test_only_shop_staff_with_the_key_may_adjust(self):
        self.assertEqual(wallet_adjust_shop(_reseller(SHOP_A)), SHOP_A)
        for staff in (
            _reseller(SHOP_A, ["dashboard", "payments", "orders"]),
            OWNER,
            {"role": "pg_staff", "permissions": ["wallet_adjust"]},
            _reseller(0),
        ):
            with self.subTest(staff=staff), self.assertRaises(PermissionError):
                wallet_adjust_shop(staff)

    def test_form_is_offered_only_to_staff_who_may_use_it(self):
        self.assertTrue(shop_wallet_form_context(_reseller(SHOP_A))["can_adjust_shop_wallet"])
        self.assertEqual(shop_wallet_form_context(_reseller(SHOP_A, ["orders"])), {"can_adjust_shop_wallet": False})
        self.assertEqual(shop_wallet_form_context(OWNER), {"can_adjust_shop_wallet": False})

    def test_bad_values_are_refused(self):
        good = {"customer_id": 1, "raw_amount": "5000", "direction": "credit", "reason": "ok reason", "request_key": _key()}
        self.assertEqual(parse_request(**good).amount, 5000)
        self.assertEqual(parse_request(**{**good, "raw_amount": "50,000"}).amount, 50_000)
        for field, value in (
            ("raw_amount", ""), ("raw_amount", "-5000"), ("raw_amount", "1.5"), ("raw_amount", "abc"),
            ("raw_amount", "999"), ("raw_amount", str(adjust_module.ADJUST_MAX + 1)), ("raw_amount", True),
            ("raw_amount", "9" * 40), ("direction", "set"), ("direction", None), ("reason", "  "),
            ("reason", "ab"), ("request_key", ""), ("request_key", "short"), ("request_key", "zz" * 16),
        ):
            with self.subTest(field=field, value=value), self.assertRaises(ShopWalletAdjustError):
                parse_request(**{**good, field: value})


class WalletDbHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import Base
        from app.db.models import BotUser, BotUserShop, ResellerProfile

        self._tmpdir = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{Path(self._tmpdir.name) / 'wallet.db'}", connect_args={"timeout": 15}
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            for shop in (SHOP_A, SHOP_B):
                session.add(BotUser(id=shop, telegram_id=shop * 10, referral_code=f"s{shop}", role="reseller"))
            await session.flush()
            session.add(ResellerProfile(user_id=SHOP_A, is_active=True, bot_admin_ids=str(STAFF_TG)))
            session.add(ResellerProfile(user_id=SHOP_B, is_active=True))
            session.add_all([
                BotUser(id=101, telegram_id=1010, referral_code="c101", reseller_id=SHOP_A, wallet_balance=700),
                BotUser(id=102, telegram_id=1020, referral_code="c102", reseller_id=SHOP_B),
                BotUser(id=103, telegram_id=1030, referral_code="c103", reseller_id=SHOP_B),
                BotUser(id=104, telegram_id=STAFF_TG, referral_code="c104", reseller_id=SHOP_A),
                BotUser(id=105, telegram_id=1050, referral_code="c105", reseller_id=None),
            ])
            await session.flush()
            session.add(BotUserShop(bot_user_id=103, shop_id=SHOP_A))
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        self._tmpdir.cleanup()

    async def _purse(self, user_id: int, shop_id: int) -> int:
        from app.services.wallet import get_wallet_balance

        async with self.Session() as session:
            return await get_wallet_balance(session, user_id, shop_id=shop_id)

    async def _platform_balance(self, user_id: int) -> int:
        from app.db.models import BotUser

        async with self.Session() as session:
            return int((await session.get(BotUser, user_id)).wallet_balance or 0)

    async def _count(self, model) -> int:
        async with self.Session() as session:
            return int((await session.execute(select(func.count()).select_from(model))).scalar_one())

    async def _used_today(self, shop_id: int) -> int:
        from app.db.models import ShopWalletAdjustDay

        async with self.Session() as session:
            value = (await session.execute(select(ShopWalletAdjustDay.used).where(ShopWalletAdjustDay.shop_id == shop_id))).scalar()
            return int(value or 0)

    async def _adjust(self, staff: dict, request):
        async with self.Session() as session:
            return await adjust_shop_wallet(session, staff=staff, request=request)


class ShopWalletAdjustTests(WalletDbHarness):
    async def test_credit_moves_only_this_shops_purse_and_is_recorded(self):
        from app.db.models import ShopStaffAction, WalletTransaction

        change = await self._adjust(_reseller(SHOP_A), _request(101, 25_000))
        self.assertEqual((change.balance_after, change.replayed), (25_000, False))
        self.assertEqual(await self._purse(101, SHOP_A), 25_000)
        self.assertEqual(await self._purse(101, SHOP_B), 0)
        self.assertEqual(await self._platform_balance(101), 700)
        async with self.Session() as session:
            record = (await session.execute(select(ShopStaffAction))).scalar_one()
            ledger = (await session.execute(select(WalletTransaction))).scalar_one()
        self.assertEqual((record.shop_id, record.action, record.target_id, record.amount), (SHOP_A, "wallet_credit", 101, 25_000))
        self.assertEqual((ledger.reseller_id, ledger.amount), (SHOP_A, 25_000))
        self.assertEqual(await self._used_today(SHOP_A), 25_000)

    async def test_a_repeated_submit_moves_money_once(self):
        request = _request(101, 10_000)
        first = await self._adjust(_reseller(SHOP_A), request)
        second = await self._adjust(_reseller(SHOP_A), request)
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(await self._purse(101, SHOP_A), 10_000)
        self.assertEqual(await self._used_today(SHOP_A), 10_000)

    async def test_a_key_reused_for_another_change_is_refused(self):
        key = _key()
        await self._adjust(_reseller(SHOP_A), _request(101, 10_000, key=key))
        for request in (_request(101, 20_000, key=key), _request(101, 10_000, "debit", key=key), _request(103, 10_000, key=key)):
            with self.subTest(request=request), self.assertRaises(ShopWalletAdjustError):
                await self._adjust(_reseller(SHOP_A), request)
        with self.assertRaises(ShopWalletAdjustError):
            await self._adjust(_reseller(SHOP_B, ["wallet_adjust"]), _request(102, 10_000, key=key))
        self.assertEqual(await self._purse(101, SHOP_A), 10_000)

    async def test_concurrent_submits_of_one_request_move_money_once(self):
        request = _request(101, 10_000)
        results = await asyncio.gather(
            *(self._adjust(_reseller(SHOP_A), request) for _ in range(4)), return_exceptions=True
        )
        self.assertEqual(await self._purse(101, SHOP_A), 10_000)
        self.assertEqual(await self._used_today(SHOP_A), 10_000)
        self.assertEqual(sum(1 for r in results if not isinstance(r, Exception) and not r.replayed), 1)

    async def test_daily_cap_holds_under_concurrency(self):
        with mock.patch.object(adjust_module, "DAILY_CAP", 30_000):
            results = await asyncio.gather(
                *(self._adjust(_reseller(SHOP_A), _request(101, 10_000)) for _ in range(5)), return_exceptions=True
            )
        succeeded = [r for r in results if not isinstance(r, Exception)]
        self.assertEqual(len(succeeded), 3)
        self.assertTrue(all(isinstance(r, ShopWalletAdjustError) for r in results if isinstance(r, Exception)))
        self.assertEqual(await self._purse(101, SHOP_A), 30_000)
        self.assertEqual(await self._used_today(SHOP_A), 30_000)

    async def test_cap_is_per_shop(self):
        with mock.patch.object(adjust_module, "DAILY_CAP", 10_000):
            await self._adjust(_reseller(SHOP_A), _request(101, 10_000))
            await self._adjust(_reseller(SHOP_B), _request(102, 10_000))
            with self.assertRaises(ShopWalletAdjustError):
                await self._adjust(_reseller(SHOP_A), _request(101, 1_000))

    async def test_a_debit_over_the_balance_changes_nothing(self):
        from app.db.models import ShopStaffAction

        await self._adjust(_reseller(SHOP_A), _request(101, 5_000))
        with self.assertRaises(ShopWalletAdjustError):
            await self._adjust(_reseller(SHOP_A), _request(101, 6_000, "debit"))
        self.assertEqual(await self._purse(101, SHOP_A), 5_000)
        self.assertEqual(await self._used_today(SHOP_A), 5_000)
        self.assertEqual(await self._count(ShopStaffAction), 1)
        change = await self._adjust(_reseller(SHOP_A), _request(101, 5_000, "debit"))
        self.assertEqual(change.balance_after, 0)

    async def test_other_shops_platform_and_staff_targets_are_refused(self):
        from app.db.models import ShopStaffAction, WalletTransaction

        for customer_id in (102, 105, SHOP_A, 104, 99_999):
            with self.subTest(customer_id=customer_id), self.assertRaises(ShopWalletAdjustError):
                await self._adjust(_reseller(SHOP_A), _request(customer_id))
        self.assertEqual(await self._count(WalletTransaction), 0)
        self.assertEqual(await self._count(ShopStaffAction), 0)
        self.assertEqual(await self._used_today(SHOP_A), 0)

    async def test_a_customer_of_several_shops_is_changed_only_in_the_acting_shop(self):
        await self._adjust(_reseller(SHOP_A), _request(103, 10_000))
        self.assertEqual(await self._purse(103, SHOP_A), 10_000)
        self.assertEqual(await self._purse(103, SHOP_B), 0)

    async def test_staff_without_the_key_cannot_adjust(self):
        with self.assertRaises(PermissionError):
            await self._adjust(_reseller(SHOP_A, ["orders", "payments"]), _request(101))
        self.assertEqual(await self._purse(101, SHOP_A), 0)


class ShopWalletRouteTests(WalletDbHarness):
    """The route is thin: permission first, then the service; errors become a safe message."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        from app.api.shop_wallet_pages import register_shop_wallet_pages

        self.staff = _reseller(SHOP_A)

        async def get_db():
            async with self.Session() as session:
                yield session

        app = FastAPI()
        register_shop_wallet_pages(app, require_staff=lambda: self.staff, get_db=get_db)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        await super().asyncTearDown()

    async def _post(self, user_id: int, **data):
        form = {"amount": "10000", "mode": "credit", "note": "goodwill", "nonce": _key(), **data}
        return await self.client.post(f"/users/{user_id}/shop-wallet", data=form)

    async def test_route_credits_and_reports(self):
        response = await self._post(101)
        self.assertIn("ok=", response.headers["location"])
        self.assertEqual(await self._purse(101, SHOP_A), 10_000)

    async def test_route_without_the_key_goes_home(self):
        self.staff = _reseller(SHOP_A, ["orders"])
        response = await self._post(101)
        self.assertEqual(response.headers["location"], "/home")
        self.assertEqual(await self._purse(101, SHOP_A), 0)

    async def test_route_shows_a_safe_error(self):
        response = await self._post(102)
        self.assertIn("err=", response.headers["location"])
        response = await self._post(101, amount="1e9")
        self.assertIn("err=", response.headers["location"])
        self.assertEqual(await self._purse(102, SHOP_B), 0)


class ShopWalletMigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        spec = importlib.util.spec_from_file_location("wallet_days", ROOT / "alembic/versions/0079_shop_wallet_adjust_days.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            migration.upgrade()
            conn.execute(sa.text("INSERT INTO shop_wallet_adjust_days (shop_id, day, used) VALUES (1, '2026-01-01', 5)"))
            with self.assertRaises(sa.exc.IntegrityError):
                conn.execute(sa.text("INSERT INTO shop_wallet_adjust_days (shop_id, day, used) VALUES (1, '2026-01-01', 1)"))
        with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
            migration.downgrade()
            self.assertNotIn("shop_wallet_adjust_days", sa.inspect(conn).get_table_names())
        engine.dispose()


class ShopWalletTemplateTests(unittest.TestCase):
    def _render(self, ctx: dict) -> str:
        from types import SimpleNamespace

        from app.api.app import templates
        from app.services.color_tags import color_tag_meta, color_tags_for_ui

        user = SimpleNamespace(
            id=7, telegram_id=7007, username="c", full_name="C", is_blocked=False, is_demo=False,
            role="user", color_tag=None, staff_note=None, wallet_balance=0,
        )
        return templates.env.get_template("_user_edit_body.html").render(
            user=user, staff={}, show_private_marks=False, can_manage_users=False, display_wallet=0,
            services=[], wallet_txs=[], plans=[], color_tags=color_tags_for_ui(), color_tag_meta=color_tag_meta,
            format_toman=str, wallet_nonce="n", **ctx,
        )

    def test_shop_form_only_with_permission_and_never_the_platform_form(self):
        with_key = self._render(shop_wallet_form_context(_reseller(SHOP_A)))
        self.assertIn("/users/7/shop-wallet", with_key)
        self.assertIn('name="nonce"', with_key)
        self.assertNotIn("/users/7/wallet-credit", with_key)
        without = self._render(shop_wallet_form_context(_reseller(SHOP_A, ["orders"])))
        self.assertNotIn("/users/7/shop-wallet", without)


if __name__ == "__main__":
    unittest.main()
