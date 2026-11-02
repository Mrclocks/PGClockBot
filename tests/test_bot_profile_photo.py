"""The Mini App logo copies a bot's Telegram profile photo; a panel upload always wins."""

from __future__ import annotations

import io
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import GetMe
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.datastructures import Headers, UploadFile

from app.db.models import Base, BotUser, ResellerProfile
from app.jobs import bot_profile_photo as photo_job
from app.services import bot_profile_photo as photos
from app.services.bot_appearance import BotAppearance, save_appearance_from_form
from app.services.miniapp_branding import brand_from_settings
from app.services.miniapp_shop import PLATFORM_SWITCH, SHOP_SWITCH
from app.services.secret_box import seal_bot_token
from app.services.users import clear_settings_cache, get_all_settings, set_settings_bulk

SHOP, OTHER_SHOP, NO_PERM_SHOP, OFF_SHOP = 5, 6, 7, 8
PERMS = '["miniapp"]'


def size(width: int, unique: str = "u", file_size: int = 1000) -> SimpleNamespace:
    return SimpleNamespace(width=width, height=width, file_id=f"f{width}", file_unique_id=unique, file_size=file_size)


class PickSizeTests(unittest.TestCase):
    def test_smallest_size_wide_enough_for_the_logo(self) -> None:
        self.assertEqual(photos.pick_photo_size([size(640), size(160), size(320)]).width, 160)

    def test_largest_when_all_are_small_and_none_without_sizes(self) -> None:
        self.assertEqual(photos.pick_photo_size([size(90), size(120)]).width, 120)
        self.assertIsNone(photos.pick_photo_size([]))


class FetchTests(unittest.IsolatedAsyncioTestCase):
    async def fetch_with(self, sizes, data: bytes = b"jpeg"):
        bot = SimpleNamespace(
            get_me=AsyncMock(return_value=SimpleNamespace(id=42)),
            get_user_profile_photos=AsyncMock(return_value=SimpleNamespace(photos=[sizes] if sizes else [])),
            get_file=AsyncMock(return_value=SimpleNamespace(file_path="photos/x.jpg")),
            download_file=AsyncMock(side_effect=lambda _p, destination, timeout: destination.write(data)),
            session=SimpleNamespace(close=AsyncMock()),
        )
        with patch.object(photos, "Bot", return_value=bot):
            result = await photos.fetch_bot_profile_photo("1:token")
        bot.session.close.assert_awaited()
        return result

    async def test_returns_the_chosen_size_and_its_unique_id(self) -> None:
        photo = await self.fetch_with([size(160, "U160"), size(640, "U640")])
        self.assertEqual(photo, photos.ProfilePhoto(unique_id="U160", data=b"jpeg"))

    async def test_no_photo_is_none(self) -> None:
        self.assertIsNone(await self.fetch_with([]))

    async def test_oversized_or_odd_files_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            await self.fetch_with([size(160, file_size=photos.PHOTO_MAX_BYTES + 1)])
        with self.assertRaises(ValueError):
            await self.fetch_with([size(160, unique="../../etc")])
        with self.assertRaises(ValueError):
            await self.fetch_with([size(160)], data=b"")


class StoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        clear_settings_cache()
        self.tmp = tempfile.TemporaryDirectory()
        self.uploads = Path(self.tmp.name)
        self.dir_patch = patch.object(photos, "uploads_dir", return_value=self.uploads)
        self.dir_patch.start()
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        now = datetime.now(timezone.utc)
        async with self.Session() as session:
            session.add_all([BotUser(id=i, telegram_id=1000 + i, referral_code=f"r{i}", role="reseller")
                             for i in (SHOP, OTHER_SHOP, NO_PERM_SHOP, OFF_SHOP)])
            for pid, uid, perms in ((1, SHOP, PERMS), (2, OTHER_SHOP, PERMS), (3, NO_PERM_SHOP, '["orders"]'), (4, OFF_SHOP, PERMS)):
                session.add(ResellerProfile(id=pid, user_id=uid, is_active=True, setup_completed_at=now,
                                            bot_token=seal_bot_token(f"{uid}:tok"), web_permissions=perms))
            await session.commit()
            await set_settings_bulk(session, {PLATFORM_SWITCH: "1"})
            for shop in (SHOP, OTHER_SHOP, NO_PERM_SHOP):
                await set_settings_bulk(session, {SHOP_SWITCH: "1"}, reseller_id=shop)
        clear_settings_cache()

    async def asyncTearDown(self) -> None:
        self.dir_patch.stop()
        clear_settings_cache()
        await self.engine.dispose()
        self.tmp.cleanup()

    async def ui(self, shop_id: int | None) -> dict[str, str]:
        clear_settings_cache()
        async with self.Session() as session:
            return await get_all_settings(session, reseller_id=shop_id)

    async def store(self, shop_id: int | None, photo: photos.ProfilePhoto | None) -> str:
        ui = await self.ui(shop_id)
        async with self.Session() as session:
            return await photos.store_telegram_photo(session, reseller_id=shop_id, ui=ui, photo=photo)

    async def test_saves_keeps_replaces_and_clears_the_copy(self) -> None:
        self.assertEqual(await self.store(SHOP, photos.ProfilePhoto("U1", b"one")), "saved")
        first = (await self.ui(SHOP))["bot_tg_photo"]
        self.assertEqual((self.uploads / Path(first).name).read_bytes(), b"one")
        self.assertTrue(brand_from_settings(await self.ui(SHOP), name="n").logo_url, "the Mini App accepts the copy as its logo")
        self.assertEqual(await self.store(SHOP, photos.ProfilePhoto("U1", b"one")), "unchanged")
        self.assertEqual(await self.store(SHOP, photos.ProfilePhoto("U2", b"two")), "saved")
        second = (await self.ui(SHOP))["bot_tg_photo"]
        self.assertFalse((self.uploads / Path(first).name).exists(), "the old copy is deleted")
        self.assertEqual(await self.store(SHOP, None), "cleared")
        self.assertFalse((self.uploads / Path(second).name).exists())
        self.assertEqual(((await self.ui(SHOP))["bot_tg_photo"], (await self.ui(SHOP))["bot_tg_photo_source"]), ("", ""))

    async def test_a_panel_upload_is_never_replaced_or_deleted(self) -> None:
        panel_file = self.uploads / "r5_bot_tg_photo_abc.jpg"
        panel_file.write_bytes(b"panel")
        for source in ("panel", ""):
            with self.subTest(source=source or "uploaded before sources were recorded"):
                async with self.Session() as session:
                    await set_settings_bulk(session, {"bot_tg_photo": "uploads/r5_bot_tg_photo_abc.jpg",
                                                      "bot_tg_photo_source": source}, reseller_id=SHOP)
                self.assertEqual(await self.store(SHOP, photos.ProfilePhoto("U9", b"tg")), "skipped")
                self.assertEqual(await self.store(SHOP, None), "skipped")
                self.assertEqual((await self.ui(SHOP))["bot_tg_photo"], "uploads/r5_bot_tg_photo_abc.jpg")
                self.assertEqual(panel_file.read_bytes(), b"panel")

    async def test_a_copy_lands_in_its_own_shop_only(self) -> None:
        await self.store(SHOP, photos.ProfilePhoto("U1", b"shop"))
        self.assertTrue((await self.ui(SHOP))["bot_tg_photo"].startswith("uploads/r5_"))
        self.assertEqual((await self.ui(OTHER_SHOP))["bot_tg_photo"], "")
        self.assertEqual((await self.ui(None))["bot_tg_photo"], "")
        await self.store(None, photos.ProfilePhoto("P1", b"platform"))
        self.assertTrue((await self.ui(None))["bot_tg_photo"].startswith("uploads/main_"))
        self.assertTrue((await self.ui(SHOP))["bot_tg_photo"].startswith("uploads/r5_"))

    async def test_only_shops_with_an_open_mini_app_are_synced_in_pages(self) -> None:
        async with self.Session() as session:
            first = await photos.shop_targets(session, after_id=0, limit=2)
            rest = await photos.shop_targets(session, after_id=first[-1].profile_id, limit=2)
        targets = first + rest
        self.assertEqual([t.shop_id for t in targets], [SHOP, OTHER_SHOP, NO_PERM_SHOP])
        self.assertEqual([bool(t.token) for t in targets], [True, True, False], "no permission: no token, never synced")
        self.assertNotIn("tok", repr(targets[0]), "tokens never appear in logs")
        async with self.Session() as session:
            await set_settings_bulk(session, {PLATFORM_SWITCH: "0"})
        clear_settings_cache()
        async with self.Session() as session:
            self.assertEqual(await photos.shop_targets(session, after_id=0, limit=10), [])

    async def test_sync_skips_telegram_when_a_panel_photo_exists_and_respects_an_upload_during_the_fetch(self) -> None:
        target = photos.PhotoTarget(shop_id=SHOP, profile_id=1, token="5:tok")
        fetch = AsyncMock(return_value=photos.ProfilePhoto("U1", b"tg"))

        async def upload_meanwhile(_token):
            async with self.Session() as session:
                await set_settings_bulk(session, {"bot_tg_photo": "uploads/r5_bot_tg_photo_new.jpg",
                                                  "bot_tg_photo_source": "panel"}, reseller_id=SHOP)
            return photos.ProfilePhoto("U1", b"tg")

        with patch.object(photos, "fetch_bot_profile_photo", side_effect=upload_meanwhile):
            self.assertEqual(await photos.sync_bot_photo(self.Session, target), "skipped")
        self.assertEqual((await self.ui(SHOP))["bot_tg_photo"], "uploads/r5_bot_tg_photo_new.jpg")
        with patch.object(photos, "fetch_bot_profile_photo", fetch):
            self.assertEqual(await photos.sync_bot_photo(self.Session, target), "skipped")
        fetch.assert_not_awaited()

    async def test_one_failing_bot_does_not_stop_the_job(self) -> None:
        calls: list[int | None] = []

        async def fake_sync(_sessions, target):
            calls.append(target.shop_id)
            if target.shop_id == SHOP:
                raise TelegramNetworkError(method=GetMe(), message="timeout")
            return "saved"

        with patch.object(photo_job, "SessionLocal", self.Session), \
                patch.object(photo_job, "sync_bot_photo", side_effect=fake_sync), \
                patch.object(photo_job, "platform_target", return_value=photos.PhotoTarget(None, None, "1:tok")):
            await photo_job.run_bot_profile_photo_sync()
        self.assertEqual(calls, [None, SHOP, OTHER_SHOP, NO_PERM_SHOP])


class PanelUploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_photo_uploaded_in_the_panel_is_marked_so_the_job_leaves_it(self) -> None:
        saved: dict = {}

        async def fake_bulk(_session, values, *, reseller_id=None):
            saved.update(values)

        upload = UploadFile(io.BytesIO(b"\xff\xd8jpeg"), filename="logo.jpg", headers=Headers({"content-type": "image/jpeg"}))
        with tempfile.TemporaryDirectory() as tmp, \
                patch("app.config.DATA_DIR", Path(tmp)), \
                patch("app.services.bot_appearance.apply_appearance", AsyncMock(return_value=BotAppearance(ok=True))), \
                patch("app.services.users.set_settings_bulk", fake_bulk):
            ok, _msg = await save_appearance_from_form(
                None, {"bot_tg_name": "n", "bot_tg_photo": upload}, token="1:tok", reseller_id=SHOP,
            )
        self.assertTrue(ok)
        self.assertEqual(saved["bot_tg_photo_source"], "panel")
        self.assertFalse(photos.wants_telegram_photo(saved))


if __name__ == "__main__":
    unittest.main()
