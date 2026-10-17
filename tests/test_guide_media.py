"""Connection guides with photos, videos and video links: validation, shop isolation, sending."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.types import FSInputFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import ResellerSetting, Setting
from app.services import guide_media
from app.services.connection_guides import parse_connection_guides
from app.services.guide_media import (
    GuideMediaError,
    check_upload,
    delete_unused_files,
    keep_stored_media,
    media_to_send,
    normalize_link,
    parse_media,
)
from app.services.users import clear_settings_cache
from tests.test_update_owner_only import OWNER_SESSION, SHOP_SESSION, RoutesHarness

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"0" * 64
MEDIA_ID = "a1b2c3d4e5f6"
STORED = "0" * 32 + ".jpg"


class _MediaDir(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        patcher = patch.object(guide_media, "GUIDE_MEDIA_DIR", Path(self._dir.name))
        patcher.start()
        self.addCleanup(patcher.stop)


class UploadAndLinkValidationTests(unittest.TestCase):
    def test_photos_and_mp4_videos_are_accepted_by_their_content(self) -> None:
        self.assertEqual(check_upload(JPEG), ("photo", "jpg"))
        self.assertEqual(check_upload(MP4), ("video", "mp4"))

    def test_other_files_and_oversized_photos_are_refused(self) -> None:
        for data in (b"MZ\x90\x00" + b"0" * 64, b"<svg onload=alert(1)>", b""):
            with self.subTest(data=data[:8]), self.assertRaises(GuideMediaError):
                check_upload(data)
        with self.assertRaises(GuideMediaError):
            check_upload(JPEG + b"0" * guide_media.MAX_PHOTO_BYTES)

    def test_only_https_youtube_and_telegram_links_pass(self) -> None:
        self.assertEqual(normalize_link("", "https://youtu.be/abc")["label"], guide_media.DEFAULT_LINK_LABEL)
        self.assertEqual(normalize_link("<b>ویدیو</b>", "https://t.me/c/1")["label"], "bویدیو/b")
        for url in (
            "http://youtube.com/x", "https://evil.example/x", "javascript:alert(1)",
            "https://user:pw@youtube.com/x", "https://youtube.com.evil.example/x",
        ):
            with self.subTest(url=url), self.assertRaises(GuideMediaError):
                normalize_link("x", url)

    def test_stored_media_never_points_outside_the_media_folder(self) -> None:
        raw = [
            {"id": MEDIA_ID, "kind": "photo", "file": "../../bot.db"},
            {"id": "bad", "kind": "photo", "file": STORED},
            {"id": "b1b2c3d4e5f6", "kind": "script", "file": STORED},
            {"id": "c1b2c3d4e5f6", "kind": "photo", "file": STORED},
        ]
        self.assertEqual(parse_media(raw), [{"id": "c1b2c3d4e5f6", "kind": "photo", "file": STORED}])

    def test_a_form_save_keeps_the_stored_media_and_ignores_forged_entries(self) -> None:
        stored = [{"id": "g1", "media": [{"id": MEDIA_ID, "kind": "photo", "file": STORED}]}]
        forged = [{"id": "g1", "title": "x", "media": [{"id": MEDIA_ID, "kind": "photo", "file": "f" * 32 + ".mp4"}]},
                  {"id": "g2", "title": "y", "media": [{"id": MEDIA_ID, "kind": "photo", "file": STORED}]}]
        kept = keep_stored_media(forged, stored)
        self.assertEqual(kept[0]["media"], stored[0]["media"])
        self.assertNotIn("media", kept[1])


class StoredFileTests(_MediaDir):
    def _write(self, name: str, *, age: float) -> Path:
        folder = guide_media.scope_dir(5)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(JPEG)
        os.utime(path, (time.time() - age, time.time() - age))
        return path

    def test_unused_files_go_but_used_and_fresh_uploads_stay(self) -> None:
        old_unused = self._write("1" * 32 + ".jpg", age=3600)
        fresh = self._write("2" * 32 + ".jpg", age=0)
        used = self._write("3" * 32 + ".jpg", age=3600)
        removed = delete_unused_files({used.name}, reseller_id=5)
        self.assertEqual(removed, 1)
        self.assertFalse(old_unused.exists())
        self.assertTrue(fresh.exists() and used.exists())

    def test_a_shop_never_reads_another_scopes_file(self) -> None:
        self._write(STORED, age=0)
        self.assertIsNotNone(guide_media.media_path(STORED, reseller_id=5))
        self.assertIsNone(guide_media.media_path(STORED, reseller_id=6))
        self.assertIsNone(guide_media.media_path(STORED, reseller_id=None))

    def test_each_bot_reuses_its_own_copy_and_uploads_otherwise(self) -> None:
        path = self._write(STORED, age=0)
        guide = {"media": [
            {"id": MEDIA_ID, "kind": "photo", "file": STORED},
            {"id": "b1b2c3d4e5f6", "kind": "video", "tg": {"77": "BOTFILE"}},
        ]}
        cache = {MEDIA_ID: {"77": "CACHED"}}
        for_77 = media_to_send(guide, bot_id=77, cache=cache, reseller_id=5)
        self.assertEqual([(m.file_id, m.path) for m in for_77], [("CACHED", None), ("BOTFILE", None)])
        for_88 = media_to_send(guide, bot_id=88, cache=cache, reseller_id=5)
        self.assertEqual([(m.file_id, m.path) for m in for_88], [(None, path)])


class GuideMediaRouteTests(RoutesHarness):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        for target, value in (("GUIDE_MEDIA_DIR", Path(self._dir.name)), ("UNUSED_FILE_GRACE_SEC", 0)):
            patcher = patch.object(guide_media, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        clear_settings_cache()
        self.addCleanup(clear_settings_cache)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        guides = json.dumps([{"id": "g1", "title": "V2Box", "body": "مراحل"}])
        async with self.Session() as session:
            session.add(Setting(key="connection_guides", value=guides))
            session.add(ResellerSetting(reseller_user_id=5, key="connection_guides", value=guides))
            await session.commit()

    def _files(self, pattern: str) -> list[Path]:
        return list(Path(self._dir.name).rglob(pattern))

    async def request(self, session_payload: dict, method: str, path: str, **kwargs):
        cookie = f"session={self.signer.dumps(session_payload)}; csrf=token"
        headers = {"cookie": cookie, "x-csrf-token": "token", "origin": "http://panel.test"}
        return await self.client.request(method, path, headers=headers, **kwargs)

    async def stored(self, *, shop: int | None = None) -> list[dict]:
        async with self.Session() as session:
            if shop is None:
                raw = await session.scalar(select(Setting.value).where(Setting.key == "connection_guides"))
            else:
                raw = await session.scalar(select(ResellerSetting.value).where(
                    ResellerSetting.reseller_user_id == shop, ResellerSetting.key == "connection_guides"))
        return parse_connection_guides(raw)

    async def upload(self, session_payload: dict, base: str, guide_id: str = "g1", data: bytes = JPEG):
        return await self.request(
            session_payload, "POST", f"{base}/upload",
            data={"guide_id": guide_id}, files={"file": ("a.jpg", data, "image/jpeg")},
        )

    async def test_the_owner_uploads_and_previews_a_photo(self) -> None:
        resp = await self.upload(OWNER_SESSION, "/settings/guides/media")
        self.assertEqual(resp.status_code, 200, resp.text)
        media = (await self.stored())[0]["media"]
        self.assertEqual(media[0]["kind"], "photo")
        preview = resp.json()["media"][0]["preview"]
        self.assertEqual((await self.request(OWNER_SESSION, "GET", preview)).content, JPEG)

    async def test_a_shop_cannot_see_the_platform_files_or_upload_to_them(self) -> None:
        resp = await self.upload(OWNER_SESSION, "/settings/guides/media")
        name = (await self.stored())[0]["media"][0]["file"]
        shop_view = await self.request(SHOP_SESSION, "GET", f"/shop-settings/guides/media/file/{name}")
        self.assertEqual(shop_view.status_code, 404)
        self.assertEqual(resp.status_code, 200)
        denied = await self.upload(SHOP_SESSION, "/settings/guides/media")
        self.assertIn(denied.status_code, {302, 303, 401, 403})
        self.assertEqual(len((await self.stored())[0]["media"]), 1)

    async def test_a_shop_upload_stays_in_its_own_guides(self) -> None:
        resp = await self.upload(SHOP_SESSION, "/shop-settings/guides/media")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(len((await self.stored(shop=5))[0]["media"]), 1)
        self.assertNotIn("media", (await self.stored())[0])

    async def test_an_unsaved_guide_or_a_bad_file_leaves_nothing_behind(self) -> None:
        for guide_id, data in (("missing", JPEG), ("g1", b"MZ" + b"0" * 64)):
            with self.subTest(guide_id=guide_id):
                resp = await self.upload(OWNER_SESSION, "/settings/guides/media", guide_id=guide_id, data=data)
                self.assertEqual(resp.status_code, 400)
        self.assertEqual(self._files("*.*"), [])

    async def test_a_form_save_keeps_media_and_removing_the_guide_deletes_its_file(self) -> None:
        await self.upload(OWNER_SESSION, "/settings/guides/media")
        name = (await self.stored())[0]["media"][0]["file"]
        forged = [{"id": "g1", "title": "V2Box", "body": "تازه", "media": [],
                   "links": [{"label": "ویدیو", "url": "https://youtu.be/x"}, {"url": "https://evil.example"}]}]
        await self.request(OWNER_SESSION, "POST", "/settings?tab=guides", data={"s_connection_guides": json.dumps(forged)})
        guide = (await self.stored())[0]
        self.assertEqual((guide["body"], guide["media"][0]["file"]), ("تازه", name))
        self.assertEqual(guide["links"], [{"label": "ویدیو", "url": "https://youtu.be/x"}])
        others = [{"id": "g2", "title": "دیگری", "body": "x"}]
        await self.request(OWNER_SESSION, "POST", "/settings?tab=guides", data={"s_connection_guides": json.dumps(others)})
        self.assertEqual(self._files("*.jpg"), [])


class CustomerGuideViewTests(_MediaDir, unittest.IsolatedAsyncioTestCase):
    async def _open(self, guide: dict, *, cache: dict) -> tuple[MagicMock, AsyncMock]:
        from app.bot.handlers import guides as handler

        bot = AsyncMock()
        bot.id = 77
        sent = MagicMock(photo=[MagicMock(file_id="NEWID")], video=None)
        bot.send_photo = AsyncMock(return_value=sent)
        callback = MagicMock(data="guide:open:0:g1", bot=bot)
        callback.answer = AsyncMock()
        callback.message = MagicMock(chat=MagicMock(id=100))
        callback.message.answer = AsyncMock()
        callback.message.delete = AsyncMock()
        remember = AsyncMock()
        with (
            patch.object(handler, "get_all_settings", AsyncMock(return_value={})),
            patch.object(handler, "get_connection_guides", AsyncMock(return_value=[guide])),
            patch.object(handler, "load_file_id_cache", AsyncMock(return_value=cache)),
            patch.object(handler, "remember_file_ids", remember),
            patch.object(handler, "current_shop_reseller_id", return_value=None),
        ):
            await handler.guide_open_cb(callback, MagicMock(), MagicMock(id=1))
        return callback, remember

    def _guide(self) -> dict:
        folder = guide_media.scope_dir(None)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / STORED).write_bytes(JPEG)
        return {"id": "g1", "title": "V2Box", "body": "مراحل", "enabled": True,
                "media": [{"id": MEDIA_ID, "kind": "photo", "file": STORED}],
                "links": [{"label": "▶️ ویدیو", "url": "https://youtu.be/x"}]}

    async def test_the_first_view_uploads_the_photo_and_remembers_its_id(self) -> None:
        callback, remember = await self._open(self._guide(), cache={})
        photo = callback.bot.send_photo.await_args.kwargs["photo"]
        self.assertIsInstance(photo, FSInputFile)
        self.assertEqual(remember.await_args.args[1], {MEDIA_ID: "NEWID"})
        markup = callback.message.answer.await_args.kwargs["reply_markup"]
        self.assertIn("https://youtu.be/x", [b.url for row in markup.inline_keyboard for b in row])

    async def test_a_later_view_reuses_the_bots_copy(self) -> None:
        callback, remember = await self._open(self._guide(), cache={MEDIA_ID: {"77": "CACHED"}})
        self.assertEqual(callback.bot.send_photo.await_args.kwargs["photo"], "CACHED")
        remember.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
