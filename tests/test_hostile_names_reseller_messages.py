"""Plan names and applicant names are user-controlled; Telegram HTML messages must escape them."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.handlers import reseller, reseller_plan_extra_packs_admin as packs_admin
from app.db.models import Base, BotUser, ResellerPlan, ResellerPlanExtraPack, Role

HOSTILE_PLAN = "Gold <a href='https://evil.example'>x</a> & <b>"
ESCAPED_PLAN = "Gold &lt;a href=&#x27;https://evil.example&#x27;&gt;x&lt;/a&gt; &amp; &lt;b&gt;"
HOSTILE_APPLICANT = "Eve <i>admin</i> & <a href='tg://user?id=1'>tap</a>"
ESCAPED_APPLICANT = "Eve &lt;i&gt;admin&lt;/i&gt; &amp; &lt;a href=&#x27;tg://user?id=1&#x27;&gt;tap&lt;/a&gt;"


def _assert_escaped(case: unittest.TestCase, text: str, escaped: str) -> None:
    case.assertIn(escaped, text)
    case.assertNotIn("<a href", text)
    case.assertNotIn("<i>", text)


class ResellerApplyMessageTests(unittest.IsolatedAsyncioTestCase):
    def _plan(self) -> ResellerPlan:
        return ResellerPlan(
            id=3, name=HOSTILE_PLAN, price=0, billing_mode="fixed", plan_kind="subscription",
            duration_days=30, included_gb=10, included_users=5, is_active=True,
        )

    def _callback(self, data: str) -> SimpleNamespace:
        return SimpleNamespace(data=data, answer=AsyncMock(), message=SimpleNamespace(), bot=MagicMock(send_message=AsyncMock()))

    def _patches(self, plan: ResellerPlan, edit: AsyncMock, **extra):
        base = {
            "_actor": AsyncMock(return_value=(None, None)),
            "_can_start_reseller_apply": lambda *_a, **_k: None,
            "list_active_reseller_plans": AsyncMock(return_value=[plan]),
            "get_all_settings": AsyncMock(return_value={}),
            "safe_edit_text": edit,
            "get_settings": lambda: SimpleNamespace(admin_ids=[111], currency="تومان"),
        }
        base.update(extra)
        return patch.multiple(reseller, **base)

    async def test_plan_screen_escapes_the_plan_name(self) -> None:
        edit = AsyncMock()
        with self._patches(self._plan(), edit):
            await reseller.resapply_plan(self._callback("resapply:plan:3"), AsyncMock(), SimpleNamespace(wallet_balance=0))
        _assert_escaped(self, edit.await_args.args[1], ESCAPED_PLAN)

    async def _buy(self, *, order) -> tuple[AsyncMock, MagicMock]:
        edit = AsyncMock()
        callback = self._callback("resapply:buy:3")
        user = BotUser(id=7, telegram_id=555, full_name=HOSTILE_APPLICANT, role=Role.USER.value)
        create = AsyncMock(return_value=(SimpleNamespace(id=9), order))
        pay = AsyncMock()
        with self._patches(self._plan(), edit, create_application=create), patch(
            "app.bot.handlers.terms.prompt_terms_if_needed", AsyncMock(return_value=False)
        ), patch("app.bot.menu_nav.present_order_pay", pay):
            await reseller.resapply_buy(callback, AsyncMock(), user, AsyncMock())
        self.edit, self.pay = edit, pay
        return edit, callback.bot.send_message

    async def test_free_application_escapes_plan_name_and_applicant_in_both_messages(self) -> None:
        edit, send = await self._buy(order=None)
        _assert_escaped(self, edit.await_args.args[1], ESCAPED_PLAN)
        admin_notice = send.await_args.args[1]
        _assert_escaped(self, admin_notice, ESCAPED_PLAN)
        _assert_escaped(self, admin_notice, ESCAPED_APPLICANT)

    async def test_paid_application_summary_escapes_the_plan_name(self) -> None:
        await self._buy(order=SimpleNamespace(id=4, amount=50_000))
        _assert_escaped(self, self.pay.await_args.kwargs["summary"], ESCAPED_PLAN)


class ExtraPacksMessageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add(ResellerPlan(id=3, name=HOSTILE_PLAN, price=0, allow_buy_extra=True))
        await self.session.flush()
        self.session.add(ResellerPlanExtraPack(id=5, plan_id=3, kind="gb", amount=10, button_style=None))
        await self.session.commit()
        self.admin = SimpleNamespace(role=Role.ADMIN.value, telegram_id=1)

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.engine.dispose()

    async def _text(self, handler, data: str) -> str:
        edit = AsyncMock()
        callback = SimpleNamespace(data=data, answer=AsyncMock(), message=SimpleNamespace())
        with patch.object(packs_admin, "safe_edit_text", edit), patch.object(packs_admin, "_is_admin", lambda _u: True), patch.object(
            packs_admin, "ensure_default_packs", AsyncMock()
        ):
            await handler(callback, self.session, self.admin)
        return edit.await_args.args[1]

    async def test_pack_home_escapes_the_plan_name(self) -> None:
        _assert_escaped(self, await self._text(packs_admin.extrapacks_home, "adm:resx:l:3"), ESCAPED_PLAN)

    async def test_pack_kind_list_escapes_the_plan_name(self) -> None:
        _assert_escaped(self, await self._text(packs_admin.extrapacks_kind, "adm:resx:k:3:gb"), ESCAPED_PLAN)

    async def test_pack_detail_escapes_the_plan_name(self) -> None:
        _assert_escaped(self, await self._text(packs_admin.extrapacks_view, "adm:resx:v:5"), ESCAPED_PLAN)

    async def test_pack_list_after_delete_escapes_the_plan_name(self) -> None:
        _assert_escaped(self, await self._text(packs_admin.extrapacks_del_ok, "adm:resx:do:5"), ESCAPED_PLAN)


if __name__ == "__main__":
    unittest.main()
