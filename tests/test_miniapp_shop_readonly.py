"""A shop's Mini App, first sections: its services, purse, transactions and guides, nothing else."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.miniapp_codes import register_miniapp_codes
from app.api.miniapp_feeds import register_miniapp_feeds
from app.api.miniapp_guides import register_miniapp_guides
from app.api.miniapp_pages import register_miniapp_pages
from app.db.models import Base, BotUser, ResellerProfile, UserService
from app.services import miniapp_shop
from app.services.connection_guides import SETTING_KEY as GUIDES_KEY
from app.services.guide_delivery import _recent_sends
from app.services.miniapp_shop import (
    PLATFORM_SWITCH,
    ROUTE_UNAVAILABLE,
    SHOP_SWITCH,
    new_shop_key,
    set_shop_miniapp,
    shop_menu_url,
)
from app.services.secret_box import seal_bot_token
from app.services.users import clear_settings_cache, set_setting
from app.services.wallet import credit_wallet
from tests.test_miniapp_shop_scope import MAIN_TOKEN, OTHER_SHOP_TOKEN, SHOP_TOKEN, signed

SHOP, OTHER_SHOP = 5, 6
KEY, OTHER_KEY = new_shop_key(), new_shop_key()
CUSTOMER = 1010
PERMS = '["orders","payments","miniapp"]'


def service(service_id: int, shop: int | None, name: str) -> UserService:
    return UserService(
        id=service_id, bot_user_id=10, pg_username=name, shop_id=shop,
        subscription_url=f"https://sub.example.com/sub/{name}-token",
    )


class ShopFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        clear_settings_cache()
        _recent_sends.clear()
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        now = datetime.now(timezone.utc)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=SHOP, telegram_id=5005, referral_code="o5", role="reseller"),
                BotUser(id=OTHER_SHOP, telegram_id=6006, referral_code="o6", role="reseller"),
                BotUser(id=10, telegram_id=CUSTOMER, referral_code="c10", role="user", reseller_id=SHOP),
                ResellerProfile(
                    id=1, user_id=SHOP, is_active=True, setup_completed_at=now, bot_token=seal_bot_token(SHOP_TOKEN),
                    miniapp_key=KEY, web_permissions=PERMS, bot_username="shop5bot",
                ),
                ResellerProfile(
                    id=2, user_id=OTHER_SHOP, is_active=True, setup_completed_at=now,
                    bot_token=seal_bot_token(OTHER_SHOP_TOKEN), miniapp_key=OTHER_KEY, web_permissions=PERMS,
                ),
                service(101, SHOP, "mine_in_shop"), service(102, None, "mine_on_platform"),
                service(103, OTHER_SHOP, "mine_in_other_shop"),
            ])
            await session.commit()
            user = await session.get(BotUser, 10)
            await credit_wallet(session, user, 50_000, reason="platform_topup")
            await credit_wallet(session, user, 7_000, reason="shop_gift", shop_id=SHOP)
            await credit_wallet(session, user, 999, reason="other_shop_gift", shop_id=OTHER_SHOP)
            await set_setting(session, PLATFORM_SWITCH, "1")
            await set_setting(session, SHOP_SWITCH, "1", reseller_id=SHOP)
            await set_setting(session, SHOP_SWITCH, "1", reseller_id=OTHER_SHOP)
            await set_setting(session, "bot_tg_name", "فروشگاه <b>نمونه</b>", reseller_id=SHOP)
            await set_setting(session, GUIDES_KEY, json.dumps([
                {"id": "shopguide", "title": "راهنمای فروشگاه", "body": "متن", "audience": "user"},
            ]), reseller_id=SHOP)
        clear_settings_cache()
        settings = SimpleNamespace(miniapp_enabled=True, bot_token=MAIN_TOKEN, currency="تومان",
                                   miniapp_url="https://panel.example.com/miniapp/")
        self.patches = [
            patch("app.services.miniapp_auth.get_settings", return_value=settings),
            patch("app.api.miniapp_pages.get_settings", return_value=settings),
            patch("app.services.miniapp_shop.get_settings", return_value=settings),
            patch("app.api.miniapp_pages._fetch_pg_info", AsyncMock(return_value={})),
        ]
        for p in self.patches:
            p.start()

        async def get_db():
            async with self.Session() as session:
                yield session

        app = FastAPI()
        register_miniapp_pages(app, render=lambda *_a, **_k: HTMLResponse("page"), get_db=get_db)
        register_miniapp_feeds(app, get_db=get_db)
        register_miniapp_guides(app, get_db=get_db)
        self.register_extra_routes(app, get_db)
        register_miniapp_codes(app, get_db=get_db)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    def register_extra_routes(self, app: FastAPI, get_db) -> None:
        """Subclasses add the Mini App routes they test."""

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        for p in reversed(self.patches):
            p.stop()
        clear_settings_cache()
        await self.engine.dispose()

    async def shop_get(self, path: str, *, key: str = KEY, token: str = SHOP_TOKEN) -> httpx.Response:
        headers = {"X-Telegram-Init-Data": signed(token, CUSTOMER), "X-Mini-Shop": key}
        return await self.client.get(path, headers=headers)


class ShopReadOnlyTests(ShopFixture):
    async def test_me_shows_the_shop_purse_services_and_open_sections_only(self) -> None:
        me = (await self.shop_get("/api/mini/me")).json()
        self.assertEqual(me["persona"], "user")
        self.assertEqual([n["id"] for n in me["nav"]], ["home", "services", "shop", "account"])
        self.assertEqual(me["sections"], ["services", "guides", "activity", "shop", "topup", "support", "codes", "club"])
        self.assertEqual(me["shop"]["name"], "فروشگاه <b>نمونه</b>", "escaped by the page, not the API")
        self.assertEqual((me["customer"]["wallet"], me["user"]["wallet"]), (7_000, 7_000))
        self.assertEqual([s["id"] for s in me["customer"]["home"]["recent"]], [101])
        self.assertEqual(me["panel_base"], "")

    async def test_services_and_details_are_the_shops_only(self) -> None:
        items = (await self.shop_get("/api/mini/services")).json()["items"]
        self.assertEqual([row["id"] for row in items], [101])
        self.assertEqual((await self.shop_get("/api/mini/service/101")).status_code, 200)
        for foreign in (102, 103, 999):
            with self.subTest(service=foreign):
                self.assertEqual((await self.shop_get(f"/api/mini/service/{foreign}")).status_code, 404)
                self.assertEqual((await self.shop_get(f"/api/mini/service/{foreign}/qr")).status_code, 404)
        self.assertEqual((await self.shop_get("/api/mini/service/101/qr")).status_code, 200)

    async def test_other_shop_sees_its_own_data_with_its_own_bot(self) -> None:
        response = await self.shop_get("/api/mini/me", key=OTHER_KEY, token=OTHER_SHOP_TOKEN)
        self.assertEqual(response.status_code, 403, "a customer of shop 5 is not shop 6's")

    async def test_activity_lists_the_shop_purse_only(self) -> None:
        items = (await self.shop_get("/api/mini/activity")).json()["items"]
        self.assertEqual([row["reason"] for row in items], ["shop_gift"])

    async def test_guides_are_the_shops_and_sent_through_its_bot(self) -> None:
        guides = (await self.shop_get("/api/mini/guides")).json()["guides"]
        self.assertEqual([g["id"] for g in guides], ["shopguide"])
        bot = SimpleNamespace(id=222222, session=SimpleNamespace(close=AsyncMock()))
        opener = AsyncMock(return_value=(bot, False))
        sender = AsyncMock()
        with patch("app.api.miniapp_guides.open_notify_bot_for_user", opener), \
                patch("app.api.miniapp_guides.send_guide_to_chat", sender):
            headers = {"X-Telegram-Init-Data": signed(SHOP_TOKEN, CUSTOMER), "X-Mini-Shop": KEY}
            self.assertEqual((await self.client.post("/api/mini/guides/v2box/send", headers=headers)).status_code, 404)
            response = await self.client.post("/api/mini/guides/shopguide/send", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(opener.await_args.kwargs["shop_id"], SHOP)
        self.assertEqual(sender.await_args.kwargs["shop_id"], SHOP)

    async def test_topup_support_club_and_service_changes_stay_closed(self) -> None:
        for path in ("/api/mini/service/101/automation", "/api/mini/service/101/cancellation", "/api/mini/ops"):
            with self.subTest(path=path):
                response = await self.shop_get(path)
                self.assertEqual((response.status_code, response.json()["detail"]), (404, ROUTE_UNAVAILABLE))

    async def test_platform_view_is_unchanged(self) -> None:
        headers = {"X-Telegram-Init-Data": signed(MAIN_TOKEN, CUSTOMER)}
        me = (await self.client.get("/api/mini/me", headers=headers)).json()
        self.assertEqual(me["customer"]["wallet"], 50_000)
        self.assertNotIn("sections", me)
        self.assertEqual([s["id"] for s in me["customer"]["home"]["recent"]], [102])


class ShopSwitchTests(ShopFixture):
    async def test_the_shop_switch_needs_the_platform_switch_and_the_permission(self) -> None:
        async with self.Session() as session:
            await set_setting(session, PLATFORM_SWITCH, "0")
            clear_settings_cache()
            profile = await session.get(ResellerProfile, 2)
            profile.miniapp_key = None
            await session.commit()
            with self.assertRaises(ValueError):
                await set_shop_miniapp(session, profile, enabled=True)
            self.assertIsNone(profile.miniapp_key, "no key is made for a refused switch")
            await set_setting(session, PLATFORM_SWITCH, "1")
            clear_settings_cache()
            await set_shop_miniapp(session, profile, enabled=True)
            self.assertTrue(miniapp_shop.KEY_PATTERN.fullmatch(profile.miniapp_key))
            clear_settings_cache()
            self.assertEqual(
                await shop_menu_url(session, profile),
                f"https://panel.example.com/miniapp/s/{profile.miniapp_key}/",
            )
            await set_shop_miniapp(session, profile, enabled=False)
            clear_settings_cache()
            self.assertEqual(await shop_menu_url(session, profile), "")

    async def test_turning_off_the_platform_switch_closes_every_shop(self) -> None:
        async with self.Session() as session:
            await set_setting(session, PLATFORM_SWITCH, "0")
        clear_settings_cache()
        self.assertEqual((await self.shop_get("/api/mini/me")).status_code, 404)


if __name__ == "__main__":
    unittest.main()
