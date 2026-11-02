"""Settings + plans JSON import is all or nothing and never breaks sold services."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("WEB_SECRET", "x" * 48)

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.db.models import BotUser, Order, Plan
from app.services.users import get_setting, set_setting
from app.services.ux20 import export_shop_bundle, import_shop_bundle


def _bundle(*plans: dict, **settings: str) -> dict:
    return {"format": "pgclock-shop-bundle", "settings": settings, "plans": list(plans)}


class ShopBundleImportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 't.db'}")

        @event.listens_for(self.engine.sync_engine, "connect")
        def _foreign_keys(dbapi_conn, _record) -> None:
            # Same enforcement as the production session.
            dbapi_conn.execute("PRAGMA foreign_keys=ON")

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            sold = Plan(name="Sold", price=100, duration_days=30, is_active=True)
            unused = Plan(name="Unused", price=50, duration_days=30, is_active=True)
            customer = BotUser(telegram_id=7, referral_code="C7")
            session.add_all([sold, unused, customer])
            await session.flush()
            session.add(Order(user_id=customer.id, plan_id=sold.id, amount=100, status="delivered"))
            await set_setting(session, "welcome_text", "old")
            await session.commit()
            self.sold_id, self.unused_id = sold.id, unused.id

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self._tmp.cleanup()

    async def _plans(self) -> dict[str, Plan]:
        async with self.Session() as session:
            return {plan.name: plan for plan in (await session.execute(select(Plan))).scalars()}

    async def _welcome(self) -> str:
        async with self.Session() as session:
            return await get_setting(session, "welcome_text", "")

    async def test_replace_keeps_sold_plans_for_their_orders(self) -> None:
        async with self.Session() as session:
            stats = await import_shop_bundle(
                session, _bundle({"name": "New", "price": 200, "duration_days": 60}, welcome_text="new"),
                replace_plans=True,
            )
        self.assertEqual((stats["plans"], stats["deleted"], stats["deactivated"]), (1, 1, 1))
        plans = await self._plans()
        self.assertFalse(plans["Sold"].is_active)
        self.assertNotIn("Unused", plans)
        self.assertTrue(plans["New"].is_active)
        async with self.Session() as session:
            order = (await session.execute(select(Order))).scalar_one()
        self.assertEqual(order.plan_id, self.sold_id)
        self.assertEqual(await self._welcome(), "new")

    async def test_invalid_plan_changes_nothing(self) -> None:
        bundle = _bundle({"name": "Good", "price": 10}, {"name": "Bad", "price": -5}, welcome_text="new")
        async with self.Session() as session:
            with self.assertRaises(ValueError):
                await import_shop_bundle(session, bundle, replace_plans=True)
        self.assertEqual(await self._welcome(), "old")
        self.assertEqual(set(await self._plans()), {"Sold", "Unused"})

    async def test_numbers_are_validated(self) -> None:
        for bad in (
            {"price": True}, {"price": "abc"}, {"price": 1.5}, {"price": 10**12},
            {"duration_days": -1}, {"duration_days": 99_999}, {"data_limit_gb": float("nan")},
            {"data_limit_gb": -2}, {"sort_order": "x"},
        ):
            with self.subTest(bad=bad):
                async with self.Session() as session:
                    with self.assertRaises(ValueError):
                        await import_shop_bundle(session, _bundle({"name": "P", **bad}))

    async def test_text_flags_are_read_as_intended(self) -> None:
        async with self.Session() as session:
            await import_shop_bundle(session, _bundle({"name": "Off", "price": "0", "is_active": "0"}))
        self.assertFalse((await self._plans())["Off"].is_active)

    async def test_plans_list_must_be_a_list(self) -> None:
        async with self.Session() as session:
            with self.assertRaises(ValueError):
                await import_shop_bundle(session, {"format": "pgclock-shop-bundle", "plans": {"a": 1}})

    async def test_export_leaves_out_credentials(self) -> None:
        async with self.Session() as session:
            for key, value in (
                ("bbc_api_key", "k"), ("card_auto_webhook_secret", "s"),
                ("psp_merchant_id", "m"), ("welcome_text", "hi"),
            ):
                await set_setting(session, key, value)
            await session.commit()
            settings = (await export_shop_bundle(session))["settings"]
        self.assertEqual(settings.get("welcome_text"), "hi")
        for key in ("bbc_api_key", "card_auto_webhook_secret", "psp_merchant_id"):
            self.assertNotIn(key, settings)
        self.assertFalse([key for key in settings if key.endswith(("_token", "_secret", "_api_key", "_password"))])


if __name__ == "__main__":
    unittest.main()
