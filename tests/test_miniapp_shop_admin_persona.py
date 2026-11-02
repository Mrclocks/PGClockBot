"""A platform admin who is also a shop's customer can use that shop's Mini App.

``/me`` in a shop's Mini App shows everyone as the shop's customer, so its screens must not
turn a platform admin away by their platform role. The platform Mini App still keeps admins
out of commerce, and a shop still admits only its own customers and staff.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.miniapp_feeds import register_miniapp_feeds
from app.api.miniapp_pages import register_miniapp_pages
from app.db.models import Base, BotUser, BotUserShop, ResellerProfile
from app.services.miniapp_shop import NOT_A_CUSTOMER, PLATFORM_SWITCH, SHOP_SWITCH
from app.services.secret_box import seal_bot_token
from app.services.users import clear_settings_cache, set_setting

MAIN_TOKEN = "100:MAIN"
SHOP_TOKEN = "200:SHOP"
OTHER_SHOP_TOKEN = "300:OTHER"
SHOP, OTHER_SHOP = 5, 6
KEY, OTHER_KEY = "k" * 22, "o" * 22
ADMIN_TG = 7001
PERMS = '["miniapp"]'


def signed(token: str, telegram_id: int) -> str:
    fields = {"auth_date": str(int(time.time())), "user": json.dumps({"id": telegram_id}, separators=(",", ":"))}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class ShopAdminPersonaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        clear_settings_cache()
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(self.engine, expire_on_commit=False)
        now = datetime.now(timezone.utc)
        async with Session() as session:
            session.add_all([
                BotUser(id=SHOP, telegram_id=5005, referral_code="o5", role="reseller"),
                BotUser(id=OTHER_SHOP, telegram_id=6006, referral_code="o6", role="reseller"),
                BotUser(id=20, telegram_id=ADMIN_TG, referral_code="a20", role="admin"),
                BotUserShop(bot_user_id=20, shop_id=SHOP),
                ResellerProfile(
                    id=1, user_id=SHOP, is_active=True, setup_completed_at=now,
                    bot_token=seal_bot_token(SHOP_TOKEN), miniapp_key=KEY, web_permissions=PERMS,
                ),
                ResellerProfile(
                    id=2, user_id=OTHER_SHOP, is_active=True, setup_completed_at=now,
                    bot_token=seal_bot_token(OTHER_SHOP_TOKEN), miniapp_key=OTHER_KEY, web_permissions=PERMS,
                ),
            ])
            await session.commit()
            await set_setting(session, PLATFORM_SWITCH, "1")
            for shop in (SHOP, OTHER_SHOP):
                await set_setting(session, SHOP_SWITCH, "1", reseller_id=shop)
        clear_settings_cache()
        settings = SimpleNamespace(miniapp_enabled=True, bot_token=MAIN_TOKEN, currency="تومان")
        self.patches = [patch("app.services.miniapp_auth.get_settings", return_value=settings)]
        for p in self.patches:
            p.start()

        async def get_db():
            async with Session() as session:
                yield session

        app = FastAPI()
        register_miniapp_pages(app, render=lambda *_a, **_k: HTMLResponse("page"), get_db=get_db)
        register_miniapp_feeds(app, get_db=get_db)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        for p in reversed(self.patches):
            p.stop()
        clear_settings_cache()
        await self.engine.dispose()

    async def get(self, path: str, *, token: str, key: str | None = None) -> httpx.Response:
        headers = {"X-Telegram-Init-Data": signed(token, ADMIN_TG)}
        if key:
            headers["X-Mini-Shop"] = key
        return await self.client.get(path, headers=headers)

    async def test_platform_admin_who_is_a_shop_customer_uses_the_shop_screens(self) -> None:
        for path in ("/api/mini/plans", "/api/mini/activity", "/api/mini/services"):
            with self.subTest(path=path):
                response = await self.get(path, token=SHOP_TOKEN, key=KEY)
                self.assertEqual(response.status_code, 200, response.text)

    async def test_platform_mini_app_still_keeps_admins_out_of_commerce(self) -> None:
        response = await self.get("/api/mini/plans", token=MAIN_TOKEN)
        self.assertEqual(response.status_code, 403)

    async def test_a_shop_the_admin_is_not_a_customer_of_still_refuses_them(self) -> None:
        response = await self.get("/api/mini/plans", token=OTHER_SHOP_TOKEN, key=OTHER_KEY)
        self.assertEqual((response.status_code, response.json()["detail"]), (403, NOT_A_CUSTOMER))


if __name__ == "__main__":
    unittest.main()
