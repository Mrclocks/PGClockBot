"""Backups keep exactly the chosen number; Telegram delivery honours its switch, size and .env rules."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("WEB_SECRET", "x" * 48)

from aiogram.exceptions import TelegramAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import Base
from app.services import backup as backup_mod
from app.services import backup_telegram as tg
from app.services.backup_settings import (
    CHAT_IDS_KEY, DEFAULT_KEEP, TOKEN_KEY, keep_count, normalize_backup_setting, parse_chat_ids,
)
from app.services.secret_box import reveal_bot_token
from app.services.users import DEFAULT_SETTINGS, SETTING_GROUPS, TOGGLE_KEYS, get_setting, set_setting
from tests.postgres_support import scoped_async_engine

TOKEN = "123456789:" + "A" * 35


class PruneTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        patcher = patch.object(backup_mod, "BACKUP_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)
        now = time.time()
        for index in range(8):
            archive = self.dir / f"{backup_mod.BACKUP_PREFIX}{index}.zip"
            archive.write_bytes(b"x")
            archive.with_suffix(".json").write_text("{}")
            os.utime(archive, (now - (8 - index) * 100, now - (8 - index) * 100))

    def names(self) -> set[str]:
        return {p.stem.replace(backup_mod.BACKUP_PREFIX, "") for p in self.dir.glob("*.zip")}

    def test_exactly_the_requested_number_of_newest_backups_remain(self) -> None:
        with patch.object(backup_mod, "read_restore_status", return_value={"state": "idle"}):
            removed = backup_mod.prune_backups(5)
        self.assertEqual(removed, 3)
        self.assertEqual(self.names(), {"3", "4", "5", "6", "7"})
        self.assertFalse(list(self.dir.glob("*0.json")))

    def test_fewer_backups_than_the_limit_are_all_kept(self) -> None:
        with patch.object(backup_mod, "read_restore_status", return_value={"state": "idle"}):
            self.assertEqual(backup_mod.prune_backups(15), 0)
        self.assertEqual(len(self.names()), 8)

    def test_nothing_is_deleted_while_a_restore_is_running(self) -> None:
        with patch.object(backup_mod, "read_restore_status", return_value={"state": "running"}):
            self.assertEqual(backup_mod.prune_backups(5), 0)
        self.assertEqual(len(self.names()), 8)


class SettingsTests(unittest.TestCase):
    def test_retention_choices_and_fallback(self) -> None:
        self.assertEqual([keep_count(v) for v in ("5", "10", "15")], [5, 10, 15])
        self.assertEqual([keep_count(v) for v in ("7", "", None, "abc", "100")], [DEFAULT_KEEP] * 5)
        with self.assertRaises(ValueError):
            normalize_backup_setting("backup_keep_count", "7")

    def test_token_is_validated_and_stored_encrypted(self) -> None:
        stored = normalize_backup_setting(TOKEN_KEY, TOKEN)
        self.assertNotIn(TOKEN, stored)
        self.assertEqual(reveal_bot_token(stored), TOKEN)
        with self.assertRaises(ValueError):
            normalize_backup_setting(TOKEN_KEY, "not-a-token")

    def test_chat_ids_are_cleaned_and_non_numbers_refused(self) -> None:
        self.assertEqual(parse_chat_ids("123, -1001 ,abc,123 456"), [123, -1001, 456])
        self.assertEqual(normalize_backup_setting(CHAT_IDS_KEY, "111 222,111"), "111, 222")
        with self.assertRaises(ValueError):
            normalize_backup_setting(CHAT_IDS_KEY, "abc")

    def test_new_switches_default_off_and_retention_is_a_dropdown_of_three_choices(self) -> None:
        for key in ("backup_telegram_enabled", "backup_telegram_custom_bot", "backup_telegram_allow_env"):
            self.assertIn(key, TOGGLE_KEYS)
            self.assertEqual(DEFAULT_SETTINGS[key], "0")
        field = next(f for f in SETTING_GROUPS["بکاپ زمان‌بندی"] if f[0] == "backup_keep_count")
        self.assertEqual(field[2], "select")
        self.assertEqual([value for value, _ in field[4]], ["5", "10", "15"])

    def test_the_risk_switch_shows_only_with_delivery_on_and_carries_a_warning(self) -> None:
        from app.services.users import SETTING_RISK_NOTES, SETTING_VISIBILITY

        field = next(f for f in SETTING_GROUPS["ارسال بکاپ به تلگرام"] if f[0] == tg.ALLOW_ENV_KEY)
        self.assertEqual(field[2], "toggle")
        self.assertTrue(field[1].startswith("تأیید ریسک"))
        self.assertEqual(SETTING_VISIBILITY[tg.ALLOW_ENV_KEY], (tg.ENABLED_KEY, ("1",)))
        self.assertIn("WEB_SECRET", SETTING_RISK_NOTES[tg.ALLOW_ENV_KEY])

    def test_a_shop_settings_import_cannot_turn_on_the_risk_switch(self) -> None:
        from app.services.shop_bundle import shop_bundle_allowed_setting_keys

        self.assertNotIn(tg.ALLOW_ENV_KEY, shop_bundle_allowed_setting_keys())


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.archive = Path(self._tmp.name) / "b.zip"
        self.archive.write_bytes(b"zip")
        self.bot = MagicMock()
        self.bot.send_document = AsyncMock()
        self.bot.send_message = AsyncMock()
        self.bot.session.close = AsyncMock()
        self.opened: list[str] = []

        def open_bot(token: str):
            self.opened.append(token)
            return self.bot

        for patcher in (
            patch.object(tg, "_open_bot", side_effect=open_bot),
            patch.object(tg, "get_settings", return_value=SimpleNamespace(bot_token="999:PANELTOKEN", admin_ids=[11, 22])),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self._tmp.cleanup()

    async def send(self, **kwargs) -> str:
        async with self.Session() as session:
            return await tg.send_backup(session, self.archive, include_env=kwargs.get("include_env", False), caption="c")

    async def status(self) -> str:
        async with self.Session() as session:
            return await get_setting(session, tg.STATUS_KEY, "")

    async def enable(self, **extra: str) -> None:
        async with self.Session() as session:
            await set_setting(session, "backup_telegram_enabled", "1")
            for key, value in extra.items():
                await set_setting(session, key, value)
            await session.commit()

    async def test_nothing_is_sent_while_the_switch_is_off(self) -> None:
        self.assertEqual(await self.send(), "disabled")
        self.bot.send_document.assert_not_awaited()

    async def test_by_default_the_panel_bot_sends_to_the_main_admins(self) -> None:
        await self.enable()
        self.assertEqual(await self.send(), "ok")
        self.assertEqual(self.opened, ["999:PANELTOKEN"])
        self.assertEqual([call.args[0] for call in self.bot.send_document.await_args_list], [11, 22])
        self.assertTrue((await self.status()).startswith("ok"))
        self.bot.session.close.assert_awaited()

    async def test_a_separate_backup_bot_uses_its_own_token_and_recipients(self) -> None:
        await self.enable(
            backup_telegram_custom_bot="1",
            **{TOKEN_KEY: normalize_backup_setting(TOKEN_KEY, TOKEN), CHAT_IDS_KEY: "555, 666"},
        )
        await self.send()
        self.assertEqual(self.opened, [TOKEN])
        self.assertEqual([call.args[0] for call in self.bot.send_document.await_args_list], [555, 666])

    async def test_a_backup_with_env_is_not_sent_until_the_risk_is_acknowledged(self) -> None:
        await self.enable()
        self.assertEqual(await self.send(include_env=True), "skipped")
        self.bot.send_document.assert_not_awaited()
        self.assertIn("تأیید ریسک", await self.status())

    async def test_a_backup_with_env_is_sent_with_a_warning_after_the_risk_is_acknowledged(self) -> None:
        await self.enable(**{tg.ALLOW_ENV_KEY: "1"})
        self.assertEqual(await self.send(include_env=True), "ok")
        self.assertEqual([call.args[0] for call in self.bot.send_document.await_args_list], [11, 22])
        caption = self.bot.send_document.await_args.kwargs["caption"]
        self.assertTrue(caption.startswith("c"))
        self.assertIn(".env", caption)
        self.assertLessEqual(len(caption), 900)
        self.assertIn("شامل .env", await self.status())

    async def test_the_acknowledgement_alone_does_not_send_while_delivery_is_off(self) -> None:
        async with self.Session() as session:
            await set_setting(session, tg.ALLOW_ENV_KEY, "1")
            await session.commit()
        self.assertEqual(await self.send(include_env=True), "disabled")
        self.bot.send_document.assert_not_awaited()

    async def test_a_long_caption_keeps_the_env_warning_within_the_telegram_limit(self) -> None:
        await self.enable(**{tg.ALLOW_ENV_KEY: "1"})
        async with self.Session() as session:
            await tg.send_backup(session, self.archive, include_env=True, caption="x" * 2000)
        caption = self.bot.send_document.await_args.kwargs["caption"]
        self.assertLessEqual(len(caption), 900)
        self.assertTrue(caption.endswith(tg.ENV_CAPTION_WARNING))

    async def test_a_backup_over_the_telegram_limit_is_skipped_with_a_visible_reason(self) -> None:
        await self.enable()
        with patch.object(tg, "MAX_UPLOAD_BYTES", 1):
            self.assertEqual(await self.send(), "skipped")
        self.assertIn("۵۰ مگابایت", await self.status())

    async def test_a_custom_bot_without_a_token_reports_an_error_instead_of_using_the_panel_bot(self) -> None:
        await self.enable(backup_telegram_custom_bot="1")
        self.assertEqual(await self.send(), "error")
        self.assertEqual(self.opened, [])

    async def test_one_failing_recipient_does_not_stop_the_others(self) -> None:
        await self.enable()
        self.bot.send_document.side_effect = [TelegramAPIError(MagicMock(), "blocked"), None]
        self.assertEqual(await self.send(), "ok")
        self.assertIn("1", await self.status())

    async def test_when_nobody_receives_it_the_status_is_an_error(self) -> None:
        await self.enable()
        self.bot.send_document.side_effect = TelegramAPIError(MagicMock(), "down")
        self.assertEqual(await self.send(), "error")

    async def test_the_backup_bot_token_is_not_part_of_the_shop_export(self) -> None:
        from app.services.shop_bundle import export_shop_bundle

        async with self.Session() as session:
            await set_setting(session, TOKEN_KEY, normalize_backup_setting(TOKEN_KEY, TOKEN))
            await session.commit()
            bundle = await export_shop_bundle(session)
        self.assertNotIn(TOKEN_KEY, bundle["settings"])

    async def test_the_test_message_reaches_every_recipient(self) -> None:
        async with self.Session() as session:
            self.assertEqual(await tg.send_test_message(session), 2)


class PayloadTests(unittest.IsolatedAsyncioTestCase):
    def test_payload_values_are_normalised_in_place_and_a_bad_value_returns_a_message(self) -> None:
        from app.services.backup_settings import normalize_backup_payload

        good = {"backup_keep_count": "15", CHAT_IDS_KEY: "111 222", TOKEN_KEY: TOKEN}
        self.assertIsNone(normalize_backup_payload(good))
        self.assertEqual(good[CHAT_IDS_KEY], "111, 222")
        self.assertNotEqual(good[TOKEN_KEY], TOKEN)
        self.assertIn("۵، ۱۰ یا ۱۵", normalize_backup_payload({"backup_keep_count": "20"}))
        self.assertIsNotNone(normalize_backup_payload({TOKEN_KEY: "bad"}))

    async def test_saving_a_smaller_limit_deletes_the_older_backups_at_once(self) -> None:
        from app.services.backup_settings import apply_backup_retention

        with tempfile.TemporaryDirectory() as tmp, patch.object(backup_mod, "BACKUP_DIR", Path(tmp)), \
                patch.object(backup_mod, "read_restore_status", return_value={"state": "idle"}):
            now = time.time()
            for index in range(7):
                archive = Path(tmp) / f"{backup_mod.BACKUP_PREFIX}{index}.zip"
                archive.write_bytes(b"x")
                os.utime(archive, (now - (7 - index), now - (7 - index)))
            self.assertEqual(await apply_backup_retention({"backup_keep_count": "5"}), 2)
            self.assertEqual(len(list(Path(tmp).glob("*.zip"))), 5)
            self.assertEqual(await apply_backup_retention({"other": "1"}), 0)

class RiskSwitchRenderTests(unittest.TestCase):
    def render(self, values: dict[str, str]) -> str:
        from app.api.app import templates

        group = SETTING_GROUPS["ارسال بکاپ به تلگرام"]
        field = next(f for f in group if f[0] == tg.ALLOW_ENV_KEY)
        return templates.get_template("_settings_field.html").render(field=field, values=values)

    def test_the_risk_switch_renders_its_danger_note_linked_to_the_switch(self) -> None:
        html = self.render({tg.ENABLED_KEY: "1", tg.ALLOW_ENV_KEY: "0"})
        self.assertIn('data-tone="danger"', html)
        self.assertIn('aria-describedby="risk-note-backup_telegram_allow_env"', html)
        self.assertIn('id="risk-note-backup_telegram_allow_env"', html)
        self.assertIn("setting-item--risk", html)
        self.assertNotIn("checked", html)
        self.assertNotIn(" hidden", html.split(">", 1)[0])

    def test_the_risk_switch_starts_hidden_while_delivery_is_off(self) -> None:
        html = self.render({tg.ENABLED_KEY: "0", tg.ALLOW_ENV_KEY: "1"})
        self.assertIn(" hidden", html.split(">", 1)[0])
        self.assertIn("checked", html)

    def test_an_ordinary_switch_has_no_risk_note(self) -> None:
        from app.api.app import templates

        field = next(f for f in SETTING_GROUPS["ارسال بکاپ به تلگرام"] if f[0] == tg.ENABLED_KEY)
        html = templates.get_template("_settings_field.html").render(field=field, values={})
        self.assertNotIn("ds-note", html)
        self.assertNotIn("aria-describedby", html)
