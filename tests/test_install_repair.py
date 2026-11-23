"""«تعمیر نصب»: only the owner, only after typing the word, never while an update or restore runs, never when a
newer version waits, and never without a full backup first; then the panel's own update runs once."""

from __future__ import annotations

import asyncio
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import ShopStaffAction
from app.services import install_repair, maintenance
from app.services.backup_postgres import BackupDatabaseError
from tests.test_update_owner_only import OWNER_SESSION, SHOP_SESSION, RoutesHarness

UP_TO_DATE = {"remote_version": "1.0.0", "update_available": False}


def repair_patches(*, info: dict = UP_TO_DATE, preflight: dict | None = None, backup=None, started: dict | None = None):
    return {
        "check": patch.object(install_repair, "check_github_update", AsyncMock(return_value=info)),
        "preflight": patch.object(install_repair, "migration_preflight", AsyncMock(return_value=preflight or {"blocked": False})),
        "channel": patch.object(install_repair, "get_update_channel", return_value="main"),
        "backup": patch.object(install_repair, "create_backup", backup or MagicMock(return_value={"id": "b1"})),
        "start": patch.object(install_repair, "start_update", MagicMock(return_value=started or {"ok": True})),
        "version": patch.object(install_repair, "local_version", return_value="1.0.0"),
    }


class RepairFlowTests(IsolatedAsyncioTestCase):
    async def run_repair(self, **kwargs) -> tuple[install_repair.RepairRun, dict]:
        patches = repair_patches(**kwargs)
        mocks = {name: p.start() for name, p in patches.items()}
        self.addCleanup(lambda: [p.stop() for p in patches.values()])
        run = install_repair.RepairRun(started_at=install_repair.datetime.now(install_repair.timezone.utc))
        await install_repair._repair(run, "owner")
        return run, mocks

    async def test_a_backup_comes_first_then_the_same_version_is_installed_again(self) -> None:
        run, mocks = await self.run_repair()
        self.assertIsNone(run.error)
        self.assertEqual(run.step, "update")
        mocks["backup"].assert_called_once_with(note="پیش از تعمیر نصب", include_env=False, created_by="owner")
        mocks["start"].assert_called_once_with("1.0.0")

    async def test_nothing_is_touched_when_a_newer_version_waits_or_github_is_unknown(self) -> None:
        for info in ({"remote_version": "2.0.0", "update_available": True}, {"remote_version": None}):
            with self.subTest(info=info):
                run, mocks = await self.run_repair(info=info)
                self.assertIsNotNone(run.error)
                mocks["backup"].assert_not_called()
                mocks["start"].assert_not_called()

    async def test_an_incompatible_migration_stops_before_the_backup(self) -> None:
        run, mocks = await self.run_repair(preflight={"blocked": True, "message": "مایگریشن ناسازگار"})
        self.assertEqual(run.error, "مایگریشن ناسازگار")
        mocks["backup"].assert_not_called()

    async def test_a_failed_backup_stops_the_repair(self) -> None:
        run, mocks = await self.run_repair(backup=MagicMock(side_effect=BackupDatabaseError("ابزار pg_dump نصب نیست")))
        self.assertIn("بکاپ گرفته نشد", run.error)
        mocks["start"].assert_not_called()

    async def test_an_update_that_refuses_to_start_is_reported(self) -> None:
        run, _ = await self.run_repair(started={"ok": False, "error": "یک عملیات آپدیت در حال اجراست"})
        self.assertEqual(run.error, "یک عملیات آپدیت در حال اجراست")

    async def test_an_unexpected_error_shows_a_safe_message(self) -> None:
        run, _ = await self.run_repair(backup=MagicMock(side_effect=OSError("/root/secret path")))
        self.assertEqual(run.error, install_repair.NOT_DONE)


class RepairRouteTests(RoutesHarness):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        install_repair._task = None
        install_repair._last_run = None
        for target, value in ((maintenance, "update_in_progress"), (maintenance, "restore_in_progress")):
            patcher = patch.object(target, value, return_value=False)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def post(self, session_payload: dict, phrase: str | None):
        cookie = f"session={self.signer.dumps(session_payload)}; csrf=token"
        headers = {"cookie": cookie, "origin": "http://panel.test", "x-csrf-token": "token"}
        data = {} if phrase is None else {"confirm_phrase": phrase}
        return await self.client.post("/tools/maintenance/repair", headers=headers, data=data)

    async def actions(self) -> list[str]:
        async with self.Session() as session:
            return [row.reason for row in await session.scalars(select(ShopStaffAction))]

    async def test_the_word_must_be_typed(self) -> None:
        with patch.object(install_repair, "_repair", AsyncMock()) as repair:
            for phrase in (None, "", "repair", "تعمیرات"):
                with self.subTest(phrase=phrase):
                    response = await self.post(OWNER_SESSION, phrase)
                    self.assertIn("/tools/maintenance?err=", response.headers["location"])
            repair.assert_not_called()
        self.assertEqual(await self.actions(), [])

    async def test_the_owner_starts_one_repair_and_it_is_recorded(self) -> None:
        release = asyncio.Event()

        async def slow(_run, _who):
            await release.wait()

        with patch.object(install_repair, "_repair", slow):
            first = await self.post(OWNER_SESSION, "تعمیر")
            second = await self.post(OWNER_SESSION, "تعمیر")
            release.set()
            await install_repair._task
        self.assertIn("/tools/maintenance?ok=", first.headers["location"])
        self.assertIn("/tools/maintenance?err=", second.headers["location"])
        self.assertEqual(await self.actions(), ["تعمیر نصب"])

    async def test_an_update_in_progress_blocks_the_repair(self) -> None:
        with patch.object(maintenance, "update_in_progress", return_value=True), \
                patch.object(install_repair, "_repair", AsyncMock()) as repair:
            response = await self.post(OWNER_SESSION, "تعمیر")
        self.assertIn("/tools/maintenance?err=", response.headers["location"])
        repair.assert_not_called()

    async def test_a_shop_cannot_repair(self) -> None:
        with patch.object(install_repair, "_repair", AsyncMock()) as repair:
            response = await self.post(SHOP_SESSION, "تعمیر")
        self.assertEqual(response.headers["location"], "/home")
        repair.assert_not_called()

    async def test_the_row_asks_for_the_typed_word(self) -> None:
        cookie = f"session={self.signer.dumps(OWNER_SESSION)}"
        page = (await self.client.get("/tools/maintenance", headers={"cookie": cookie})).text
        self.assertIn('action="/tools/maintenance/repair"', page)
        self.assertIn('data-confirm-phrase="تعمیر"', page)
