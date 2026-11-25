"""Group messages with a photo or video: validation, storage, sending once per recipient, web upload and bot input."""

from __future__ import annotations

import importlib.util
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import sqlalchemy as sa
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import BufferedInputFile, FSInputFile
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text

from app.bot.broadcast_media_input import media_from_data
from app.db.models import BroadcastLog
from app.services import broadcast_media
from app.services.broadcast_media import CAPTION_MAX, BroadcastMedia, media_from_row, prune_uploads, write_upload
from app.services.broadcast_queue import (
    STATUS_DONE,
    STATUS_FAILED,
    BroadcastRequest,
    process_broadcast_batch,
    prune_broadcast_uploads,
    queue_broadcast,
)
from app.services.broadcast_test_send import checked_test_media, send_test_broadcast
from app.services.shop_broadcast import clean_shop_broadcast_text
from tests.postgres_support import scoped_sync_engine
from tests.test_broadcast_pages import OWNER, PageCase
from tests.test_broadcast_queue import EVERYONE, QueueCase
from tests.test_broadcast_retry import RetryCase
from tests.test_broadcast_safety import DbCase as BotDbCase
from tests.test_broadcast_test_send import TestSendCase, opener

ROOT = Path(__file__).resolve().parents[1]
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64


class MediaDirMixin:
    def use_temp_media_dir(self) -> Path:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patcher = patch.object(broadcast_media, "BROADCAST_MEDIA_DIR", Path(folder.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        return Path(folder.name)


class MediaBot:
    """Records each send; a first upload of a stored file gets a new file id back, as Telegram does."""

    def __init__(self):
        self.calls: list[tuple[str, int, object, dict]] = []
        self.behaviour: dict[int, Exception] = {}

    async def _record(self, method: str, chat_id: int, source: object, kwargs: dict):
        if isinstance(self.behaviour.get(chat_id), Exception):
            raise self.behaviour[chat_id]
        self.calls.append((method, chat_id, source, kwargs))
        file = SimpleNamespace(file_id=f"tg-{method}-id")
        return SimpleNamespace(photo=[file] if method == "photo" else None, video=file if method == "video" else None)

    async def send_photo(self, chat_id, photo, **kwargs):
        return await self._record("photo", chat_id, photo, kwargs)

    async def send_video(self, chat_id, video, **kwargs):
        return await self._record("video", chat_id, video, kwargs)

    async def send_message(self, chat_id, body, **kwargs):
        return await self._record("message", chat_id, body, kwargs)


class MediaValueTests(MediaDirMixin, unittest.TestCase):
    def test_media_needs_a_known_kind_and_a_source(self):
        for kwargs in ({"kind": "audio", "file_id": "x"}, {"kind": "photo"}, {"kind": "photo", "file_name": "../../etc/passwd"},
                       {"kind": "video", "file_id": "x" * 300}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                BroadcastMedia(**kwargs)
        self.assertIsNone(media_from_row("photo", None, "../x.png"))
        self.assertIsNone(media_from_row(None, "id", None))
        self.assertEqual(media_from_row("video", "id", None), BroadcastMedia(kind="video", file_id="id"))

    def test_an_upload_is_checked_by_its_content_and_stored_per_tenant(self):
        folder = self.use_temp_media_dir()
        photo = write_upload(PNG, shop_id=None)
        video = write_upload(MP4, shop_id=7)
        self.assertEqual((photo.kind, video.kind), ("photo", "video"))
        self.assertTrue((folder / "platform" / photo.file_name).is_file())
        self.assertTrue((folder / "shop-7" / video.file_name).is_file())
        self.assertIsNone(broadcast_media.stored_path(video.file_name, shop_id=8))
        for data in (b"<html>not a photo</html>", b"%PDF-1.7" + b"\x00" * 20):
            with self.subTest(data=data[:8]), self.assertRaises(ValueError):
                write_upload(data, shop_id=None)

    def test_an_oversized_photo_is_refused(self):
        self.use_temp_media_dir()
        with self.assertRaises(ValueError):
            write_upload(PNG + b"\x00" * broadcast_media.MAX_PHOTO_BYTES, shop_id=None)

    def test_prune_keeps_files_in_use_and_recent_uploads(self):
        folder = self.use_temp_media_dir()
        used = write_upload(PNG, shop_id=3)
        unused = write_upload(PNG, shop_id=3)
        recent = write_upload(PNG, shop_id=None)
        old = time.time() - broadcast_media.UNUSED_FILE_GRACE_SEC - 60
        for media, shop in ((used, 3), (unused, 3)):
            os.utime(folder / f"shop-{shop}" / media.file_name, (old, old))
        (folder / "stray").mkdir()
        self.assertEqual(prune_uploads({(3, used.file_name)}), 1)
        self.assertTrue((folder / "shop-3" / used.file_name).is_file())
        self.assertFalse((folder / "shop-3" / unused.file_name).exists())
        self.assertTrue((folder / "platform" / recent.file_name).is_file())

    def test_a_shop_caption_may_be_empty_with_media_and_is_limited(self):
        self.assertEqual(clean_shop_broadcast_text("  ", with_media=True), "")
        with self.assertRaises(ValueError):
            clean_shop_broadcast_text("  ")
        with self.assertRaises(ValueError):
            clean_shop_broadcast_text("a" * (CAPTION_MAX + 1), with_media=True)
        # Escaping a caption must not push it past Telegram's limit either.
        with self.assertRaises(ValueError):
            clean_shop_broadcast_text("<" * (CAPTION_MAX // 2), with_media=True)


class MediaQueueTests(MediaDirMixin, QueueCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.bot = MediaBot()

    async def test_platform_media_allows_an_empty_caption_and_limits_its_length(self):
        media = BroadcastMedia(kind="photo", file_id="tg-file")
        log = await queue_broadcast(self.session, text="", audience="users", created_by="o", request=BroadcastRequest(media=media))
        self.assertEqual((log.media_kind, log.media_file_id, log.text), ("photo", "tg-file", ""))
        with self.assertRaises(ValueError):
            await queue_broadcast(self.session, text="x" * (CAPTION_MAX + 1), audience="users", created_by="o",
                                  request=BroadcastRequest(media=media))
        with self.assertRaises(ValueError):
            await queue_broadcast(self.session, text="", audience="users", created_by="o")

    async def test_a_bot_photo_is_sent_once_to_each_recipient_with_its_caption(self):
        media = BroadcastMedia(kind="photo", file_id="tg-file")
        log = await queue_broadcast(self.session, text="<b>hi</b>", audience="users", created_by="o",
                                    request=BroadcastRequest(media=media))
        await self.run_all(log.id, batch_size=2)
        self.assertEqual(sorted(chat for _m, chat, _s, _k in self.bot.calls), EVERYONE)
        method, _chat, source, kwargs = self.bot.calls[0]
        self.assertEqual((method, source, kwargs["caption"], kwargs["parse_mode"]), ("photo", "tg-file", "<b>hi</b>", "HTML"))
        self.assertEqual((await self.log(log.id)).status, STATUS_DONE)

    async def test_a_web_upload_is_uploaded_once_then_reused_by_file_id(self):
        self.use_temp_media_dir()
        media = write_upload(MP4, shop_id=None)
        log = await queue_broadcast(self.session, text="", audience="users", created_by="o", request=BroadcastRequest(media=media))
        self.bot.behaviour[1001] = TelegramForbiddenError(method=MagicMock(), message="blocked")
        await self.run_all(log.id, batch_size=2)
        sources = [source for method, _chat, source, _k in self.bot.calls if method == "video"]
        self.assertIsInstance(sources[0], FSInputFile)
        self.assertEqual(sources[1:], ["tg-video-id"] * (len(EVERYONE) - 2))
        self.assertNotIn("caption", self.bot.calls[0][3])
        row = await self.log(log.id)
        self.assertEqual((row.media_file_id, row.ok_count, row.fail_count), ("tg-video-id", 5, 1))

    async def test_a_lost_upload_fails_the_broadcast_instead_of_sending_text_only(self):
        self.use_temp_media_dir()
        media = write_upload(PNG, shop_id=None)
        log = await queue_broadcast(self.session, text="hi", audience="users", created_by="o", request=BroadcastRequest(media=media))
        broadcast_media.delete_upload(media.file_name, shop_id=None)
        async with self.Session() as session:
            self.assertFalse(await process_broadcast_batch(session, log.id, self.bot, delay=0))
        self.assertEqual(self.bot.calls, [])
        self.assertEqual((await self.log(log.id)).status, STATUS_FAILED)

    async def test_prune_keeps_the_upload_of_a_queued_broadcast(self):
        folder = self.use_temp_media_dir()
        media = write_upload(PNG, shop_id=None)
        await queue_broadcast(self.session, text="hi", audience="users", created_by="o", request=BroadcastRequest(media=media))
        old = time.time() - broadcast_media.UNUSED_FILE_GRACE_SEC - 60
        os.utime(folder / "platform" / media.file_name, (old, old))
        self.assertEqual(await prune_broadcast_uploads(self.session), 0)
        self.assertTrue((folder / "platform" / media.file_name).is_file())


class MediaWebTests(MediaDirMixin, PageCase):
    def form(self, token: str) -> dict[str, str]:
        return {"segment": "all", "text": "", "request_token": token * 32,
                "btn_target": "", "btn_label": "", "btn_url": "", "btn_style": "", "btn_plan": ""}

    async def test_the_owner_can_queue_a_photo_without_text(self):
        folder = self.use_temp_media_dir()
        response = await self.call(OWNER, "POST", "/broadcast", platform=True, data=self.form("a"),
                                   files={"media": ("p.png", PNG, "image/png")})
        self.assertIn("ok=", response.headers["location"])
        row = await self.session.scalar(select(BroadcastLog))
        self.assertEqual(row.media_kind, "photo")
        self.assertTrue((folder / "platform" / row.media_file).is_file())

    async def test_a_file_that_is_not_media_is_refused_and_not_kept(self):
        folder = self.use_temp_media_dir()
        response = await self.call(OWNER, "POST", "/broadcast", platform=True, data=self.form("b"),
                                   files={"media": ("p.png", b"<svg onload=alert(1)>", "image/png")})
        self.assertIn("err=", response.headers["location"])
        self.assertIsNone(await self.session.scalar(select(BroadcastLog)))
        self.assertFalse(any(folder.rglob("*.*")))

    async def test_a_rejected_message_leaves_no_stored_file(self):
        folder = self.use_temp_media_dir()
        form = {**self.form("c"), "text": "x" * (CAPTION_MAX + 1)}
        response = await self.call(OWNER, "POST", "/broadcast", platform=True, data=form,
                                   files={"media": ("p.png", PNG, "image/png")})
        self.assertIn("err=", response.headers["location"])
        self.assertFalse(any(folder.rglob("*.png")))


class BotReceiptRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_photo_during_an_input_step_is_not_taken_as_a_receipt(self):
        from app.bot.handlers import wallet

        handler = next(h for h in wallet.router.message.handlers if h.callback is wallet.generic_receipt)
        message = SimpleNamespace(photo=[SimpleNamespace(file_id="x")])
        during_step, _ = await handler.check(message, raw_state="AdminStates:broadcast_text")
        idle, _ = await handler.check(message, raw_state=None)
        self.assertFalse(during_step)
        self.assertTrue(idle)


class OwnerBotMediaTests(BotDbCase):
    async def test_a_photo_with_a_caption_is_previewed_and_queued_with_its_media(self):
        from aiogram.fsm.context import FSMContext
        from aiogram.fsm.storage.base import StorageKey
        from aiogram.fsm.storage.memory import MemoryStorage
        from aiogram.types import Message

        from app.bot.handlers import admin_broadcast_confirm as confirm

        state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=101, user_id=101))
        message = MagicMock(spec=Message)
        message.text = message.entities = message.video = None
        message.photo = [SimpleNamespace(file_id="small"), SimpleNamespace(file_id="large")]
        message.caption, message.caption_entities = "تخفیف <ویژه>", None
        message.answer, message.answer_photo = AsyncMock(), AsyncMock()
        with patch("app.bot.message_button_picker.finish_text_step", AsyncMock()):
            async with self.Session() as session:
                await confirm.preview_broadcast(message, state, session, audience="all")
        callback = MagicMock()
        callback.message = message
        async with self.Session() as session:
            await confirm._preview(callback, state, session, MagicMock(), [])
        self.assertEqual(message.answer_photo.await_args.args, ("large",))
        self.assertEqual(message.answer_photo.await_args.kwargs["caption"], "تخفیف &lt;ویژه&gt;")
        # What «ارسال» queues: the text and media kept in the confirmation state.
        data = await state.get_data()
        async with self.Session() as session:
            row = await queue_broadcast(session, text=data["bc_text"], audience="all", created_by="101",
                                        request=BroadcastRequest(data["bc_token"], media=media_from_data(data)))
        self.assertEqual((row.media_kind, row.media_file_id, row.text), ("photo", "large", "تخفیف &lt;ویژه&gt;"))


class ResendMediaTests(RetryCase):
    async def test_a_resend_keeps_the_photo_of_the_original(self):
        log = await self.failed_broadcast()
        await self.session.execute(
            sa.update(BroadcastLog).where(BroadcastLog.id == log.id).values(media_kind="photo", media_file_id="tg-photo")
        )
        await self.session.commit()
        resend = await self.resend(log.id)
        self.assertEqual((resend.media_kind, resend.media_file_id), ("photo", "tg-photo"))


class TestSendMediaTests(TestSendCase):
    async def test_a_test_send_with_a_photo_goes_as_a_photo_with_its_caption(self):
        calls = []

        class PhotoBot:
            async def send_photo(self, chat_id, photo, **kwargs):
                calls.append((chat_id, photo, kwargs))

        delivered = await send_test_broadcast(self.session, shop_id=None, text="", buttons=None,
                                              open_bot=opener(PhotoBot()), media=checked_test_media(PNG))
        self.assertEqual(delivered, 2)
        self.assertEqual([chat for chat, _p, _k in calls], [7001, 7002])
        self.assertIsInstance(calls[0][1], BufferedInputFile)
        self.assertNotIn("caption", calls[0][2])

    async def test_a_test_file_that_is_not_media_is_refused(self):
        with self.assertRaises(ValueError):
            checked_test_media(b"MZ\x90\x00 not a photo")


class MigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade_keep_rows(self):
        spec = importlib.util.spec_from_file_location("broadcast_media_mig", ROOT / "alembic/versions/0137_broadcast_media.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = scoped_sync_engine()
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE broadcast_logs (id INTEGER PRIMARY KEY, text TEXT)"))
            conn.execute(text("INSERT INTO broadcast_logs VALUES (1, 'old')"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                cols = {c["name"] for c in sa.inspect(conn).get_columns("broadcast_logs")}
                self.assertTrue({"media_kind", "media_file_id", "media_file"} <= cols)
                migration.downgrade()
                cols = {c["name"] for c in sa.inspect(conn).get_columns("broadcast_logs")}
                self.assertFalse({"media_kind", "media_file_id", "media_file"} & cols)
                self.assertEqual(conn.execute(text("SELECT text FROM broadcast_logs")).scalar(), "old")
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
