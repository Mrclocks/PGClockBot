"""Owner-only PAYG charge review in the bot: denial, one decision per charge, escaped names, hub badge."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.handlers import payg_charge_review as view
from app.bot.nav_inline import admin_ops_hub_keyboard
from app.db import Base
from app.db.models import BotUser, ResellerCharge, ResellerPlan, ResellerProfile
from app.services import bot_feedback_texts as feedback
from app.services.payg_charges import ChargeStatus, reserve_charge, send_to_review
from app.services.users import DEFAULT_SETTINGS
from tests.test_payg_charges import create

HANDLERS = {
    "l": view.show_list,
    "v": view.show_charge,
    "s": view.confirm_decision,
    "r": view.confirm_decision,
    "S": view.run_decision,
    "R": view.run_decision,
}


def callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


class PaygReviewBotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 'r.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                ResellerPlan(id=1, name="payg", billing_mode="payg", price_per_gb=1000,
                             payg_basis="traffic", payg_charge_upfront=True),
                BotUser(id=5, telegram_id=5, referral_code="r5", role="reseller", wallet_balance=50_000,
                        full_name="<b>Shop</b>"),
            ])
            session.add(ResellerProfile(id=1, user_id=5, plan_id=1, billing_mode="payg", payg_wallet_linked=True))
            await session.commit()
            profile = await session.get(ResellerProfile, 1)
            charge = await reserve_charge(session, profile, create(10))
            await send_to_review(session, charge)
            self.charge_id = int(charge.id)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self._tmp.cleanup()

    async def press(self, data: str, *, owner: bool = True, reseller_bot: bool = False):
        callback = AsyncMock()
        callback.data = data
        callback.message = AsyncMock()
        async with self.Session() as session:
            with patch.object(view, "require_bot_owner", AsyncMock(return_value=owner)), \
                    patch.object(view, "safe_edit_text", AsyncMock()) as edit:
                await HANDLERS[data.split(":")[1]](
                    callback, session=session, db_user=SimpleNamespace(id=9), is_reseller_bot=reseller_bot
                )
        return callback, edit

    async def wallet(self) -> int:
        async with self.Session() as session:
            return int((await session.get(BotUser, 5)).wallet_balance)

    async def status(self) -> str:
        async with self.Session() as session:
            return str((await session.get(ResellerCharge, self.charge_id)).status)

    async def test_non_owners_and_shop_bots_see_and_change_nothing(self) -> None:
        cid = self.charge_id
        for data in ("pcr:l:0", f"pcr:v:{cid}", f"pcr:s:{cid}", f"pcr:r:{cid}", f"pcr:S:{cid}", f"pcr:R:{cid}"):
            for owner, reseller_bot in ((False, False), (True, True)):
                with self.subTest(data=data, owner=owner, reseller_bot=reseller_bot):
                    callback, edit = await self.press(data, owner=owner, reseller_bot=reseller_bot)
                    callback.answer.assert_awaited_once_with(feedback.NO_ACCESS, show_alert=True)
                    edit.assert_not_awaited()
        self.assertEqual(await self.status(), ChargeStatus.REVIEW)
        self.assertEqual(await self.wallet(), 40_000)

    async def test_the_list_and_card_show_the_charge_with_escaped_shop_name(self) -> None:
        _, edit = await self.press("pcr:l:0")
        self.assertIn(f"pcr:v:{self.charge_id}", callbacks(edit.await_args.kwargs["reply_markup"]))
        _, edit = await self.press(f"pcr:v:{self.charge_id}")
        text = edit.await_args.args[1]
        self.assertIn("&lt;b&gt;Shop&lt;/b&gt;", text)
        self.assertNotIn("<b>Shop</b>", text)
        found = callbacks(edit.await_args.kwargs["reply_markup"])
        self.assertIn(f"pcr:s:{self.charge_id}", found)
        self.assertIn(f"pcr:r:{self.charge_id}", found)

    async def test_asking_to_decide_changes_nothing_until_confirmed(self) -> None:
        _, edit = await self.press(f"pcr:r:{self.charge_id}")
        self.assertIn(f"pcr:R:{self.charge_id}", callbacks(edit.await_args.kwargs["reply_markup"]))
        self.assertEqual(await self.status(), ChargeStatus.REVIEW)
        self.assertEqual(await self.wallet(), 40_000)

    async def test_settling_keeps_the_money_and_a_second_tap_is_refused(self) -> None:
        callback, _ = await self.press(f"pcr:S:{self.charge_id}")
        callback.answer.assert_awaited_once_with(view.SETTLED_MESSAGE, show_alert=True)
        self.assertEqual(await self.status(), ChargeStatus.SETTLED)
        self.assertEqual(await self.wallet(), 40_000)
        for data in (f"pcr:R:{self.charge_id}", f"pcr:S:{self.charge_id}"):
            callback, _ = await self.press(data)
            callback.answer.assert_awaited_once_with(view.ALREADY_DECIDED_MESSAGE, show_alert=True)
        self.assertEqual(await self.wallet(), 40_000)

    async def test_refunding_twice_returns_the_money_once(self) -> None:
        callback, _ = await self.press(f"pcr:R:{self.charge_id}")
        callback.answer.assert_awaited_once_with(view.REFUNDED_MESSAGE, show_alert=True)
        callback, _ = await self.press(f"pcr:R:{self.charge_id}")
        callback.answer.assert_awaited_once_with(view.ALREADY_DECIDED_MESSAGE, show_alert=True)
        self.assertEqual(await self.status(), ChargeStatus.REFUNDED)
        self.assertEqual(await self.wallet(), 50_000)

    async def test_a_decided_charge_has_no_card(self) -> None:
        await self.press(f"pcr:S:{self.charge_id}")
        for data in (f"pcr:v:{self.charge_id}", f"pcr:r:{self.charge_id}", "pcr:v:999999"):
            callback, edit = await self.press(data)
            callback.answer.assert_awaited_once_with(view.ALREADY_DECIDED_MESSAGE, show_alert=True)
            edit.assert_not_awaited()


class OpsHubEntryTests(unittest.TestCase):
    def test_the_ops_hub_shows_the_review_only_while_charges_wait(self) -> None:
        ui = dict(DEFAULT_SETTINGS)
        self.assertNotIn(view.LIST_CALLBACK, callbacks(admin_ops_hub_keyboard(ui)))
        markup = admin_ops_hub_keyboard(ui, pending_payg_review=3)
        button = next(b for row in markup.inline_keyboard for b in row if b.callback_data == view.LIST_CALLBACK)
        self.assertIn("۳", button.text)
