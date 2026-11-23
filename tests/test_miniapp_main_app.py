"""The «Open» button beside the bot in Telegram search: BotFather guide, status and the browser fallback.

The panel shows the exact address to paste in BotFather (each shop its own), reads the status
from getMe with a short cache and never logs the token; the Mini App page carries only the
bot's public username so a browser visit can open the Mini App in Telegram.
"""

from __future__ import annotations

import logging
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import GetMe
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.miniapp_pages import register_miniapp_pages
from app.api.shop_settings import _miniapp_switch_context
from app.db.models import Base, BotUser, ResellerProfile
from app.services import miniapp_main_app
from app.services.miniapp_main_app import (
    MainAppGuide,
    clean_bot_username,
    main_app_guide,
    platform_bot_username,
)
from app.services.miniapp_shop import PLATFORM_SWITCH, SHOP_SWITCH, new_shop_key
from app.services.secret_box import seal_bot_token
from app.services.users import clear_settings_cache, set_setting
from tests.postgres_support import scoped_async_engine

TOKEN = "123456:AAH-secret-token-value"
SHOP_TOKEN = "222222:shop-secret-token"
URL = "https://panel.example.com/miniapp/"
PERMS = '["orders","payments","miniapp"]'


def me(*, has_main: bool, username: str = "PlatformBot") -> SimpleNamespace:
    return SimpleNamespace(username=username, has_main_web_app=has_main)


class MainAppStatusTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        miniapp_main_app._cache.clear()
        self.clock = [1000.0]
        self.clock_patch = patch("app.services.miniapp_main_app.time.monotonic", lambda: self.clock[0])
        self.clock_patch.start()

    def tearDown(self) -> None:
        self.clock_patch.stop()
        miniapp_main_app._cache.clear()

    def get_me(self, *results):
        return patch("aiogram.Bot.get_me", AsyncMock(side_effect=list(results)))

    async def test_status_and_address_come_from_telegram_and_settings(self) -> None:
        with self.get_me(me(has_main=True)):
            guide = await main_app_guide(TOKEN, url=URL)
        self.assertEqual(guide, MainAppGuide(url=URL, bot_username="PlatformBot", enabled=True))
        self.assertEqual(guide.open_link, "https://t.me/PlatformBot?startapp")

    async def test_on_is_cached_and_off_is_read_again_soon(self) -> None:
        with self.get_me(me(has_main=False), me(has_main=True), me(has_main=False)) as get_me:
            self.assertFalse((await main_app_guide(TOKEN, url=URL)).enabled)
            self.clock[0] += miniapp_main_app.OFF_TTL_SEC + 1
            self.assertTrue((await main_app_guide(TOKEN, url=URL)).enabled, "turned on in BotFather")
            self.clock[0] += miniapp_main_app.OFF_TTL_SEC + 1
            self.assertTrue((await main_app_guide(TOKEN, url=URL)).enabled, "still cached")
        self.assertEqual(get_me.await_count, 2)

    async def test_an_unreachable_telegram_is_unknown_and_never_logs_the_token(self) -> None:
        error = TelegramNetworkError(method=GetMe(), message=f"cannot reach https://api.telegram.org/bot{TOKEN}/getMe")
        with self.get_me(error), self.assertLogs("app.services.miniapp_main_app", logging.DEBUG) as logs:
            guide = await main_app_guide(TOKEN, url=URL, known_username="@SavedName")
        self.assertEqual((guide.enabled, guide.bot_username), (None, "SavedName"))
        self.assertNotIn(TOKEN.split(":")[1], "\n".join(logs.output))
        with self.get_me(me(has_main=True)) as get_me:
            await main_app_guide(TOKEN, url=URL)
        self.assertEqual(get_me.await_count, 1, "a failure is not cached")

    async def test_no_token_malformed_token_or_plain_http_address(self) -> None:
        self.assertEqual(await main_app_guide("", url=URL), MainAppGuide(url=URL, bot_username="", enabled=None))
        self.assertIsNone((await main_app_guide("not-a-token", url=URL)).enabled)
        with self.get_me(me(has_main=True)):
            self.assertEqual((await main_app_guide(TOKEN, url="http://panel.example.com/miniapp/")).url, "")

    async def test_the_cache_stays_bounded(self) -> None:
        with patch("aiogram.Bot.get_me", AsyncMock(return_value=me(has_main=True))):
            for i in range(miniapp_main_app.CACHE_MAX_ENTRIES + 20):
                await main_app_guide(f"{100000 + i}:token", url=URL)
        self.assertEqual(len(miniapp_main_app._cache), miniapp_main_app.CACHE_MAX_ENTRIES)

    def test_only_real_bot_usernames_are_used(self) -> None:
        self.assertEqual(clean_bot_username("@My_Bot1"), "My_Bot1")
        for bad in ("", None, "ab", "1bot", "bad name", "<script>", "a" * 40, "bot/../x"):
            with self.subTest(bad=bad):
                self.assertEqual(clean_bot_username(bad), "")

    def test_the_page_username_never_calls_telegram(self) -> None:
        settings = SimpleNamespace(bot_username="", bot_token=TOKEN)
        with patch("app.services.miniapp_main_app.get_settings", return_value=settings), \
                patch("aiogram.Bot.get_me", AsyncMock(side_effect=AssertionError("no network on the page"))):
            self.assertEqual(platform_bot_username(), "")
            miniapp_main_app._remember(miniapp_main_app._cache_key(TOKEN), miniapp_main_app._BotFacts("FromCache", True))
            self.assertEqual(platform_bot_username(), "FromCache")
            settings.bot_username = "@Configured_Bot"
            self.assertEqual(platform_bot_username(), "Configured_Bot")


class GuideCardTests(unittest.TestCase):
    def render(self, guide: MainAppGuide | None, *, shop_mode: bool = False) -> str:
        from app.api.app import templates

        return templates.get_template("_miniapp_main_app_guide.html").render(main_app=guide, shop_mode=shop_mode)

    def test_the_card_shows_the_address_to_copy_and_the_status(self) -> None:
        html = self.render(MainAppGuide(url=URL, bot_username="PlatformBot", enabled=False))
        self.assertIn(f'data-copy="{URL}"', html)
        self.assertIn("@PlatformBot", html)
        self.assertIn("خاموش", html)
        self.assertIn("https://t.me/PlatformBot?startapp", html)
        self.assertIn("روشن", self.render(MainAppGuide(url=URL, bot_username="b_bot", enabled=True)))
        self.assertIn("نامشخص", self.render(MainAppGuide(url=URL, bot_username="", enabled=None)))

    def test_without_https_the_card_says_what_is_missing(self) -> None:
        html = self.render(MainAppGuide(url="", bot_username="PlatformBot", enabled=None))
        self.assertNotIn("data-copy", html)
        self.assertIn("HTTPS", html)

    def test_a_shop_is_warned_not_to_paste_the_platform_address(self) -> None:
        guide = MainAppGuide(url=URL + "s/abc/", bot_username="ShopBot", enabled=False)
        self.assertIn("آدرس مینی‌اپ پلتفرم را در ربات فروشگاه وارد نکنید", self.render(guide, shop_mode=True))
        self.assertNotIn("آدرس مینی‌اپ پلتفرم را در ربات فروشگاه وارد نکنید", self.render(guide))

    def test_no_guide_renders_nothing(self) -> None:
        self.assertEqual(self.render(None).strip(), "")


class ShopAndPageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        clear_settings_cache()
        self.key, self.other_key = new_shop_key(), new_shop_key()
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        now = datetime.now(timezone.utc)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=5, telegram_id=5005, referral_code="o5", role="reseller"),
                BotUser(id=6, telegram_id=6006, referral_code="o6", role="reseller"),
                ResellerProfile(id=1, user_id=5, is_active=True, setup_completed_at=now, bot_token=seal_bot_token(SHOP_TOKEN),
                                miniapp_key=self.key, web_permissions=PERMS, bot_username="Shop5Bot"),
                ResellerProfile(id=2, user_id=6, is_active=True, setup_completed_at=now, bot_token=seal_bot_token("333333:x"),
                                miniapp_key=self.other_key, web_permissions=PERMS, bot_username="bad name<"),
            ])
            await session.commit()
            await set_setting(session, PLATFORM_SWITCH, "1")
            await set_setting(session, SHOP_SWITCH, "1", reseller_id=5)
            await set_setting(session, SHOP_SWITCH, "1", reseller_id=6)
        clear_settings_cache()
        settings = SimpleNamespace(miniapp_enabled=True, miniapp_url=URL, bot_token=TOKEN, bot_username="PlatformBot")
        self.patches = [
            patch("app.services.miniapp_shop.get_settings", return_value=settings),
            patch("app.services.miniapp_auth.get_settings", return_value=settings),
            patch("app.services.miniapp_main_app.get_settings", return_value=settings),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()
        clear_settings_cache()
        await self.engine.dispose()

    async def test_a_shop_gets_its_own_address_and_is_checked_with_its_own_bot(self) -> None:
        guide = AsyncMock(return_value=MainAppGuide(url="", bot_username="", enabled=None))
        async with self.Session() as session:
            profile = await session.get(ResellerProfile, 1)
            with patch("app.api.shop_settings.main_app_guide", guide):
                ctx = await _miniapp_switch_context(session, profile)
        self.assertIs(ctx["main_app"], guide.return_value)
        self.assertEqual(guide.await_args.args, (SHOP_TOKEN,))
        self.assertEqual(guide.await_args.kwargs, {"url": f"{URL}s/{self.key}/", "known_username": "Shop5Bot"})

    async def test_no_guide_while_the_shop_mini_app_is_off(self) -> None:
        async with self.Session() as session:
            await set_setting(session, SHOP_SWITCH, "0", reseller_id=5)
            clear_settings_cache()
            profile = await session.get(ResellerProfile, 1)
            with patch("app.api.shop_settings.main_app_guide", AsyncMock(side_effect=AssertionError("not asked"))):
                self.assertIsNone((await _miniapp_switch_context(session, profile))["main_app"])

    async def test_each_page_names_its_own_bot_and_nothing_unsafe(self) -> None:
        seen: list[dict] = []

        def render(_request, _name, ctx):
            seen.append(ctx)
            return HTMLResponse("page")

        async def get_db():
            async with self.Session() as session:
                yield session

        app = FastAPI()
        register_miniapp_pages(app, render=render, get_db=get_db)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for path in ("/miniapp/", f"/miniapp/s/{self.key}/", f"/miniapp/s/{self.other_key}/"):
                self.assertEqual((await client.get(path)).status_code, 200, path)
        self.assertEqual([ctx["mini_bot_username"] for ctx in seen], ["PlatformBot", "Shop5Bot", ""])


if __name__ == "__main__":
    unittest.main()
