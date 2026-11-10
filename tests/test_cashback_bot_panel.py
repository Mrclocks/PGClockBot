"""Cashback light panel in the bot: shop scoping, Owner/shop gates, idempotent toggles."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot import marketing_panels as panels
from app.bot.handlers import cashback_tools
from app.db.models import BotUser, CashbackRule, Role
from app.services.cashback_rules import CashbackSettings, list_rules, load_settings, set_rule_enabled


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


class CashbackPanelBuilderTests(unittest.TestCase):
    def _rule(self, **kw):
        base = dict(
            id=1,
            name="Spring",
            enabled=True,
            mode="percent",
            percent_bps=500,
            fixed_amount=0,
        )
        base.update(kw)
        return SimpleNamespace(**base)

    def test_feature_and_rule_buttons_target_the_opposite_state(self):
        settings = CashbackSettings(enabled=True, combine="best", hold_hours=24, monthly_budget=0)
        on = self._rule(id=1, enabled=True)
        off = self._rule(id=2, name="Off", enabled=False)
        _, markup = panels.cashback_panel(settings, [on, off])
        data = _callbacks(markup)
        self.assertIn("adm:cb:en:0", data)
        self.assertIn("adm:cb:set:1:0", data)
        self.assertIn("adm:cb:set:2:1", data)

    def test_hostile_rule_name_is_escaped(self):
        settings = CashbackSettings(enabled=False, combine="best", hold_hours=24, monthly_budget=0)
        text, _ = panels.cashback_panel(settings, [self._rule(name="<b>x</b>")])
        self.assertNotIn("<b>x</b>", text)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", text)

    def test_shop_prefixes_and_web_footer(self):
        settings = CashbackSettings(enabled=False, combine="sum", hold_hours=12, monthly_budget=1000)
        _, markup = panels.cashback_panel(
            settings,
            [],
            web_url="https://panel.example/cashback",
            back_callback="nv:res:grp:shop",
            feature_prefix="res:cb",
            rule_prefix="res:cb",
        )
        data = _callbacks(markup)
        self.assertIn("res:cb:en:1", data)
        self.assertIn("nv:res:grp:shop", data)
        urls = [b.url for row in markup.inline_keyboard for b in row if b.url]
        self.assertEqual(urls, ["https://panel.example/cashback"])

    def test_patterns_reject_malformed_data(self):
        for bad in ("adm:cb:en:2", "adm:cb:set:1:2", "adm:cb:set:-1:1", "res:cb:en:", "res:cb:set:1:1:x"):
            self.assertTrue(
                panels.CASHBACK_FEATURE_PATTERN.match(bad) is None
                and panels.CASHBACK_RULE_PATTERN.match(bad) is None
                and panels.SHOP_CASHBACK_FEATURE_PATTERN.match(bad) is None
                and panels.SHOP_CASHBACK_RULE_PATTERN.match(bad) is None,
                bad,
            )


class CashbackDbCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import Base
        import app.db.models  # noqa: F401

        self._tmpdir = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmpdir.name) / 'c.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self._tmpdir.cleanup()


class CashbackRuleServiceTests(CashbackDbCase):
    async def test_platform_cannot_toggle_a_shop_rule(self):
        async with self.Session() as session:
            session.add(
                CashbackRule(
                    shop_id=5,
                    name="Shop",
                    enabled=True,
                    mode="fixed",
                    fixed_amount=1000,
                    events="new",
                )
            )
            await session.commit()
            rule_id = (await list_rules(session, 5))[0].id
            with self.assertRaises(ValueError):
                await set_rule_enabled(session, None, rule_id, False)
            await session.rollback()
            self.assertTrue((await session.get(CashbackRule, rule_id)).enabled)

    async def test_shop_cannot_toggle_platform_or_other_shop_rule(self):
        async with self.Session() as session:
            session.add_all(
                [
                    CashbackRule(
                        shop_id=None,
                        name="Plat",
                        enabled=True,
                        mode="fixed",
                        fixed_amount=1,
                        events="new",
                    ),
                    CashbackRule(
                        shop_id=5,
                        name="Shop5",
                        enabled=True,
                        mode="fixed",
                        fixed_amount=1,
                        events="new",
                    ),
                ]
            )
            await session.commit()
            plat = (await list_rules(session, None))[0].id
            shop = (await list_rules(session, 5))[0].id
            for shop_id, rule_id in ((5, plat), (6, shop)):
                with self.assertRaises(ValueError):
                    await set_rule_enabled(session, shop_id, rule_id, False)
                await session.rollback()
            self.assertTrue((await session.get(CashbackRule, plat)).enabled)
            self.assertTrue((await session.get(CashbackRule, shop)).enabled)


class OwnerCashbackHandlerTests(CashbackDbCase):
    async def test_owner_can_toggle_platform_feature_and_rule(self):
        async with self.Session() as session:
            session.add(
                CashbackRule(
                    shop_id=None,
                    name="Plat",
                    enabled=True,
                    mode="percent",
                    percent_bps=1000,
                    events="new,renew",
                )
            )
            await session.commit()
            rule_id = (await list_rules(session, None))[0].id
            message = AsyncMock()
            message.from_user = SimpleNamespace(is_bot=True)
            callback = SimpleNamespace(
                data=f"adm:cb:set:{rule_id}:0", message=message, answer=AsyncMock()
            )
            state = AsyncMock()
            state.get_data = AsyncMock(return_value={})
            with patch("app.bot.auth.is_bot_owner_principal", AsyncMock(return_value=True)):
                with patch.object(cashback_tools, "panel_page_url", return_value=None):
                    await cashback_tools.owner_set_rule(
                        callback, session=session, db_user=BotUser(telegram_id=1, role=Role.ADMIN.value), state=state
                    )
                    await cashback_tools.owner_set_feature(
                        SimpleNamespace(data="adm:cb:en:1", message=message, answer=AsyncMock()),
                        session=session,
                        db_user=BotUser(telegram_id=1, role=Role.ADMIN.value),
                        state=state,
                    )
            self.assertFalse((await session.get(CashbackRule, rule_id)).enabled)
            self.assertTrue((await load_settings(session, None)).enabled)
            message.edit_text.assert_awaited()


if __name__ == "__main__":
    unittest.main()
