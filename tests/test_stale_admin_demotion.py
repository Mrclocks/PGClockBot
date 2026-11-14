"""A user dropped from ADMIN_IDS loses the sticky admin role in the Mini App too, as in the bot."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from app.db.models import Base, BotUser, ResellerProfile, Role
from app.services import miniapp_auth
from app.services.receipt_review import ReviewDenied, mini_reviewer
from app.services.users import demote_stale_admin


def _request() -> Request:
    return Request({
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
        "scheme": "https", "path": "/api/mini/me", "raw_path": b"/api/mini/me", "query_string": b"",
        "headers": [(b"x-telegram-init-data", b"dummy")], "client": ("127.0.0.1", 1), "server": ("t", 443),
    })


class StaleAdminDemotionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.engine.dispose()

    async def _user(self, telegram_id: int, role: str) -> BotUser:
        user = BotUser(telegram_id=telegram_id, referral_code=f"R{telegram_id}", role=role)
        self.session.add(user)
        await self.session.commit()
        return user

    async def _load_mini(self, user: BotUser, *, admin_ids: set[int]) -> BotUser:
        scope = SimpleNamespace(bot_token="", is_shop=False, shop_id=None)
        with patch("app.services.users.get_settings", return_value=SimpleNamespace(admin_ids=admin_ids)), patch.multiple(
            miniapp_auth,
            assert_miniapp_feature_enabled=lambda: None,
            request_scope=AsyncMock(return_value=scope),
            validate_webapp_init_data=lambda *_a, **_k: {"id": user.telegram_id},
            init_data_from_request=lambda _r: "dummy",
            assert_shop_member=AsyncMock(),
            assert_shop_route=lambda *_a, **_k: None,
            assert_mini_force_join=AsyncMock(),
            assert_mini_terms_entry=AsyncMock(),
        ):
            return await miniapp_auth.load_mini_user(self.session, _request())

    async def test_removed_admin_cannot_review_receipts_in_the_mini_app(self) -> None:
        user = await self._user(7001, Role.ADMIN.value)
        loaded = await self._load_mini(user, admin_ids={999})
        self.assertEqual(loaded.role, Role.USER.value)
        with self.assertRaises(ReviewDenied):
            await mini_reviewer(self.session, loaded)

    async def test_listed_admin_keeps_the_role(self) -> None:
        user = await self._user(7002, Role.ADMIN.value)
        loaded = await self._load_mini(user, admin_ids={7002})
        self.assertEqual(loaded.role, Role.ADMIN.value)

    async def test_removed_admin_who_owns_an_active_shop_becomes_a_reseller(self) -> None:
        user = await self._user(7003, Role.ADMIN.value)
        self.session.add(ResellerProfile(user_id=user.id, is_active=True))
        await self.session.commit()
        with patch("app.services.users.get_settings", return_value=SimpleNamespace(admin_ids=set())):
            self.assertTrue(await demote_stale_admin(self.session, user))
        self.assertEqual(user.role, Role.RESELLER.value)

    async def test_ordinary_customers_are_untouched(self) -> None:
        user = await self._user(7004, Role.USER.value)
        with patch("app.services.users.get_settings", return_value=SimpleNamespace(admin_ids=set())):
            self.assertFalse(await demote_stale_admin(self.session, user))
        self.assertEqual(user.role, Role.USER.value)


if __name__ == "__main__":
    unittest.main()
