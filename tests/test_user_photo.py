"""The user card's Telegram photo: fetched with the viewer's own bot, cached per scope, never leaked."""

from __future__ import annotations

import logging
import os
import tempfile
import time
import unittest
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import GetUserProfilePhotos
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.user_pages import register_user_pages
from app.db.models import Base, BotUser, ResellerProfile
from app.services import user_photo
from app.services.formatting import format_money, format_number
from app.services.secret_box import seal_bot_token

ROOT = Path(__file__).resolve().parents[1]
OWNER = {"role": "admin", "org_principal_id": 1, "org_depth": 0, "org_status": "active"}
SHOP_A, SHOP_B, CUSTOMER, PLATFORM_CUSTOMER = 10, 20, 11, 1
SHOP_A_TOKEN, SHOP_B_TOKEN, PLATFORM_TOKEN = "1010:shop-a-secret", "2020:shop-b-secret", "9999:platform-secret"
JPEG = b"\xff\xd8\xff\xe0" + b"photo-bytes"


def shop_staff(shop_id: int) -> dict[str, Any]:
    return {"role": "reseller", "bot_user_id": shop_id, "permissions": ["dashboard"]}


class UserPhotoRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add_all([
            BotUser(id=SHOP_A, telegram_id=5010, referral_code="shopa", role="reseller"),
            BotUser(id=SHOP_B, telegram_id=5020, referral_code="shopb", role="reseller"),
        ])
        await self.session.flush()
        self.session.add_all([
            BotUser(id=CUSTOMER, telegram_id=9011, referral_code="c11", full_name="Customer", reseller_id=SHOP_A),
            BotUser(id=PLATFORM_CUSTOMER, telegram_id=9001, referral_code="c1", full_name="Platform"),
            ResellerProfile(user_id=SHOP_A, is_active=True, bot_token=seal_bot_token(SHOP_A_TOKEN)),
            ResellerProfile(user_id=SHOP_B, is_active=True, bot_token=seal_bot_token(SHOP_B_TOKEN)),
        ])
        await self.session.commit()

        self.cache = tempfile.TemporaryDirectory()
        self.fetch = AsyncMock(return_value=JPEG)
        for target, value in (
            ("app.services.user_photo.photo_root", lambda: Path(self.cache.name)),
            ("app.services.user_photo.fetch_profile_photo", self.fetch),
            ("app.services.user_photo.get_settings", lambda: SimpleNamespace(bot_token=PLATFORM_TOKEN)),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.staff: dict[str, Any] = shop_staff(SHOP_A)
        app = FastAPI()

        async def get_db() -> AsyncIterator[AsyncSession]:
            yield self.session

        async def require_staff() -> dict[str, Any]:
            return self.staff

        env = Environment(loader=FileSystemLoader(ROOT / "app/web/templates"), autoescape=select_autoescape())
        env.filters["num"] = format_number
        env.filters["money"] = format_money

        def render(request: Request, name: str, context: dict[str, Any]) -> HTMLResponse:
            return HTMLResponse(env.get_template(name).render(**context, csrf_token="fake-csrf"))

        register_user_pages(app, render=render, require_admin=require_staff, get_db=get_db)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self.session.close()
        await self.engine.dispose()
        self.cache.cleanup()

    async def test_a_shop_gets_the_photo_its_own_bot_sees(self) -> None:
        response = await self.client.get(f"/users/{CUSTOMER}/photo")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, JPEG)
        self.assertEqual(response.headers["content-type"], "image/jpeg")
        self.assertIn("private", response.headers["cache-control"])
        self.fetch.assert_awaited_once_with(SHOP_A_TOKEN, 9011)
        for secret in (SHOP_A_TOKEN, SHOP_B_TOKEN, PLATFORM_TOKEN):
            self.assertNotIn(secret.encode(), response.content)

    async def test_a_second_open_is_served_from_the_cache(self) -> None:
        await self.client.get(f"/users/{CUSTOMER}/photo")
        response = await self.client.get(f"/users/{CUSTOMER}/photo")
        self.assertEqual(response.content, JPEG)
        self.fetch.assert_awaited_once()

    async def test_another_shop_cannot_read_the_photo(self) -> None:
        await self.client.get(f"/users/{CUSTOMER}/photo")
        self.staff = shop_staff(SHOP_B)
        response = await self.client.get(f"/users/{CUSTOMER}/photo")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.content, b"")
        self.fetch.assert_awaited_once()

    async def test_the_owner_uses_the_platform_bot(self) -> None:
        self.staff = OWNER.copy()
        response = await self.client.get(f"/users/{PLATFORM_CUSTOMER}/photo")
        self.assertEqual(response.status_code, 200)
        self.fetch.assert_awaited_once_with(PLATFORM_TOKEN, 9001)

    async def test_each_bot_keeps_its_own_copy(self) -> None:
        shop_bot = user_photo.PhotoBot(scope=f"s{SHOP_A}", token=SHOP_A_TOKEN)
        platform_bot = user_photo.PhotoBot(scope=user_photo.PLATFORM_SCOPE, token=PLATFORM_TOKEN)
        self.assertEqual(await user_photo.user_photo(shop_bot, bot_user_id=CUSTOMER, telegram_id=9011), JPEG)
        self.fetch.return_value = b""
        self.assertIsNone(
            await user_photo.user_photo(platform_bot, bot_user_id=CUSTOMER, telegram_id=9011),
            "one shop's cached photo is never served to another scope",
        )
        self.assertEqual(self.fetch.await_args_list[-1].args, (PLATFORM_TOKEN, 9011))
        self.assertNotIn(SHOP_A_TOKEN, repr(shop_bot))

    async def test_no_photo_is_remembered_for_a_day(self) -> None:
        self.fetch.return_value = b""
        first = await self.client.get(f"/users/{CUSTOMER}/photo")
        second = await self.client.get(f"/users/{CUSTOMER}/photo")
        self.assertEqual((first.status_code, second.status_code), (404, 404))
        self.fetch.assert_awaited_once()

    async def test_a_telegram_failure_is_not_cached_and_the_token_is_not_logged(self) -> None:
        self.fetch.side_effect = TelegramNetworkError(
            method=GetUserProfilePhotos(user_id=9011), message=f"https://api.telegram.org/file/bot{SHOP_A_TOKEN}/x",
        )
        # Alembic's fileConfig (run by earlier migration tests) disables loggers that already exist.
        logging.getLogger("app.services.user_photo").disabled = False
        with self.assertLogs("app.services.user_photo", level="WARNING") as logs:
            first = await self.client.get(f"/users/{CUSTOMER}/photo")
        self.assertEqual(first.status_code, 404)
        self.assertNotIn(SHOP_A_TOKEN, "\n".join(logs.output))
        self.fetch.side_effect = None
        second = await self.client.get(f"/users/{CUSTOMER}/photo")
        self.assertEqual(second.status_code, 200, "the next open asks Telegram again")

    async def test_a_day_old_copy_is_fetched_again(self) -> None:
        await self.client.get(f"/users/{CUSTOMER}/photo")
        cached = user_photo.cache_path(f"s{SHOP_A}", CUSTOMER)
        old = time.time() - user_photo.CACHE_SECONDS - 60
        os.utime(cached, (old, old))
        await self.client.get(f"/users/{CUSTOMER}/photo")
        self.assertEqual(self.fetch.await_count, 2)

    async def test_an_unknown_user_is_not_found_without_asking_telegram(self) -> None:
        response = await self.client.get("/users/999/photo")
        self.assertEqual(response.status_code, 404)
        self.fetch.assert_not_awaited()

    async def test_the_card_shows_the_initial_and_points_at_the_scoped_photo(self) -> None:
        response = await self.client.get(f"/users/{CUSTOMER}/card")
        self.assertIn(f'src="/users/{CUSTOMER}/photo"', response.text)
        self.assertIn("<span>C</span>", response.text)


class FakeBot:
    """Stands in for aiogram's Bot: one photo in three sizes, downloads return ``payload``."""

    payload = JPEG
    profile_error: Exception | None = None

    def __init__(self, token: str) -> None:
        self.session = SimpleNamespace(close=AsyncMock())
        self.downloaded: list[str] = []

    async def get_user_profile_photos(self, user_id: int, **_: Any) -> SimpleNamespace:
        if self.profile_error:
            raise self.profile_error
        sizes = [SimpleNamespace(file_id=f"f{side}", width=side, height=side, file_size=side * 10) for side in (640, 160, 320)]
        return SimpleNamespace(photos=[sizes])

    async def get_file(self, file_id: str, **_: Any) -> SimpleNamespace:
        return SimpleNamespace(file_path=f"photos/{file_id}.jpg")

    async def download_file(self, file_path: str, *, destination: Any, **_: Any) -> None:
        destination.write(self.payload)


class FetchProfilePhotoTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        FakeBot.payload, FakeBot.profile_error = JPEG, None
        patcher = patch("app.services.user_photo.Bot", FakeBot)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_the_smallest_size_wide_enough_for_the_card_is_downloaded(self) -> None:
        with patch.object(FakeBot, "get_file", AsyncMock(return_value=SimpleNamespace(file_path="p.jpg"))) as get_file:
            data = await user_photo.fetch_profile_photo("t", 1)
        self.assertEqual(data, JPEG)
        self.assertEqual(get_file.await_args.args[0], "f160")

    async def test_a_download_that_is_not_a_jpeg_counts_as_no_photo(self) -> None:
        FakeBot.payload = b"<svg onload=alert(1)>"
        self.assertEqual(await user_photo.fetch_profile_photo("t", 1), b"")

    async def test_a_user_telegram_does_not_know_counts_as_no_photo(self) -> None:
        FakeBot.profile_error = TelegramBadRequest(method=GetUserProfilePhotos(user_id=1), message="user not found")
        self.assertEqual(await user_photo.fetch_profile_photo("t", 1), b"")


class PrunePhotoCacheTests(unittest.TestCase):
    def test_copies_not_refreshed_for_a_week_are_removed(self) -> None:
        with tempfile.TemporaryDirectory() as root, patch("app.services.user_photo.photo_root", lambda: Path(root)):
            stale, fresh = user_photo.cache_path("s10", 1), user_photo.cache_path("main", 2)
            for path in (stale, fresh):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(JPEG)
            old = time.time() - user_photo.PRUNE_AFTER_SECONDS - 60
            os.utime(stale, (old, old))
            self.assertEqual(user_photo.prune_photo_cache(), 1)
            self.assertFalse(stale.exists())
            self.assertTrue(fresh.exists())


if __name__ == "__main__":
    unittest.main()
