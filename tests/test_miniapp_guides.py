"""Mini App connection guides: which guides are shown, what leaves the server, sending to chat."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.miniapp_guides import register_miniapp_guides
from app.db.models import Base, BotUser
from app.services import guide_delivery
from app.services.users import reset_shop_reseller_id, set_shop_reseller_id

GUIDES = [
    {"id": "v2box", "title": "V2Box", "body": "<b>۱)</b> نصب کنید &amp; وصل شوید", "audience": "user", "deep_link": True,
     "links": [{"label": "ویدیو", "url": "https://youtu.be/abc"}, {"label": "bad", "url": "javascript:alert(1)"}],
     "media": [{"id": "aaaaaaaaaaaa", "kind": "photo", "tg": {"777": "TG_FILE_ID_SECRET"}},
               {"id": "bbbbbbbbbbbb", "kind": "video", "file": "0123456789abcdef0123456789abcdef.mp4"}]},
    {"id": "off", "title": "Disabled", "body": "x", "audience": "user", "enabled": False},
    {"id": "panel", "title": "Reseller panel", "body": "x", "audience": "reseller"},
]


class FakeBot:
    id = 777

    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []
        self.photos: list[int] = []
        self.session = SimpleNamespace(close=AsyncMock())

    async def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))

    async def send_photo(self, chat_id, photo):
        self.photos.append(chat_id)
        return SimpleNamespace(video=None, photo=[SimpleNamespace(file_id="p1")])


class MiniAppGuidesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add(BotUser(id=1, telegram_id=101, referral_code="a", role="user"))
        await self.session.commit()
        self.shop_token = set_shop_reseller_id(None)
        guide_delivery._recent_sends.clear()
        self.bot = FakeBot()
        self.raw = json.dumps(GUIDES)

        async def load_user(*_args):
            return await self.session.scalar(select(BotUser).where(BotUser.id == 1))

        async def get_setting(_session, key, default="", **kwargs):
            return self.raw if key == "connection_guides" and kwargs.get("reseller_id") is None else default

        self.patches = [
            patch("app.api.miniapp_guides.load_mini_user", AsyncMock(side_effect=load_user)),
            patch("app.services.connection_guides.get_setting", AsyncMock(side_effect=get_setting)),
            patch("app.services.guide_media.get_setting", AsyncMock(return_value="")),
            patch("app.services.guide_media.set_setting", AsyncMock()),
            patch("app.api.miniapp_guides.open_notify_bot_for_user", AsyncMock(return_value=(self.bot, True))),
        ]
        for p in self.patches:
            p.start()
        app = FastAPI()

        async def get_db():
            yield self.session

        register_miniapp_guides(app, get_db=get_db)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        for p in reversed(self.patches):
            p.stop()
        reset_shop_reseller_id(self.shop_token)
        await self.session.close()
        await self.engine.dispose()

    async def test_lists_only_enabled_customer_guides_as_plain_safe_data(self) -> None:
        response = await self.client.get("/api/mini/guides")
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers["cache-control"])
        guides = response.json()["guides"]
        self.assertEqual([g["id"] for g in guides], ["v2box"])
        guide = guides[0]
        self.assertEqual(guide["body"], "۱) نصب کنید & وصل شوید")
        self.assertEqual(guide["links"], [{"label": "ویدیو", "url": "https://youtu.be/abc"}])
        self.assertTrue(guide["opens_subscription"])
        self.assertEqual(guide["media_count"], 2)
        for leak in ("TG_FILE_ID_SECRET", "0123456789abcdef", "javascript"):
            self.assertNotIn(leak, response.text)

    async def test_sends_album_and_text_to_the_callers_chat_once_a_minute(self) -> None:
        response = await self.client.post("/api/mini/guides/v2box/send")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.bot.photos, [101], "the reusable Telegram copy is sent; the missing file is skipped")
        self.assertEqual([chat for chat, _ in self.bot.messages], [101])
        self.assertIn("V2Box", self.bot.messages[0][1])
        self.bot.session.close.assert_awaited()
        again = await self.client.post("/api/mini/guides/v2box/send")
        self.assertEqual(again.status_code, 429)
        self.assertEqual(len(self.bot.messages), 1)

    async def test_unknown_disabled_and_reseller_guides_are_not_sent(self) -> None:
        for guide_id in ("off", "panel", "nope", "..%2F..%2Fetc", "x" * 40):
            with self.subTest(guide_id=guide_id):
                self.assertEqual((await self.client.post(f"/api/mini/guides/{guide_id}/send")).status_code, 404)
        self.assertEqual(self.bot.messages, [])


if __name__ == "__main__":
    unittest.main()
