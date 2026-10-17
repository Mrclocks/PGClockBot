"""Editing connection guides in the bot: wizard, files, links, order, delete, shop isolation."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from connection_monitor_support import MonitorDatabaseCase
from sqlalchemy import select

from app.bot.handlers import guide_editor
from app.bot.handlers.guide_editor import Actor, handle_callback, handle_message
from app.db.models import ResellerSetting, Setting
from app.services.connection_guides import parse_connection_guides
from app.services.users import clear_settings_cache

OWNER = Actor(audience="a", reseller_id=None)
SHOP2 = Actor(audience="r", reseller_id=2)
SHOP3 = Actor(audience="r", reseller_id=3)
CHAT = 500


def _callback(data: str) -> MagicMock:
    callback = MagicMock(data=data)
    callback.answer = AsyncMock()
    callback.bot = AsyncMock()
    callback.bot.id = 77
    callback.message = MagicMock(message_id=10, chat=MagicMock(id=CHAT))
    callback.message.edit_text = AsyncMock()
    callback.message.answer = AsyncMock()
    return callback


def _message(text: str | None = None, *, photo: str | None = None, video: str | None = None) -> MagicMock:
    message = MagicMock(text=text, html_text=text, chat=MagicMock(id=CHAT), message_id=20)
    message.photo = [MagicMock(file_id=photo)] if photo else None
    message.video = MagicMock(file_id=video) if video else None
    message.bot = AsyncMock()
    message.bot.id = 77
    message.answer = AsyncMock()
    message.delete = AsyncMock()
    return message


def _shown(callback: MagicMock) -> str:
    return callback.message.edit_text.await_args.args[0]


def _buttons(callback: MagicMock) -> list[str]:
    markup = callback.message.edit_text.await_args.kwargs["reply_markup"]
    return [button.callback_data for row in markup.inline_keyboard for button in row]


class GuideEditorTests(MonitorDatabaseCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        clear_settings_cache()
        self.addCleanup(clear_settings_cache)
        self.state = FSMContext(MemoryStorage(), StorageKey(bot_id=77, chat_id=CHAT, user_id=CHAT))

    async def tap(self, actor: Actor, data: str) -> MagicMock:
        callback = _callback(data)
        async with self.Session() as session:
            await handle_callback(callback, session, self.state, actor)
        return callback

    async def send(self, actor: Actor, message: MagicMock) -> MagicMock:
        async with self.Session() as session:
            await handle_message(message, session, self.state, actor)
        return message

    async def stored(self, *, shop: int | None = None) -> list[dict]:
        clear_settings_cache()
        async with self.Session() as session:
            if shop is None:
                raw = await session.scalar(select(Setting.value).where(Setting.key == "connection_guides"))
            else:
                raw = await session.scalar(select(ResellerSetting.value).where(
                    ResellerSetting.reseller_user_id == shop, ResellerSetting.key == "connection_guides"))
        return parse_connection_guides(raw)

    async def create(self, actor: Actor, title: str = "راهنمای تازه", body: str = "<b>مرحله ۱</b>") -> dict:
        await self.tap(actor, f"gde:{actor.audience}:n")
        await self.send(actor, _message(title))
        await self.send(actor, _message(body))
        return next(g for g in await self.stored(shop=actor.reseller_id) if g["title"] == title)

    async def test_the_wizard_creates_a_guide_with_formatted_text(self) -> None:
        guide = await self.create(OWNER)
        self.assertEqual(guide["body"], "<b>مرحله ۱</b>")
        self.assertTrue(guide["enabled"])
        self.assertIsNone(await self.state.get_state())

    async def test_cancel_in_the_middle_saves_nothing(self) -> None:
        await self.tap(OWNER, "gde:a:n")
        await self.send(OWNER, _message("نیمه‌کاره"))
        await self.send(OWNER, _message("انصراف"))
        self.assertNotIn("نیمه‌کاره", [g["title"] for g in await self.stored()])
        self.assertIsNone(await self.state.get_state())

    async def test_an_empty_title_is_asked_again(self) -> None:
        await self.tap(OWNER, "gde:a:n")
        message = await self.send(OWNER, _message("   "))
        message.answer.assert_awaited_once_with(guide_editor.TEXT_REQUIRED)
        self.assertIsNotNone(await self.state.get_state())

    async def test_an_album_keeps_every_file(self) -> None:
        guide = await self.create(OWNER)
        await self.tap(OWNER, f"gde:a:ma:{guide['id']}")
        album = [_message(photo=f"P{i}") for i in range(4)] + [_message(video="V1")]
        async with self.Session() as s1, self.Session() as s2, self.Session() as s3:
            sessions = [s1, s2, s3, s1, s2]
            await asyncio.gather(*(handle_message(m, s, self.state, OWNER) for m, s in zip(album, sessions)))
        media = (await self.stored())[-1]["media"]
        self.assertEqual(sorted(m["tg"]["77"] for m in media), ["P0", "P1", "P2", "P3", "V1"])
        self.assertEqual(sum(m["kind"] == "video" for m in media), 1)

    async def test_text_in_the_file_step_is_refused(self) -> None:
        guide = await self.create(OWNER)
        await self.tap(OWNER, f"gde:a:ma:{guide['id']}")
        message = await self.send(OWNER, _message("سلام"))
        self.assertIn("فقط عکس یا ویدیو", message.answer.await_args.args[0])
        self.assertNotIn("media", (await self.stored())[-1])

    async def test_links_are_checked_and_can_be_removed(self) -> None:
        guide = await self.create(OWNER)
        await self.tap(OWNER, f"gde:a:La:{guide['id']}")
        await self.send(OWNER, _message("https://evil.example/x"))
        refused = await self.send(OWNER, _message("-"))
        self.assertIn("youtube", refused.answer.await_args.args[0])
        await self.tap(OWNER, f"gde:a:La:{guide['id']}")
        await self.send(OWNER, _message("https://youtu.be/abc"))
        await self.send(OWNER, _message("-"))
        self.assertEqual((await self.stored())[-1]["links"][0]["url"], "https://youtu.be/abc")
        await self.tap(OWNER, f"gde:a:Ld:{guide['id']}:0")
        self.assertNotIn("links", (await self.stored())[-1])

    async def test_switches_order_and_delete(self) -> None:
        first = await self.create(OWNER, "A")
        second = await self.create(OWNER, "B")
        await self.tap(OWNER, f"gde:a:e:{second['id']}")
        await self.tap(OWNER, f"gde:a:u:{second['id']}")
        await self.tap(OWNER, f"gde:a:o:{second['id']}:u")
        guides = await self.stored()
        moved = next(g for g in guides if g["id"] == second["id"])
        self.assertEqual((moved["enabled"], moved["audience"]), (False, "reseller"))
        self.assertLess(guides.index(moved), guides.index(next(g for g in guides if g["id"] == first["id"])))
        ask = await self.tap(OWNER, f"gde:a:x:{second['id']}")
        self.assertIn(f"gde:a:X:{second['id']}", _buttons(ask))
        self.assertIn(second["id"], [g["id"] for g in await self.stored()])
        await self.tap(OWNER, f"gde:a:X:{second['id']}")
        self.assertNotIn(second["id"], [g["id"] for g in await self.stored()])

    async def test_a_shop_edits_only_its_own_guides(self) -> None:
        guide = await self.create(SHOP2, "فروشگاه دو")
        self.assertNotIn("فروشگاه دو", [g["title"] for g in await self.stored()])
        self.assertEqual(await self.stored(shop=3), [])
        foreign = await self.tap(SHOP3, f"gde:r:x:{guide['id']}")
        foreign.answer.assert_awaited_once()
        self.assertTrue(foreign.answer.await_args.kwargs.get("show_alert"))
        await self.tap(SHOP3, f"gde:r:X:{guide['id']}")
        self.assertIn(guide["id"], [g["id"] for g in await self.stored(shop=2)])

    async def test_malformed_callbacks_are_refused(self) -> None:
        for data in ("gde:a:zz", "gde:a:g", "gde:a:mo:x", "gde:a:Ld:g1:notanumber"):
            with self.subTest(data=data):
                callback = await self.tap(OWNER, data)
                self.assertTrue(callback.answer.await_args.kwargs.get("show_alert"))

    async def test_a_person_without_shop_settings_cannot_open_the_editor(self) -> None:
        callback = _callback("gde:r:l")
        with patch.object(guide_editor, "shop_settings_shop_id", AsyncMock(return_value=None)):
            async with self.Session() as session:
                await guide_editor.shop_guides(callback, session, MagicMock(), self.state)
        self.assertTrue(callback.answer.await_args.kwargs.get("show_alert"))
        callback.message.edit_text.assert_not_awaited()

    async def test_preview_shows_the_album_then_the_text(self) -> None:
        guide = await self.create(OWNER)
        await self.tap(OWNER, f"gde:a:ma:{guide['id']}")
        await self.send(OWNER, _message(photo="P0"))
        callback = _callback(f"gde:a:p:{guide['id']}")
        callback.bot.send_photo = AsyncMock(return_value=MagicMock(photo=[MagicMock(file_id="P0")], video=None))
        async with self.Session() as session:
            await handle_callback(callback, session, self.state, OWNER)
        self.assertEqual(callback.bot.send_photo.await_args.kwargs["photo"], "P0")
        self.assertIn("مرحله ۱", callback.bot.send_message.await_args_list[0].args[1])

    async def test_starter_guides_become_editable(self) -> None:
        async with self.Session() as session:
            session.add(Setting(key="connection_guides", value=json.dumps([])))
            await session.commit()
        await self.tap(OWNER, "gde:a:e:v2box")
        self.assertFalse(next(g for g in await self.stored() if g["id"] == "v2box")["enabled"])
