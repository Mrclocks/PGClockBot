"""«راه‌اندازی مجدد پنل» and «همگام‌سازی با پنل»: a restart is refused while an update or a restore runs and is
recorded only when it is allowed; the page then waits for a new boot. The resync reads every live service page
by page, stores the panel's expiry and volume, skips a failing page, and never runs twice at once."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import BotUser, Plan, ShopStaffAction, UserService
from app.runtime import BOOT_ID
from app.services import maintenance, service_resync
from tests.test_update_owner_only import OWNER_SESSION, SHOP_SESSION, RoutesHarness

EXPIRE_TS = int(datetime(2027, 1, 1, tzinfo=timezone.utc).timestamp())


class ToolHarness(RoutesHarness):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        service_resync._task = None
        service_resync._last_run = None

    async def call(self, session_payload: dict, method: str, path: str):
        cookie = f"session={self.signer.dumps(session_payload)}; csrf=token"
        headers = {"cookie": cookie, "origin": "http://panel.test", "x-csrf-token": "token"}
        return await self.client.request(method, path, headers=headers)

    async def actions(self) -> list[str]:
        async with self.Session() as session:
            return [row.reason for row in await session.scalars(select(ShopStaffAction).order_by(ShopStaffAction.id))]


class RestartTests(ToolHarness):
    def blockers(self, *, update: bool = False, restore: bool = False, scheduled: bool = True):
        return (patch.object(maintenance, "update_in_progress", return_value=update),
                patch.object(maintenance, "restore_in_progress", return_value=restore),
                patch.object(maintenance, "schedule_panel_restart", MagicMock(return_value=scheduled)))

    async def test_the_owner_schedules_one_restart_and_the_page_waits_for_a_new_boot(self) -> None:
        update, restore, schedule = self.blockers()
        with update, restore, schedule as scheduled:
            response = await self.call(OWNER_SESSION, "POST", "/tools/maintenance/restart")
        self.assertEqual(response.headers["location"], f"/tools/maintenance?restarting={BOOT_ID}")
        scheduled.assert_called_once()
        self.assertEqual(await self.actions(), ["درخواست راه‌اندازی مجدد پنل"])
        page = (await self.call(OWNER_SESSION, "GET", response.headers["location"])).text
        self.assertIn(f'data-restart-wait="{BOOT_ID}"', page)
        self.assertIn("/static/restart-wait.js", page)

    async def test_an_update_or_a_restore_in_progress_blocks_the_restart(self) -> None:
        for kind in ("update", "restore"):
            update, restore, schedule = self.blockers(**{kind: True})
            with self.subTest(kind=kind), update, restore, schedule as scheduled:
                response = await self.call(OWNER_SESSION, "POST", "/tools/maintenance/restart")
                self.assertIn("/tools/maintenance?err=", response.headers["location"])
                scheduled.assert_not_called()
        self.assertEqual(await self.actions(), [])

    async def test_a_blocked_restart_has_no_button(self) -> None:
        update, restore, schedule = self.blockers(update=True)
        with update, restore, schedule:
            page = (await self.call(OWNER_SESSION, "GET", "/tools/maintenance")).text
        self.assertNotIn('action="/tools/maintenance/restart"', page)
        self.assertIn("آپدیت پنل در جریان است", page)

    async def test_a_restart_already_scheduled_is_reported(self) -> None:
        update, restore, schedule = self.blockers(scheduled=False)
        with update, restore, schedule:
            response = await self.call(OWNER_SESSION, "POST", "/tools/maintenance/restart")
        self.assertIn("/tools/maintenance?err=", response.headers["location"])

    async def test_a_shop_cannot_restart_or_resync(self) -> None:
        update, restore, schedule = self.blockers()
        with update, restore, schedule as scheduled:
            for path in ("/tools/maintenance/restart", "/tools/maintenance/resync"):
                response = await self.call(SHOP_SESSION, "POST", path)
                self.assertEqual(response.headers["location"], "/home")
        scheduled.assert_not_called()
        self.assertIsNone(service_resync._task)

    async def test_a_hostile_restarting_value_is_escaped_and_cut(self) -> None:
        page = (await self.call(OWNER_SESSION, "GET", '/tools/maintenance?restarting="><script>x</script>' + "a" * 200)).text
        self.assertNotIn('"><script>x</script>', page)
        self.assertNotIn("a" * 100, page)


class ResyncTests(ToolHarness):
    async def seed_services(self) -> None:
        async with self.Session() as session:
            session.add_all([BotUser(id=1, telegram_id=101, referral_code="u1"), Plan(id=1, name="P", price=1)])
            await session.flush()
            session.add_all([
                UserService(id=11, bot_user_id=1, plan_id=1, pg_user_id=501, pg_username="a"),
                UserService(id=12, bot_user_id=1, plan_id=1, pg_user_id=502, pg_username="b"),
                UserService(id=13, bot_user_id=1, plan_id=1, pg_user_id=503, pg_username="c"),
                UserService(id=14, bot_user_id=1, plan_id=1, pg_user_id=504, pg_username="d", is_cancelled=True),
                UserService(id=15, bot_user_id=1, plan_id=1, pg_user_id=None, pg_username="e"),
            ])
            await session.commit()

    async def run_resync(self, panel_infos) -> service_resync.ResyncRun:
        with patch.object(service_resync, "SessionLocal", self.Session), \
                patch.object(service_resync, "get_pg", return_value=object()), \
                patch.object(service_resync, "panel_infos", panel_infos), \
                patch.object(service_resync, "RESYNC_BATCH", 2):
            response = await self.call(OWNER_SESSION, "POST", "/tools/maintenance/resync")
            self.assertIn("/tools/maintenance?ok=", response.headers["location"])
            await service_resync._task
        return service_resync._last_run

    async def test_every_live_service_is_read_page_by_page_and_stored(self) -> None:
        await self.seed_services()
        asked: list[list[int]] = []

        async def infos(_pg, services):
            asked.append([int(s.id) for s in services])
            return [{"status": "active", "expire": EXPIRE_TS, "data_limit": 5 * 1024**3} for _ in services]

        run = await self.run_resync(infos)
        self.assertEqual(asked, [[11, 12], [13]])
        self.assertEqual((run.read, run.synced, run.unread, run.failed_pages), (3, 3, 0, 0))
        async with self.Session() as session:
            stored = {int(s.id): s.quota_expire_at for s in await session.scalars(select(UserService))}
        self.assertEqual({sid: int(stored[sid].timestamp()) for sid in (11, 12, 13)}, dict.fromkeys((11, 12, 13), EXPIRE_TS))
        self.assertIsNone(stored[14])
        self.assertEqual(await self.actions(), ["همگام‌سازی سرویس‌ها با پنل"])

    async def test_a_failing_page_is_skipped_and_the_run_goes_on(self) -> None:
        await self.seed_services()

        async def infos(_pg, services):
            if int(services[0].id) == 11:
                raise TimeoutError("panel slow")
            return [None for _ in services]

        run = await self.run_resync(infos)
        self.assertEqual((run.read, run.synced, run.unread, run.failed_pages), (3, 0, 1, 1))
        self.assertIsNotNone(run.finished_at)

    async def test_a_second_resync_while_one_runs_is_refused(self) -> None:
        await self.seed_services()
        release = asyncio.Event()

        async def infos(_pg, services):
            await release.wait()
            return [None for _ in services]

        with patch.object(service_resync, "SessionLocal", self.Session), \
                patch.object(service_resync, "get_pg", return_value=object()), \
                patch.object(service_resync, "panel_infos", infos):
            await self.call(OWNER_SESSION, "POST", "/tools/maintenance/resync")
            second = await self.call(OWNER_SESSION, "POST", "/tools/maintenance/resync")
            release.set()
            await service_resync._task
        self.assertIn("/tools/maintenance?err=", second.headers["location"])
        self.assertEqual(await self.actions(), ["همگام‌سازی سرویس‌ها با پنل"])
