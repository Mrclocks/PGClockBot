"""User hard-delete vs bot auto-recreate (get_or_create).

Scenario reported: delete in panel → still seen in bot → back in panel.
That can happen WITHOUT a failed delete: any Telegram update from the same
telegram_id runs middleware get_or_create_user and inserts a fresh BotUser.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from tests.postgres_support import scoped_async_engine


class DeleteBotUserBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import Base
        import app.db.models  # noqa: F401

        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _make_user(self, session, *, tid: int = 900001, wallet: int = 5000):
        from app.db.models import BotUser, Role

        u = BotUser(
            telegram_id=tid,
            username="doomed",
            full_name="کاربر تست",
            role=Role.USER.value,
            referral_code=f"T{tid}",
            wallet_balance=wallet,
        )
        session.add(u)
        await session.commit()
        await session.refresh(u)
        return u

    async def test_delete_removes_row_from_db(self):
        from app.db.models import BotUser
        from app.services.users import delete_bot_user

        async with self.Session() as session:
            user = await self._make_user(session, tid=910001, wallet=12000)
            uid = user.id
            tid = user.telegram_id

            with patch("app.services.users.get_settings") as gs:
                gs.return_value.admin_ids = []
                info = await delete_bot_user(session, uid)

            self.assertEqual(info["user_id"], uid)
            self.assertEqual(info["telegram_id"], tid)

            gone = await session.get(BotUser, uid)
            self.assertIsNone(gone)
            by_tg = (
                await session.execute(select(BotUser).where(BotUser.telegram_id == tid))
            ).scalar_one_or_none()
            self.assertIsNone(by_tg)

    async def test_bot_message_after_delete_recreates_fresh_user(self):
        """This is the path that looks like 'delete failed' in the panel."""
        from app.db.models import BotUser, Role
        from app.services.users import delete_bot_user, get_or_create_user

        async with self.Session() as session:
            user = await self._make_user(session, tid=920002, wallet=77777)
            old_id = user.id
            tid = user.telegram_id

            # Occupy the next id so the recreated user cannot reuse old_id
            filler = BotUser(
                telegram_id=920099,
                role=Role.USER.value,
                referral_code="FILL099",
                wallet_balance=0,
            )
            session.add(filler)
            await session.commit()
            await session.refresh(filler)
            self.assertGreater(filler.id, old_id)

            with patch("app.services.users.get_settings") as gs:
                gs.return_value.admin_ids = []
                await delete_bot_user(session, old_id)

            self.assertIsNone(await session.get(BotUser, old_id))

            with patch("app.services.users.get_settings") as gs:
                gs.return_value.admin_ids = []
                revived = await get_or_create_user(
                    session,
                    tid,
                    username="doomed",
                    full_name="کاربر تست",
                )

            self.assertIsNotNone(revived.id)
            self.assertNotEqual(revived.id, old_id)
            self.assertEqual(revived.telegram_id, tid)
            # New account — not the old wallet / history
            self.assertEqual(int(revived.wallet_balance or 0), 0)

            still_old = await session.get(BotUser, old_id)
            self.assertIsNone(still_old)

            rows = list(
                (
                    await session.execute(select(BotUser).where(BotUser.telegram_id == tid))
                ).scalars().all()
            )
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].id, revived.id)

    async def test_delete_value_error_keeps_user(self):
        from app.db.models import BotUser
        from app.services.users import delete_bot_user

        async with self.Session() as session:
            user = await self._make_user(session, tid=930003)
            with patch("app.services.users.get_settings") as gs:
                gs.return_value.admin_ids = [user.telegram_id]
                with self.assertRaises(ValueError):
                    await delete_bot_user(session, user.id)
            kept = await session.get(BotUser, user.id)
            self.assertIsNotNone(kept)

    async def test_web_handler_only_oks_after_delete(self):
        """Success flash path is after delete_bot_user returns (post-commit)."""
        api = Path("app/api/user_list_pages.py").read_text(encoding="utf-8")
        block = api.split("async def users_delete", 1)[1].split(
            "async def menu_layout_page", 1
        )[0]
        self.assertIn("await delete_bot_user(", block)
        self.assertLess(
            block.find("await delete_bot_user("),
            block.find('ok=f"کاربر'),
        )
        self.assertIn("friendly_user_delete_error", block)
        # Notify runs before delete — outbound only; does not recreate via middleware
        self.assertLess(
            block.find("notify_account_edit"),
            block.find("await delete_bot_user("),
        )

    def test_middleware_always_get_or_create(self):
        mw = Path("app/bot/middlewares.py").read_text(encoding="utf-8")
        self.assertIn("get_or_create_user(", mw)


class DeleteConfirmCopyTests(unittest.TestCase):
    def test_delete_confirm_mentions_bot_recreate(self):
        users = Path("app/web/templates/users.html").read_text(encoding="utf-8")
        edit = Path("app/web/templates/_user_edit_body.html").read_text(encoding="utf-8")
        needle = "اگر دوباره به ربات پیام بدهد"
        self.assertIn(needle, users)
        self.assertIn(needle, edit)


if __name__ == "__main__":
    unittest.main()
