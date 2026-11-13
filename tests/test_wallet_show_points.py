"""Customer wallet screens show this shop's loyalty points only while the club is on."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.handlers.wallet import build_wallet_home_text, load_wallet_home
from app.bot.nav_inline import wallet_hub_keyboard
from app.db import Base
from app.db.models import BotUser
from app.services.formatting import format_number
from app.services.loyalty import credit_points
from app.services.users import (
    DEFAULT_SETTINGS,
    reset_shop_reseller_id,
    set_setting,
    set_shop_reseller_id,
)


def _ui(**overrides: str) -> dict:
    ui = dict(DEFAULT_SETTINGS)
    ui.update(overrides)
    return ui


class WalletHomePointsTextTests(unittest.IsolatedAsyncioTestCase):
    async def _text(self, *, loyalty_flag: str, points: int, shop_id: int | None) -> str:
        session = AsyncMock()
        user = SimpleNamespace(id=7)
        with (
            patch("app.services.wallet.wallet_balance_for_context", AsyncMock(return_value=50_000)),
            patch("app.services.loyalty.points_balance", AsyncMock(return_value=points)) as balance,
            patch("app.services.users.current_shop_reseller_id", return_value=shop_id),
            patch("app.config.get_settings") as gs,
        ):
            gs.return_value.currency = "تومان"
            text = await build_wallet_home_text(
                session, user, _ui(loyalty_enabled=loyalty_flag)
            )
        if loyalty_flag in {"1", "true", "yes", "on", "True"}:
            balance.assert_awaited_once_with(session, user, shop_id)
        else:
            balance.assert_not_awaited()
        return text

    async def test_points_line_appears_when_loyalty_is_on(self) -> None:
        text = await self._text(loyalty_flag="1", points=1234, shop_id=None)
        self.assertIn("موجودی", text)
        self.assertIn("امتیاز", text)
        self.assertIn(format_number(1234), text)

    async def test_points_line_omitted_when_loyalty_is_off(self) -> None:
        text = await self._text(loyalty_flag="0", points=999, shop_id=None)
        self.assertIn("موجودی", text)
        self.assertNotIn("امتیاز", text)
        self.assertNotIn(format_number(999), text)

    async def test_shop_bot_reads_that_shop_points_purse(self) -> None:
        text = await self._text(loyalty_flag="1", points=42, shop_id=5)
        self.assertIn("امتیاز", text)
        self.assertIn(format_number(42), text)


class WalletHubLoyaltyShortcutTests(unittest.TestCase):
    def test_wallet_hub_has_no_club_shortcut_even_when_loyalty_enabled(self) -> None:
        for flag in ("1", "0"):
            data = [
                b.callback_data
                for row in wallet_hub_keyboard(_ui(loyalty_enabled=flag)).inline_keyboard
                for b in row
            ]
            self.assertNotIn("nv:loy:home", data)
            self.assertIn("nv:w:topup", data)
            self.assertIn("menu:home", data)


class WalletHomeShopSettingsParityTests(unittest.IsolatedAsyncioTestCase):
    """Text line and club button must share one shop-scoped settings source."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 'w.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all(
                [
                    BotUser(id=1, telegram_id=101, referral_code="u1", role="user", wallet_balance=10_000),
                    BotUser(id=3, telegram_id=103, referral_code="shop", role="reseller"),
                ]
            )
            await session.commit()
        self.token = set_shop_reseller_id(3)

    async def asyncTearDown(self) -> None:
        reset_shop_reseller_id(self.token)
        await self.engine.dispose()
        self._tmp.cleanup()

    async def _home(self) -> tuple[str, list[str]]:
        async with self.Session() as session:
            user = await session.get(BotUser, 1)
            with patch("app.config.get_settings") as gs:
                gs.return_value.currency = "تومان"
                text, markup = await load_wallet_home(session, user)
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        return text, callbacks

    async def test_platform_on_shop_off_hides_points_and_club(self) -> None:
        async with self.Session() as session:
            await set_setting(session, "loyalty_enabled", "1", reseller_id=None)
            await set_setting(session, "loyalty_enabled", "0", reseller_id=3)
            await session.commit()
        text, callbacks = await self._home()
        self.assertNotIn("امتیاز", text)
        self.assertNotIn("nv:loy:home", callbacks)

    async def test_platform_off_shop_on_shows_points_and_club(self) -> None:
        async with self.Session() as session:
            await set_setting(session, "loyalty_enabled", "0", reseller_id=None)
            await set_setting(session, "loyalty_enabled", "1", reseller_id=3)
            user = await session.get(BotUser, 1)
            await credit_points(
                session,
                user,
                88,
                tx_type="earn",
                source="test",
                reference=None,
                description="seed",
                idempotency_key="seed-shop-parity",
                shop_id=3,
            )
            await session.commit()
        text, callbacks = await self._home()
        self.assertIn("امتیاز", text)
        self.assertIn(format_number(88), text)
        self.assertIn("nv:loy:home", callbacks)


class MiniappCustomerSummaryPointsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 'w.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all(
                [
                    BotUser(id=1, telegram_id=101, referral_code="u1", role="user", wallet_balance=10_000),
                    BotUser(id=3, telegram_id=103, referral_code="shop", role="reseller"),
                ]
            )
            await session.commit()
        self.token = set_shop_reseller_id(None)

    async def asyncTearDown(self) -> None:
        reset_shop_reseller_id(self.token)
        await self.engine.dispose()
        self._tmp.cleanup()

    async def _summary(self, *, shop_id: int | None):
        from app.api.miniapp_pages import _customer_summary
        from app.services.miniapp_shop import PLATFORM_SCOPE, MiniScope

        scope = PLATFORM_SCOPE if shop_id is None else MiniScope(shop_id=shop_id)
        async with self.Session() as session:
            user = await session.get(BotUser, 1)
            with (
                patch("app.api.miniapp_pages.customer_home_summary", AsyncMock(return_value=SimpleNamespace(payload=lambda: {"service_count": 0, "attention": [], "recent": []}))),
                patch("app.api.miniapp_pages.home_badges", AsyncMock(return_value=SimpleNamespace(payload=lambda: {}))),
                patch("app.api.miniapp_pages.checkout_options", AsyncMock(return_value={})),
                patch("app.api.miniapp_pages.get_wallet_balance", AsyncMock(return_value=10_000)),
            ):
                return await _customer_summary(session, user, scope=scope)

    async def test_points_included_when_loyalty_enabled(self) -> None:
        async with self.Session() as session:
            await set_setting(session, "loyalty_enabled", "1", reseller_id=None)
            user = await session.get(BotUser, 1)
            await credit_points(
                session,
                user,
                1500,
                tx_type="earn",
                source="test",
                reference=None,
                description="seed",
                idempotency_key="seed-platform",
                shop_id=None,
            )
            await session.commit()
        summary = await self._summary(shop_id=None)
        self.assertTrue(summary.get("loyalty_enabled"))
        self.assertEqual(summary.get("points"), 1500)

    async def test_points_omitted_when_loyalty_disabled(self) -> None:
        async with self.Session() as session:
            await set_setting(session, "loyalty_enabled", "0", reseller_id=None)
            user = await session.get(BotUser, 1)
            user.points_balance = 900
            await session.commit()
        summary = await self._summary(shop_id=None)
        self.assertNotIn("loyalty_enabled", summary)
        self.assertNotIn("points", summary)

    async def test_shop_scope_returns_only_that_shop_purse(self) -> None:
        async with self.Session() as session:
            await set_setting(session, "loyalty_enabled", "1", reseller_id=3)
            user = await session.get(BotUser, 1)
            user.points_balance = 777
            await credit_points(
                session,
                user,
                55,
                tx_type="earn",
                source="test",
                reference=None,
                description="shop seed",
                idempotency_key="seed-shop",
                shop_id=3,
            )
            await session.commit()
        summary = await self._summary(shop_id=3)
        self.assertTrue(summary.get("loyalty_enabled"))
        self.assertEqual(summary.get("points"), 55)
        self.assertNotEqual(summary.get("points"), 777)

    async def test_miniapp_uses_ui_loyalty_flag_not_extra_query(self) -> None:
        async with self.Session() as session:
            await set_setting(session, "loyalty_enabled", "1", reseller_id=None)
            await session.commit()
        with patch("app.services.loyalty.loyalty_enabled", AsyncMock(return_value=False)) as enabled:
            summary = await self._summary(shop_id=None)
        enabled.assert_not_awaited()
        self.assertTrue(summary.get("loyalty_enabled"))


if __name__ == "__main__":
    unittest.main()
