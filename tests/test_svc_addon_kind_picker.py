"""Customer bot addon flow: kind picker before pack list, kind-scoped exclusivity."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.handlers.services import svc_addon
from app.db.models import Base, BotUser, Plan, ServiceAddonPack, UserService
from app.services.users import reset_shop_reseller_id, set_shop_reseller_id


class SvcAddonKindPickerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.shop = set_shop_reseller_id(None)

    async def asyncTearDown(self) -> None:
        reset_shop_reseller_id(self.shop)
        await self.engine.dispose()

    async def _seed(self, session, *, packs: list[ServiceAddonPack]) -> BotUser:
        user = BotUser(id=1, telegram_id=101, referral_code="u", wallet_balance=50_000)
        session.add_all(
            [
                user,
                Plan(id=1, name="P", price=1000, duration_days=30),
                UserService(
                    id=10,
                    bot_user_id=1,
                    plan_id=1,
                    pg_username="svc-a",
                    pg_user_id=1,
                ),
                *packs,
            ]
        )
        await session.commit()
        return user

    async def _run(self, session, user: BotUser, data: str):
        callback = MagicMock(data=data, message=MagicMock())
        callback.answer = AsyncMock()
        with (
            patch("app.bot.handlers.services.safe_edit_text", new=AsyncMock()) as edit,
            patch(
                "app.bot.handlers.services.get_all_settings",
                new=AsyncMock(return_value={}),
            ),
        ):
            await svc_addon(callback, session, user)
        return callback, edit

    def _button_data(self, edit: AsyncMock) -> list[str]:
        markup = edit.await_args.kwargs.get("reply_markup")
        return [
            b.callback_data
            for row in markup.inline_keyboard
            for b in row
            if b.callback_data
        ]

    def _button_texts(self, edit: AsyncMock) -> list[str]:
        markup = edit.await_args.kwargs.get("reply_markup")
        return [b.text for row in markup.inline_keyboard for b in row if b.text]

    async def test_both_kinds_show_picker_first(self) -> None:
        async with self.Session() as session:
            user = await self._seed(
                session,
                packs=[
                    ServiceAddonPack(
                        id=1, name="V", kind="volume", amount=5, price=100, is_general=True
                    ),
                    ServiceAddonPack(
                        id=2, name="D", kind="duration", amount=7, price=200, is_general=True
                    ),
                ],
            )
            callback, edit = await self._run(session, user, "svc:addon:10")
            callback.answer.assert_awaited()
            data = self._button_data(edit)
            self.assertIn("svc:addon:10:volume", data)
            self.assertIn("svc:addon:10:duration", data)
            self.assertTrue(all(not d.startswith("svc:addonpay:") for d in data))

    async def test_single_kind_skips_picker(self) -> None:
        async with self.Session() as session:
            user = await self._seed(
                session,
                packs=[
                    ServiceAddonPack(
                        id=1, name="V", kind="volume", amount=5, price=100, is_general=True
                    ),
                ],
            )
            _callback, edit = await self._run(session, user, "svc:addon:10")
            data = self._button_data(edit)
            self.assertIn("svc:addonpay:10:1", data)
            self.assertNotIn("svc:addon:10:duration", data)

    async def test_kind_list_has_back_to_picker_when_both_exist(self) -> None:
        async with self.Session() as session:
            user = await self._seed(
                session,
                packs=[
                    ServiceAddonPack(
                        id=1, name="V", kind="volume", amount=5, price=100, is_general=True
                    ),
                    ServiceAddonPack(
                        id=2, name="D", kind="duration", amount=7, price=200, is_general=True
                    ),
                ],
            )
            _callback, edit = await self._run(session, user, "svc:addon:10:volume")
            data = self._button_data(edit)
            self.assertIn("svc:addonpay:10:1", data)
            self.assertIn("svc:addon:10", data)
            texts = self._button_texts(edit)
            self.assertTrue(any("حجم" in t or "گیگ" in t for t in texts))
            self.assertFalse(any(t == "همه بسته‌ها" for t in texts))
            self.assertFalse(any(t == "فقط حجم" for t in texts))

    async def test_no_packs_answers_alert(self) -> None:
        async with self.Session() as session:
            user = await self._seed(session, packs=[])
            callback, edit = await self._run(session, user, "svc:addon:10")
            edit.assert_not_awaited()
            self.assertTrue(callback.answer.await_args.kwargs.get("show_alert"))

    async def test_volume_scoped_hides_general_volume_but_duration_picker_remains(self) -> None:
        async with self.Session() as session:
            user = await self._seed(
                session,
                packs=[
                    ServiceAddonPack(
                        id=1,
                        name="general-vol",
                        kind="volume",
                        amount=5,
                        price=100,
                        is_general=True,
                    ),
                    ServiceAddonPack(
                        id=2,
                        name="general-time",
                        kind="duration",
                        amount=7,
                        price=200,
                        is_general=True,
                    ),
                    ServiceAddonPack(
                        id=3,
                        name="plan-vol",
                        kind="volume",
                        amount=10,
                        price=300,
                        plan_id=1,
                        is_general=False,
                    ),
                ],
            )
            _callback, edit = await self._run(session, user, "svc:addon:10")
            data = self._button_data(edit)
            self.assertIn("svc:addon:10:volume", data)
            self.assertIn("svc:addon:10:duration", data)

            _c2, edit_vol = await self._run(session, user, "svc:addon:10:volume")
            vol_data = self._button_data(edit_vol)
            self.assertIn("svc:addonpay:10:3", vol_data)
            self.assertNotIn("svc:addonpay:10:1", vol_data)

            _c3, edit_dur = await self._run(session, user, "svc:addon:10:duration")
            dur_data = self._button_data(edit_dur)
            self.assertIn("svc:addonpay:10:2", dur_data)


if __name__ == "__main__":
    unittest.main()
