"""One person, one trial: the phone lock ignores number formats and reuses a verified number."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.handlers import shop
from app.db import Base
from app.db.models import BotUser, Plan, PurchaseContact, Role
from app.services.orders import create_order
from app.services.purchase_contact import verified_contact_phone_hash
from app.services.trial_contact import hash_trial_phone
from app.services.users import set_setting

SECRET = "test-secret-for-trial-phone-lock"
IRAN_FORMATS = ("09121234567", "989121234567", "+98 912 123 4567", "00989121234567", "9121234567")


def _hash(raw: str, *, require_iran: bool = False) -> str:
    return hash_trial_phone(raw, secret=SECRET, require_iran=require_iran)


class PhoneHashTests(unittest.TestCase):
    def test_every_format_of_an_iranian_number_gives_one_hash(self) -> None:
        for require_iran in (False, True):
            with self.subTest(require_iran=require_iran):
                self.assertEqual({_hash(raw, require_iran=require_iran) for raw in IRAN_FORMATS}, {_hash("989121234567")})

    def test_foreign_numbers_stay_distinct_and_keep_their_digits(self) -> None:
        self.assertNotEqual(_hash("+905321234567"), _hash("989121234567"))
        self.assertEqual(_hash("+90 532 123 4567"), _hash("00905321234567"))

    def test_too_short_numbers_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            _hash("12345")


class TrialLockTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmpdir.name) / 'p.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self._tmpdir.cleanup()

    async def _user(self, session, telegram_id: int, *, blocked: bool = False) -> BotUser:
        user = BotUser(
            telegram_id=telegram_id, username=f"u{telegram_id}", full_name=f"user {telegram_id}",
            role=Role.USER.value, referral_code=f"R{telegram_id}", is_blocked=blocked,
        )
        session.add(user)
        await session.flush()
        return user


class SamePhoneTests(TrialLockTestCase):
    async def test_a_second_account_with_the_same_number_in_another_format_gets_no_trial(self) -> None:
        async with self.Session() as session:
            first = await self._user(session, 1)
            second = await self._user(session, 2)
            plan = Plan(name="trial", price=0, duration_days=1, data_limit_gb=1, is_active=True, is_trial=True)
            session.add(plan)
            await session.commit()
            await set_setting(session, "trial_require_contact", "1")
            with patch("app.services.users.current_shop_reseller_id", return_value=None):
                await create_order(
                    session, user_id=first.id, plan_id=plan.id,
                    trial_phone_hash=_hash("+98 912 123 4567"), trial_telegram_id=first.telegram_id,
                )
                with self.assertRaisesRegex(ValueError, "قبلاً"):
                    await create_order(
                        session, user_id=second.id, plan_id=plan.id,
                        trial_phone_hash=_hash("09121234567"), trial_telegram_id=second.telegram_id,
                    )


class VerifiedNumberTests(TrialLockTestCase):
    async def test_a_number_verified_in_this_shop_is_reused(self) -> None:
        async with self.Session() as session:
            user = await self._user(session, 10)
            session.add(PurchaseContact(user_id=user.id, shop_key=0, telegram_id=10, phone_hash="h" * 64))
            await session.commit()
            self.assertEqual(await verified_contact_phone_hash(session, user.id, shop_key=0), "h" * 64)

    async def test_other_shops_other_accounts_and_blocked_users_get_nothing(self) -> None:
        async with self.Session() as session:
            other_shop = await self._user(session, 20)
            moved = await self._user(session, 21)
            blocked = await self._user(session, 22, blocked=True)
            session.add_all([
                PurchaseContact(user_id=other_shop.id, shop_key=7, telegram_id=20, phone_hash="a" * 64),
                PurchaseContact(user_id=moved.id, shop_key=0, telegram_id=999, phone_hash="b" * 64),
                PurchaseContact(user_id=blocked.id, shop_key=0, telegram_id=22, phone_hash="c" * 64),
            ])
            await session.commit()
            self.assertIsNone(await verified_contact_phone_hash(session, other_shop.id, shop_key=0))
            self.assertIsNone(await verified_contact_phone_hash(session, moved.id, shop_key=0))
            self.assertIsNone(await verified_contact_phone_hash(session, blocked.id, shop_key=0))


class TrialButtonTests(TrialLockTestCase):
    async def _tap(self, ui: dict, *, verified_hash: str | None):
        plan = SimpleNamespace(id=5, is_trial=True, price=0)
        callback = SimpleNamespace(data="shop:buy:5", answer=AsyncMock(), message=SimpleNamespace(answer=AsyncMock()))
        user = SimpleNamespace(id=3, telegram_id=33)
        state = SimpleNamespace(set_state=AsyncMock(), update_data=AsyncMock(), get_data=AsyncMock(return_value={}))
        complete = AsyncMock()
        with (
            patch.object(shop, "get_all_settings", AsyncMock(return_value=ui)),
            patch.object(shop, "_answer_shop_maintenance", AsyncMock(return_value=False)),
            patch("app.bot.handlers.terms.prompt_terms_if_needed", AsyncMock(return_value=False)),
            patch.object(shop, "get_catalog_plan", AsyncMock(return_value=plan)),
            patch.object(shop, "sale_price", return_value=0),
            patch.object(shop, "verified_contact_phone_hash", AsyncMock(return_value=verified_hash)),
            patch.object(shop, "_complete_shop_buy", complete),
        ):
            await shop.shop_buy(callback, AsyncMock(), user, state)
        return complete, callback, state

    async def test_a_verified_number_skips_the_contact_prompt(self) -> None:
        complete, callback, state = await self._tap({"trial_require_contact": "1"}, verified_hash="v" * 64)
        kwargs = complete.await_args.kwargs
        self.assertEqual((kwargs["trial_phone_hash"], kwargs["trial_telegram_id"]), ("v" * 64, 33))
        callback.message.answer.assert_not_awaited()
        state.set_state.assert_not_awaited()

    async def test_the_iran_only_gate_never_reuses_an_unlabelled_hash(self) -> None:
        complete, callback, state = await self._tap(
            {"trial_require_contact": "1", "trial_require_iran_phone": "1"}, verified_hash="v" * 64,
        )
        complete.assert_not_awaited()
        state.set_state.assert_awaited_once()
        callback.message.answer.assert_awaited_once()

    async def test_without_a_verified_number_the_customer_is_asked(self) -> None:
        complete, callback, state = await self._tap({"trial_require_contact": "1"}, verified_hash=None)
        complete.assert_not_awaited()
        state.set_state.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
