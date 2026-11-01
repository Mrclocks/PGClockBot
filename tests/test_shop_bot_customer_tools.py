"""Shop bot customer card: tag, note, wallet, points and block, under the same keys and checks as the web."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.handlers import reseller_customer_tools as tools
from app.services.customer_marks import marks_for

SHOP_A = 81
SHOP_B = 82
BOT_ADMIN_TG = 990_001
ALL_KEYS = "dashboard,customer_notes,wallet_adjust,loyalty_adjust,customers_block"


def _callback(data: str) -> MagicMock:
    message = MagicMock(edit_text=AsyncMock(), answer=AsyncMock())
    return MagicMock(data=data, answer=AsyncMock(), message=message)


def _message(text: str) -> MagicMock:
    return MagicMock(text=text, answer=AsyncMock())


class ShopBotToolsHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import Base
        from app.db.models import BotUser, ResellerProfile

        self._tmpdir = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmpdir.name) / 'tools.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            for shop in (SHOP_A, SHOP_B):
                session.add(BotUser(id=shop, telegram_id=shop * 10, referral_code=f"s{shop}", role="reseller", username=f"shop{shop}"))
            await session.flush()
            session.add(ResellerProfile(user_id=SHOP_A, is_active=True, web_permissions=ALL_KEYS, bot_permissions=ALL_KEYS,
                                        bot_admin_ids=str(BOT_ADMIN_TG)))
            session.add(ResellerProfile(user_id=SHOP_B, is_active=True, web_permissions=ALL_KEYS, bot_permissions=ALL_KEYS))
            session.add_all([
                BotUser(id=101, telegram_id=1010, referral_code="c101", reseller_id=SHOP_A, full_name="<b>Sara</b>"),
                BotUser(id=102, telegram_id=1020, referral_code="c102", reseller_id=SHOP_B),
                BotUser(id=104, telegram_id=BOT_ADMIN_TG, referral_code="c104", reseller_id=SHOP_A, username="helper"),
                BotUser(id=105, telegram_id=1050, referral_code="c105", reseller_id=SHOP_A, username="stranger"),
            ])
            await session.commit()
        self.state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))

    async def asyncTearDown(self):
        await self.engine.dispose()
        self._tmpdir.cleanup()

    async def _actor(self, session, user_id: int = SHOP_A):
        from app.db.models import BotUser

        return await session.get(BotUser, user_id)

    def _ctx(self) -> dict:
        return {"is_reseller_bot": True, "reseller_owner_id": SHOP_A}

    async def _set_keys(self, keys: str) -> None:
        from sqlalchemy import update

        from app.db.models import ResellerProfile

        async with self.Session() as session:
            await session.execute(update(ResellerProfile).where(ResellerProfile.user_id == SHOP_A)
                                  .values(web_permissions=keys, bot_permissions=keys))
            await session.commit()


class CardButtonsTests(ShopBotToolsHarness):
    async def test_only_the_actions_the_shop_may_take(self):
        from app.db.models import ResellerProfile

        async with self.Session() as session:
            actor = await self._actor(session)
            profile = (await session.execute(select(ResellerProfile).where(ResellerProfile.user_id == SHOP_A))).scalar_one()
            every = [b.callback_data for row in tools.customer_tool_rows(profile, actor, 101) for b in row]
            self.assertEqual(every, [
                "rct:tag:101", "rct:note:101", "rct:wallet:credit:101", "rct:wallet:debit:101",
                "rct:points:credit:101", "rct:points:debit:101", "rct:block:101",
            ])
            profile.web_permissions = "dashboard"
            self.assertEqual(tools.customer_tool_rows(profile, actor, 101), [])
            self.assertTrue(all(len(c.encode()) <= 64 for c in every))


class TagAndNoteTests(ShopBotToolsHarness):
    async def test_tag_is_saved_for_this_shop_only(self):
        async with self.Session() as session:
            callback = _callback("rct:tagset:red:101")
            await tools.rct_tag_save(callback, session, await self._actor(session), **self._ctx())
            marks = await marks_for(session, SHOP_A, [101])
            other = await marks_for(session, SHOP_B, [101])
        self.assertEqual(marks[101].tag, "red")
        self.assertEqual(other[101].tag, "green")

    async def test_another_shops_customer_and_a_forged_tag_are_refused(self):
        async with self.Session() as session:
            for data in ("rct:tagset:red:102", "rct:tagset:<b>:101", "rct:tagset:red:abc"):
                callback = _callback(data)
                await tools.rct_tag_save(callback, session, await self._actor(session), **self._ctx())
                callback.answer.assert_awaited_with(tools.feedback.NO_ACCESS, show_alert=True)
            marks = await marks_for(session, SHOP_B, [102])
        self.assertEqual(marks[102].tag, "green")

    async def test_note_flow(self):
        async with self.Session() as session:
            actor = await self._actor(session)
            await tools.rct_note_start(_callback("rct:note:101"), self.state, session, actor, **self._ctx())
            await tools.rct_note_save(_message("pays late"), self.state, session, actor, **self._ctx())
            self.assertIsNone(await self.state.get_state())
            self.assertEqual((await marks_for(session, SHOP_A, [101]))[101].note, "pays late")

    async def test_an_outsider_on_the_shop_bot_gets_nothing(self):
        async with self.Session() as session:
            outsider = await self._actor(session, 105)
            callback = _callback("rct:tagset:red:101")
            await tools.rct_tag_save(callback, session, outsider, **self._ctx())
            callback.answer.assert_awaited_with(tools.feedback.NO_ACCESS, show_alert=True)
            self.assertEqual((await marks_for(session, SHOP_A, [101]))[101].tag, "green")


class BalanceTests(ShopBotToolsHarness):
    async def _wallet_balance(self, user_id: int) -> int:
        from app.services.wallet import get_wallet_balance

        async with self.Session() as session:
            return await get_wallet_balance(session, user_id, shop_id=SHOP_A)

    async def _flow(self, data: str, amount: str, reason: str, actor_id: int = SHOP_A) -> MagicMock:
        async with self.Session() as session:
            actor = await self._actor(session, actor_id)
            await tools.rct_balance_start(_callback(data), self.state, session, actor, **self._ctx())
            await tools.rct_balance_amount(_message(amount), self.state)
            done = _message(reason)
            await tools.rct_balance_apply(done, self.state, session, actor, **self._ctx())
            return done

    async def test_wallet_credit_applies_once_for_the_flows_key(self):
        await self.state.clear()
        async with self.Session() as session:
            actor = await self._actor(session)
            await tools.rct_balance_start(_callback("rct:wallet:credit:101"), self.state, session, actor, **self._ctx())
            data = await self.state.get_data()
            for _ in range(2):
                await tools._apply_balance_change(
                    session, tools.shop_bot_staff(await self._profile(session), actor),
                    {**data, "rct_amount": "20000"}, user_id=101, reason="refund",
                )
        self.assertEqual(await self._wallet_balance(101), 20_000)

    async def _profile(self, session):
        from app.db.models import ResellerProfile

        return (await session.execute(select(ResellerProfile).where(ResellerProfile.user_id == SHOP_A))).scalar_one()

    async def test_wallet_and_points_flows(self):
        from app.db.models import BotUser
        from app.services.loyalty import points_balance

        done = await self._flow("rct:wallet:credit:101", "15000", "outage refund")
        self.assertIn("15,000", done.answer.await_args.args[0])
        await self._flow("rct:points:credit:101", "40", "birthday")
        self.assertEqual(await self._wallet_balance(101), 15_000)
        async with self.Session() as session:
            self.assertEqual(await points_balance(session, await session.get(BotUser, 101), SHOP_A), 40)

    async def test_a_bot_admin_acts_but_never_on_themselves_and_rules_still_hold(self):
        await self._flow("rct:wallet:credit:101", "5000", "by helper", actor_id=104)
        self.assertEqual(await self._wallet_balance(101), 5_000)
        refused = await self._flow("rct:wallet:credit:104", "5000", "self credit", actor_id=104)
        self.assertNotIn("✅", refused.answer.await_args.args[0])
        too_big = await self._flow("rct:wallet:debit:101", "6000", "over balance")
        self.assertNotIn("✅", too_big.answer.await_args.args[0])
        self.assertEqual(await self._wallet_balance(104), 0)
        self.assertEqual(await self._wallet_balance(101), 5_000)

    async def test_without_the_key_the_flow_does_not_start(self):
        await self._set_keys("dashboard,customer_notes")
        async with self.Session() as session:
            callback = _callback("rct:wallet:credit:101")
            await tools.rct_balance_start(callback, self.state, session, await self._actor(session), **self._ctx())
        callback.answer.assert_awaited_with(tools.feedback.NO_ACCESS, show_alert=True)
        self.assertIsNone(await self.state.get_state())


class BlockTests(ShopBotToolsHarness):
    async def test_block_and_unblock_in_this_shop(self):
        from app.services.shop_customer_block import is_blocked_in_shop

        async with self.Session() as session:
            actor = await self._actor(session)
            await tools.rct_block_start(_callback("rct:block:101"), self.state, session, actor, **self._ctx())
            await tools.rct_block_apply(_message("spam"), self.state, session, actor, **self._ctx())
            self.assertTrue(await is_blocked_in_shop(session, 101, SHOP_A))
            self.assertFalse(await is_blocked_in_shop(session, 101, SHOP_B))
            await tools.rct_unblock(_callback("rct:unblock:101"), session, actor, **self._ctx())
            self.assertFalse(await is_blocked_in_shop(session, 101, SHOP_A))

    async def test_another_shops_customer_cannot_be_blocked(self):
        from app.db.models import BotUserShop

        async with self.Session() as session:
            actor = await self._actor(session)
            await self.state.update_data(rct_user_id=102)
            reply = _message("spam")
            await tools.rct_block_apply(reply, self.state, session, actor, **self._ctx())
            count = (await session.execute(select(func.count()).select_from(BotUserShop))).scalar_one()
        self.assertEqual(count, 0)
        self.assertNotIn("🚫 مشتری", reply.answer.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
