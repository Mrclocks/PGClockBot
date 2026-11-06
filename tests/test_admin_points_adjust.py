"""Platform admin points adjust + points on staff/web user cards."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.keyboards import admin_user_actions
from app.services.staff_cards import format_staff_customer_card


class KeyboardPointsButtonsTests(unittest.TestCase):
    def test_admin_user_actions_has_points_credit_and_debit(self) -> None:
        markup = admin_user_actions(42, is_blocked=False, role="user")
        cbs = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("adm:users:pcredit:42", cbs)
        self.assertIn("adm:users:pdebit:42", cbs)
        texts = [b.text for row in markup.inline_keyboard for b in row]
        self.assertIn("⭐ امتیاز +", texts)
        self.assertIn("⭐ امتیاز −", texts)
        wallet_idx = next(
            i
            for i, row in enumerate(markup.inline_keyboard)
            if any("wcredit" in (b.callback_data or "") for b in row)
        )
        points_idx = next(
            i
            for i, row in enumerate(markup.inline_keyboard)
            if any("pcredit" in (b.callback_data or "") for b in row)
        )
        self.assertEqual(points_idx, wallet_idx + 1)


class StaffCardPointsTests(unittest.TestCase):
    def test_points_row_shown_when_provided(self) -> None:
        text = format_staff_customer_card(
            flags="",
            display_name="Ali",
            telegram_id=1,
            username=None,
            wallet_text="۰ تومان",
            points_text="۱٬۲۵۰",
            service_count=0,
            is_blocked=False,
        )
        self.assertIn("امتیاز", text)
        self.assertIn("۱٬۲۵۰", text)

    def test_points_row_omitted_when_none(self) -> None:
        text = format_staff_customer_card(
            flags="",
            display_name="Ali",
            telegram_id=1,
            username=None,
            wallet_text="۰ تومان",
            service_count=0,
            is_blocked=False,
        )
        self.assertNotIn("امتیاز", text)


class AdminPointsAdjustHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from app.db import Base
        from app.db.models import BotUser

        self._tmpdir = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmpdir.name) / 'pts.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all(
                [
                    BotUser(
                        id=1,
                        telegram_id=1001,
                        referral_code="adm",
                        role="admin",
                        full_name="Owner",
                        points_balance=0,
                    ),
                    BotUser(
                        id=10,
                        telegram_id=1010,
                        referral_code="c10",
                        role="user",
                        full_name="Customer",
                        points_balance=50,
                        reseller_id=None,
                    ),
                    BotUser(
                        id=20,
                        telegram_id=1020,
                        referral_code="c20",
                        role="user",
                        full_name="ShopCustomer",
                        points_balance=0,
                        reseller_id=99,
                    ),
                ]
            )
            await session.commit()
        self.state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self._tmpdir.cleanup()

    async def _admin(self, session):
        from app.db.models import BotUser

        return await session.get(BotUser, 1)

    def _message(self, text: str) -> MagicMock:
        return MagicMock(text=text, answer=AsyncMock())

    @asynccontextmanager
    async def _owner_gate(self):
        with patch("app.bot.auth.is_bot_owner_principal", AsyncMock(return_value=True)):
            with patch("app.bot.handlers.admin_points_adjust._is_admin", return_value=True):
                yield


class BotApplyPointsTests(AdminPointsAdjustHarness):
    async def _apply(self, session, *, mode: str, amount: int, reason: str, user_id: int = 10, key: str = "a" * 16):
        from app.bot.handlers import admin_points_adjust as mod

        admin = await self._admin(session)
        await self.state.set_state(mod.PointsAdjustStates.reason)
        await self.state.update_data(
            admin_points_user_id=user_id,
            admin_points_mode=mode,
            admin_points_amount=amount,
            admin_points_key=key,
        )
        msg = self._message(reason)
        outcome = AsyncMock()
        async with self._owner_gate():
            with patch("app.bot.handlers.admin._outcome_on_card", new=outcome):
                await mod.adm_users_points_apply(msg, self.state, session, admin)
        return msg, outcome

    async def test_credit_with_reason_updates_balance(self) -> None:
        from app.db.models import BotUser
        from app.services.loyalty import points_balance

        async with self.Session() as session:
            _msg, outcome = await self._apply(session, mode="credit", amount=25, reason="هدیه پشتیبانی")
            self.assertEqual(await points_balance(session, await session.get(BotUser, 10), None), 75)
            outcome.assert_awaited()

    async def test_debit_with_reason_updates_balance(self) -> None:
        from app.db.models import BotUser
        from app.services.loyalty import points_balance

        async with self.Session() as session:
            _msg, outcome = await self._apply(
                session, mode="debit", amount=20, reason="اصلاح اشتباه", key="b" * 16
            )
            self.assertEqual(await points_balance(session, await session.get(BotUser, 10), None), 30)
            outcome.assert_awaited()

    async def test_short_reason_is_rejected(self) -> None:
        from app.db.models import BotUser
        from app.services.loyalty import points_balance

        async with self.Session() as session:
            msg, outcome = await self._apply(
                session, mode="credit", amount=10, reason="اب", key="c" * 16
            )
            self.assertEqual(await points_balance(session, await session.get(BotUser, 10), None), 50)
            outcome.assert_not_awaited()
            msg.answer.assert_awaited()
            self.assertIn("علت", msg.answer.await_args.args[0])

    async def test_empty_reason_is_rejected(self) -> None:
        from app.db.models import BotUser
        from app.services.loyalty import points_balance

        async with self.Session() as session:
            msg, outcome = await self._apply(
                session, mode="credit", amount=10, reason="  ", key="d" * 16
            )
            self.assertEqual(await points_balance(session, await session.get(BotUser, 10), None), 50)
            outcome.assert_not_awaited()
            msg.answer.assert_awaited()

    async def test_shop_customer_is_denied(self) -> None:
        from app.db.models import BotUser
        from app.services.loyalty import points_balance

        async with self.Session() as session:
            msg, outcome = await self._apply(
                session, mode="credit", amount=10, reason="هدیه تست", user_id=20, key="e" * 16
            )
            self.assertEqual(await points_balance(session, await session.get(BotUser, 20), None), 0)
            outcome.assert_not_awaited()
            msg.answer.assert_awaited()

    async def test_pcredit_callback_denies_shop_customer(self) -> None:
        from app.bot.handlers import admin_points_adjust as mod
        from app.db.models import BotUser
        from app.services.loyalty import points_balance

        async with self.Session() as session:
            admin = await self._admin(session)
            callback = MagicMock(
                data="adm:users:pcredit:20",
                answer=AsyncMock(),
                message=MagicMock(),
            )
            async with self._owner_gate():
                with patch("app.bot.nav_input.ask_text", AsyncMock()) as ask:
                    await mod.adm_users_points_credit_ask(callback, self.state, session, admin)
            ask.assert_not_awaited()
            callback.answer.assert_awaited()
            self.assertIn("فروشگاه", callback.answer.await_args.args[0])
            self.assertEqual(await points_balance(session, await session.get(BotUser, 20), None), 0)

    async def test_pdebit_callback_denies_missing_user(self) -> None:
        from app.bot.handlers import admin_points_adjust as mod

        async with self.Session() as session:
            admin = await self._admin(session)
            callback = MagicMock(
                data="adm:users:pdebit:404",
                answer=AsyncMock(),
                message=MagicMock(),
            )
            async with self._owner_gate():
                with patch("app.bot.nav_input.ask_text", AsyncMock()) as ask:
                    await mod.adm_users_points_debit_ask(callback, self.state, session, admin)
            ask.assert_not_awaited()
            callback.answer.assert_awaited()


class ShopCardPointsIsolationTests(AdminPointsAdjustHarness):
    async def test_shop_card_shows_only_that_shops_points(self) -> None:
        from app.db.models import BotUser, ShopPoints
        from app.services.formatting import format_number
        from app.services.loyalty import credit_points, points_balance
        from app.services.staff_cards import format_staff_customer_card

        shop_a, shop_b = 99, 88
        async with self.Session() as session:
            user = await session.get(BotUser, 20)
            # Platform purse must never leak onto a shop card.
            user.points_balance = 9_999
            session.add(ShopPoints(user_id=20, reseller_id=shop_a, balance=0))
            session.add(ShopPoints(user_id=20, reseller_id=shop_b, balance=0))
            await session.commit()
            await credit_points(
                session,
                user,
                120,
                tx_type="adjust",
                source="test",
                reference=None,
                description="shop a",
                idempotency_key="shop-a-pts",
                shop_id=shop_a,
                commit=True,
            )
            await credit_points(
                session,
                user,
                45,
                tx_type="adjust",
                source="test",
                reference=None,
                description="shop b",
                idempotency_key="shop-b-pts",
                shop_id=shop_b,
                commit=True,
            )
            await session.refresh(user)
            self.assertEqual(await points_balance(session, user, shop_a), 120)
            self.assertEqual(await points_balance(session, user, shop_b), 45)
            self.assertEqual(await points_balance(session, user, None), 9_999)
            card_a = format_staff_customer_card(
                flags="",
                display_name=user.full_name or "—",
                telegram_id=int(user.telegram_id),
                username=None,
                wallet_text="۰",
                points_text=format_number(await points_balance(session, user, shop_a)),
                service_count=0,
                is_blocked=False,
                wallet_label="کیف پول فروشگاه",
            )
            card_b = format_staff_customer_card(
                flags="",
                display_name=user.full_name or "—",
                telegram_id=int(user.telegram_id),
                username=None,
                wallet_text="۰",
                points_text=format_number(await points_balance(session, user, shop_b)),
                service_count=0,
                is_blocked=False,
                wallet_label="کیف پول فروشگاه",
            )
        self.assertIn(format_number(120), card_a)
        self.assertNotIn(format_number(45), card_a)
        self.assertNotIn(format_number(9_999), card_a)
        self.assertIn(format_number(45), card_b)
        self.assertNotIn(format_number(120), card_b)

    async def test_web_user_card_uses_shop_scope_points(self) -> None:
        from app.db.models import BotUser, ShopPoints
        from app.services.loyalty import credit_points
        from app.services.user_card import build_user_card

        async with self.Session() as session:
            user = await session.get(BotUser, 20)
            user.points_balance = 500
            session.add(ShopPoints(user_id=20, reseller_id=99, balance=0))
            await session.commit()
            await credit_points(
                session,
                user,
                33,
                tx_type="adjust",
                source="test",
                reference=None,
                description="shop",
                idempotency_key="shop-web-pts",
                shop_id=99,
                commit=True,
            )
            shop_card = await build_user_card(session, user, shop_id=99, show_private_marks=False)
            platform_card = await build_user_card(session, user, shop_id=None, show_private_marks=False)
            self.assertEqual(shop_card.points_balance, 33)
            self.assertEqual(platform_card.points_balance, 500)


class WebUserCardPointsTests(AdminPointsAdjustHarness):
    async def test_user_card_includes_points_balance(self) -> None:
        from app.db.models import BotUser
        from app.services.user_card import build_user_card

        async with self.Session() as session:
            user = await session.get(BotUser, 10)
            card = await build_user_card(session, user, shop_id=None, show_private_marks=False)
            self.assertEqual(card.points_balance, 50)

    async def test_web_card_template_renders_points(self) -> None:
        from jinja2 import Environment, FileSystemLoader, select_autoescape

        from app.db.models import BotUser
        from app.services.formatting import format_money, format_number
        from app.services.user_card import build_user_card

        async with self.Session() as session:
            user = await session.get(BotUser, 10)
            card = await build_user_card(session, user, shop_id=None, show_private_marks=False)
        env = Environment(
            loader=FileSystemLoader("app/web/templates"),
            autoescape=select_autoescape(["html"]),
        )
        env.filters["money"] = format_money
        env.filters["num"] = format_number
        html = env.get_template("_user_card_body.html").render(
            card=card, can_edit=False, can_message=False
        )
        self.assertIn("امتیاز", html)
        self.assertIn("50", html)


if __name__ == "__main__":
    unittest.main()
