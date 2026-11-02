"""Bot screens for a shop owner renewing or changing their own package: one charge per button."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.handlers import reseller as reseller_handlers
from app.bot.handlers import reseller_package as handlers
from app.db.models import Base, BotUser, PgAdminSubscription, ResellerPlan, ResellerProfile, WalletTransaction
from app.services import bot_feedback_texts as feedback
from app.services.billing import GB
from app.services.reseller_package import STALE_PAGE

PERMS = '["orders","payments"]'


def plan(plan_id: int, name: str, *, users: int, gb: int, price: int, **extra) -> ResellerPlan:
    fields = {
        "plan_kind": "subscription", "billing_mode": "fixed", "duration_days": 30, "web_permissions": PERMS,
        "pg_role_id": 2, "pg_group_ids": "1", "create_pg_admin": True, "is_active": True, "is_system": False, **extra,
    }
    return ResellerPlan(id=plan_id, name=name, included_users=users, included_gb=gb, price=price, sort_order=plan_id, **fields)


def buttons(markup) -> dict[str, str]:
    return {b.callback_data: b.text for row in markup.inline_keyboard for b in row}


class ResellerPackageBotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add_all([
            BotUser(id=3, telegram_id=33, referral_code="s3", role="reseller", wallet_balance=1_000_000),
            BotUser(id=5, telegram_id=55, referral_code="u5", role="user", wallet_balance=1_000_000),
            plan(1, "برنز", users=10, gb=100, price=100_000),
            plan(2, "نقره <i>", users=20, gb=200, price=200_000),
            plan(3, "کوچک", users=2, gb=10, price=20_000),
            plan(4, "طلا", users=50, gb=500, price=500_000, pg_role_id=9),
            ResellerProfile(id=1, user_id=3, pg_admin_username="shop3", plan_id=1, web_permissions=PERMS, is_active=True),
            PgAdminSubscription(pg_username="shop3", plan_id=1, access_status="active", base_users=10, base_gb=100, renew_generation=5),
        ])
        await self.session.commit()
        self.pg = SimpleNamespace(
            get_admin=AsyncMock(return_value={"total_users": 6, "used_traffic": 20 * GB}),
            modify_admin=AsyncMock(return_value={}),
            get_users=AsyncMock(return_value={"users": []}),
        )
        self.edit = AsyncMock()
        self.patches = [
            patch("app.services.pasarguard.get_pg", return_value=self.pg),
            patch("app.services.billing_suspend._set_admin_enabled", AsyncMock()),
            patch("app.services.pg_admin_subscription.assert_child_growth_fits", AsyncMock()),
            patch.object(handlers, "safe_edit_text", self.edit),
            patch.object(reseller_handlers, "safe_edit_text", self.edit),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()
        await self.session.close()
        await self.engine.dispose()

    def callback(self, data: str) -> MagicMock:
        return MagicMock(data=data, answer=AsyncMock(), message=MagicMock())

    async def tap(self, handler, data: str, user_id: int = 3) -> MagicMock:
        self.session.expire_all()
        callback = self.callback(data)
        await handler(callback, self.session, await self.session.get(BotUser, user_id))
        return callback

    async def wallet(self) -> int:
        self.session.expire_all()
        return int((await self.session.get(BotUser, 3)).wallet_balance)

    async def debits(self) -> int:
        return int(await self.session.scalar(
            select(func.count()).select_from(WalletTransaction).where(WalletTransaction.amount < 0)
        ))

    def last_alert(self, callback: MagicMock) -> str:
        return callback.answer.await_args.args[0] if callback.answer.await_args.args else ""

    async def test_renew_screen_pays_once_per_shown_invoice(self) -> None:
        await self.tap(reseller_handlers.res_renew, "res:renew")
        shown = buttons(self.edit.await_args.kwargs["reply_markup"])
        self.assertIn("res:pkg:ok:1:5", shown)
        self.assertIn(handlers.LIST, shown)
        await self.tap(handlers.res_package_pay, "res:pkg:ok:1:5")
        second = await self.tap(handlers.res_package_pay, "res:pkg:ok:1:5")
        self.assertEqual(self.last_alert(second), STALE_PAGE[:180])
        self.assertEqual((await self.wallet(), await self.debits()), (900_000, 1))

    async def test_old_renew_button_shows_the_invoice_instead_of_paying(self) -> None:
        await self.tap(reseller_handlers.res_renew_go, "res:renew:go")
        self.assertEqual(await self.wallet(), 1_000_000)
        self.assertIn("res:pkg:ok:1:5", buttons(self.edit.await_args.kwargs["reply_markup"]))

    async def test_list_offers_only_moves_the_owner_may_make(self) -> None:
        await self.tap(handlers.res_package_list, handlers.LIST)
        text = self.edit.await_args.args[1]
        shown = buttons(self.edit.await_args.kwargs["reply_markup"])
        self.assertIn("res:pkg:v:2:5", shown)
        self.assertNotIn("res:pkg:v:3:5", shown, "6 users do not fit a 2-user package")
        self.assertNotIn("res:pkg:v:4:5", shown, "another panel role needs the admin")
        self.assertIn("نقره &lt;i&gt;", text)
        self.assertEqual(shown["res:renew"], "⬅️ بازگشت")

    async def test_upgrade_through_confirm_screen(self) -> None:
        await self.tap(handlers.res_package_view, "res:pkg:v:2:5")
        self.assertIn("res:pkg:ok:2:5", buttons(self.edit.await_args.kwargs["reply_markup"]))
        await self.tap(handlers.res_package_pay, "res:pkg:ok:2:5")
        self.assertEqual(await self.wallet(), 800_000)
        self.assertEqual((await self.session.get(ResellerProfile, 1)).plan_id, 2)
        self.assertIn("بستهٔ جدید", self.edit.await_args.args[1])

    async def test_stale_or_refused_targets_change_nothing(self) -> None:
        for handler, data in (
            (handlers.res_package_view, "res:pkg:v:2:4"),
            (handlers.res_package_view, "res:pkg:v:4:5"),
            (handlers.res_package_pay, "res:pkg:ok:3:5"),
            (handlers.res_package_pay, "res:pkg:ok:4:5"),
            (handlers.res_package_pay, "res:pkg:ok:2:x"),
            (handlers.res_package_pay, "res:pkg:ok:-2:5"),
            (handlers.res_package_pay, "res:pkg:ok:99999999999:5"),
        ):
            with self.subTest(data=data):
                self.edit.reset_mock()
                callback = await self.tap(handler, data)
                self.assertTrue(callback.answer.await_args.kwargs.get("show_alert"))
                self.edit.assert_not_awaited()
        self.assertEqual(await self.wallet(), 1_000_000)

    async def test_a_customer_cannot_use_the_shop_screens(self) -> None:
        for handler, data in ((handlers.res_package_list, handlers.LIST), (handlers.res_package_pay, "res:pkg:ok:1:5")):
            with self.subTest(data=data):
                callback = await self.tap(handler, data, user_id=5)
                self.assertEqual(self.last_alert(callback), feedback.RESELLERS_ONLY)
        self.assertEqual(await self.wallet(), 1_000_000)


if __name__ == "__main__":
    unittest.main()
