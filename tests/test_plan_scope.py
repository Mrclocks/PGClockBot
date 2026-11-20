"""Plan scope of a customer feature: «all», or the shop's own chosen plans only."""

from __future__ import annotations

import unittest

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.plan_scope_picker import picker_markup
from app.db.models import Base, BotUser, Plan
from app.services.plan_scope import (
    ALL,
    PlanChoice,
    normalize_plan_scope,
    parse_plan_scope,
    plan_choices,
    scope_summary,
    toggle_plan_scope,
)
from tests.postgres_support import scoped_async_engine

SHOP_A, SHOP_B = 5, 6


class ParseTests(unittest.TestCase):
    def test_all_is_the_default_and_a_list_allows_only_its_plans(self) -> None:
        self.assertTrue(parse_plan_scope(None).allows(99))
        scope = parse_plan_scope("1, 3,x,")
        self.assertEqual((scope.allows(3), scope.allows(2), scope.allows(None)), (True, False, False))
        self.assertFalse(parse_plan_scope("").allows(1))

    def test_toggling_all_and_single_plans(self) -> None:
        own = [1, 2, 3]
        self.assertEqual(toggle_plan_scope(ALL, "2", own), "1,3")
        self.assertEqual(toggle_plan_scope("1,3", "3", own), "1")
        self.assertEqual(toggle_plan_scope("1", ALL, own), ALL)
        self.assertEqual(toggle_plan_scope(ALL, ALL, own), "")
        with self.assertRaises(ValueError):
            toggle_plan_scope("1", "9", own)

    def test_summary_and_picker_marks(self) -> None:
        choices = [PlanChoice(1, "ماهانه"), PlanChoice(2, "سه‌ماهه")]
        self.assertEqual(scope_summary(ALL), "همهٔ پلن‌ها")
        self.assertEqual(scope_summary("1,2"), "۲ پلن")
        texts = [b.text for row in picker_markup("adm:st", "service_freeze_plans", "2", choices, "back").inline_keyboard for b in row]
        self.assertEqual(texts[:3], ["⬜️ همهٔ پلن‌ها", "⬜️ ماهانه", "✅ سه‌ماهه"])


class OwnPlansTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=SHOP_A, telegram_id=5, referral_code="a", role="reseller"),
                BotUser(id=SHOP_B, telegram_id=6, referral_code="b", role="reseller"),
            ])
            await session.flush()
            session.add_all([
                Plan(id=1, name="platform", price=1),
                Plan(id=2, name="a-month", price=1, owner_reseller_id=SHOP_A),
                Plan(id=3, name="a-trial", price=0, owner_reseller_id=SHOP_A, is_trial=True),
                Plan(id=4, name="b-month", price=1, owner_reseller_id=SHOP_B),
            ])
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_a_shop_lists_and_keeps_only_its_own_non_trial_plans(self) -> None:
        async with self.Session() as session:
            self.assertEqual([c.plan_id for c in await plan_choices(session, SHOP_A)], [2])
            self.assertEqual([c.plan_id for c in await plan_choices(session, None)], [1])
            self.assertEqual(await normalize_plan_scope(session, ["2", "3", "4", "1"], shop_id=SHOP_A), "2")
            self.assertEqual(await normalize_plan_scope(session, ["4", ALL], shop_id=SHOP_A), ALL)
            self.assertEqual(await normalize_plan_scope(session, [], shop_id=SHOP_A), "")


if __name__ == "__main__":
    unittest.main()


class ShopBotPickerTests(unittest.IsolatedAsyncioTestCase):
    async def tap(self, data: str):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.bot.handlers import reseller_settings as rs

        callback = MagicMock(data=data, answer=AsyncMock(), message=None)
        with (
            patch.object(rs, "_gate", AsyncMock(return_value=(SimpleNamespace(user_id=SHOP_A), None))),
            patch.object(rs, "plan_choices", AsyncMock(return_value=[PlanChoice(72, "شاپ")])),
            patch.object(rs, "get_setting", AsyncMock(return_value=ALL)),
            patch.object(rs, "set_setting", AsyncMock()) as saved,
        ):
            await rs.settings_plan_scope_toggle(callback, MagicMock(), SimpleNamespace(id=1))
        return saved, callback

    async def test_a_shop_toggles_its_own_plan_on_its_own_copy(self) -> None:
        saved, _ = await self.tap("res:st:pls:service_freeze_plans:72")
        self.assertEqual(saved.await_args.args[1:], ("service_freeze_plans", ""))
        self.assertEqual(saved.await_args.kwargs["reseller_id"], SHOP_A)

    async def test_another_shops_plan_or_another_key_is_refused(self) -> None:
        for data in ("res:st:pls:service_freeze_plans:71", "res:st:pls:service_freeze_max_days:72"):
            with self.subTest(data=data):
                saved, callback = await self.tap(data)
                saved.assert_not_awaited()
                self.assertTrue(callback.answer.await_args.kwargs.get("show_alert"))
