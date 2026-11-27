"""Customer link change: one revoke per change, inside the shop, and never repeated when the answer is lost."""

from __future__ import annotations

import asyncio
import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Base, BotUser, Order, Plan, ServiceFreeze, ServiceLinkReset, ServiceTransfer, UserService
from app.services import service_link_reset as reset
from app.services.pasarguard import PasarGuardError
from tests.postgres_support import scoped_async_engine, scoped_sync_engine

ROOT = Path(__file__).resolve().parents[1]
UI = {"service_link_reset_enabled": "1", "service_link_reset_plans": "all", "service_link_reset_per_period": "2"}
OWNER, OTHER, SHOP = 1, 2, 7
OLD_URL = "https://sub.example/sub/OLDTOKEN000"
NEW_URL = "https://sub.example/sub/NEWTOKEN123"


class FakePanel:
    """Revokes once per call; ``lose_answer`` makes the call time out after the panel changed (or not)."""

    def __init__(self) -> None:
        self.revoked: list[int] = []
        self.current = OLD_URL
        self.lose_answer = False
        self.apply_when_lost = True
        self.refuse: int | None = None
        self.read_fails = False

    async def revoke_sub_by_id(self, user_id: int) -> dict:
        if self.refuse is not None:
            raise PasarGuardError("refused", self.refuse, "{}")
        self.revoked.append(user_id)
        if not self.lose_answer or self.apply_when_lost:
            self.current = NEW_URL if self.current == OLD_URL else NEW_URL.replace("123", "456")
        if self.lose_answer:
            raise TimeoutError("answer lost")
        return {"id": user_id, "subscription_url": self.current}

    async def get_user_by_id(self, user_id: int, *, use_cache: bool = True) -> dict:
        assert use_cache is False
        if self.read_fails:
            raise TimeoutError("panel down")
        return {"id": user_id, "username": "svc", "subscription_url": self.current}


class LinkResetHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        self.pg = FakePanel()
        for target in ("app.services.service_link_reset.get_pg",):
            patcher = patch(target, return_value=self.pg)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch("app.services.service_link_reset.get_pg_for_reseller", AsyncMock(return_value=self.pg))
        patcher.start()
        self.addCleanup(patcher.stop)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=OWNER, telegram_id=1001, referral_code="c1"),
                BotUser(id=OTHER, telegram_id=1002, referral_code="c2"),
                BotUser(id=SHOP, telegram_id=1007, referral_code="s", role="reseller"),
            ])
            await session.flush()
            session.add_all([Plan(id=1, name="month", price=100), Plan(id=2, name="trial", price=0, is_trial=True)])
            await session.flush()
            session.add_all([
                UserService(id=10, bot_user_id=OWNER, plan_id=1, pg_user_id=500, pg_username="svc<b>",
                            subscription_url=OLD_URL, subscription_token="OLDTOKEN000"),
                UserService(id=11, bot_user_id=OWNER, plan_id=1, pg_user_id=501, pg_username="shop",
                            subscription_url=OLD_URL, subscription_token="OLDTOKEN000", shop_id=SHOP),
            ])
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def start(self, *, user_id: int = OWNER, service_id: int = 10, shop_id: int | None = None,
                    ui: dict | None = None) -> reset.LinkResetOutcome:
        async with self.Session() as session:
            return await reset.start_link_reset(
                session, await session.get(BotUser, user_id), service_id, shop_id=shop_id, ui=ui or UI
            )

    async def service(self, service_id: int = 10) -> UserService:
        async with self.Session() as session:
            return await session.get(UserService, service_id)

    async def resets(self) -> list[ServiceLinkReset]:
        async with self.Session() as session:
            return list(await session.scalars(select(ServiceLinkReset).order_by(ServiceLinkReset.id)))

    async def age_resets(self, minutes: int) -> None:
        async with self.Session() as session:
            await session.execute(sa.update(ServiceLinkReset).values(
                created_at=ServiceLinkReset.created_at - timedelta(minutes=minutes)))
            await session.commit()

    async def run_job(self, *, later: timedelta = timedelta()) -> int:
        async with self.Session() as session:
            return await reset.process_open_link_resets(session, now=datetime.now(timezone.utc) + later)



class LinkResetTests(LinkResetHarness):
    async def test_the_new_link_is_saved_and_only_its_hash_of_the_old_token_is_kept(self) -> None:
        outcome = await self.start()
        self.assertEqual((outcome.status, outcome.link.url), ("done", NEW_URL))
        service = await self.service()
        self.assertEqual((service.subscription_url, service.subscription_token), (NEW_URL, "NEWTOKEN123"))
        (row,) = await self.resets()
        self.assertEqual((row.status, row.shop_id, row.user_id, row.pg_user_id), ("done", None, OWNER, 500))
        self.assertEqual(row.old_token_hash, reset.token_hash("OLDTOKEN000"))
        self.assertNotIn("OLDTOKEN000", repr(row.__dict__))
        self.assertEqual(self.pg.revoked, [500])

    async def test_two_taps_at_once_revoke_the_link_once(self) -> None:
        results = await asyncio.gather(self.start(), self.start(), return_exceptions=True)
        self.assertEqual(sorted(type(r).__name__ for r in results), ["LinkResetOutcome", "ValueError"])
        self.assertEqual(self.pg.revoked, [500])
        self.assertEqual([r.status for r in await self.resets()], ["done"])

    async def test_a_second_change_waits_for_the_cooldown_and_the_limit_counts_done_changes(self) -> None:
        await self.start()
        with self.assertRaisesRegex(ValueError, "تازگی"):
            await self.start()
        await self.age_resets(11)
        await self.start()
        await self.age_resets(11)
        with self.assertRaisesRegex(ValueError, "سقف"):
            await self.start()
        async with self.Session() as session:
            policy = reset.link_reset_policy(UI)
            self.assertEqual(await reset.changes_left(session, 10, policy, datetime.now(timezone.utc)), 0)
            later = datetime.now(timezone.utc) + timedelta(days=31)
            self.assertEqual(await reset.changes_left(session, 10, policy, later), 2)
        self.assertEqual(len(self.pg.revoked), 2)

    async def test_a_panel_refusal_fails_without_counting_and_can_be_tried_again(self) -> None:
        self.pg.refuse = 403
        with self.assertRaisesRegex(ValueError, "اجازه"):
            await self.start()
        self.assertEqual([r.status for r in await self.resets()], ["failed"])
        self.assertEqual((await self.service()).subscription_url, OLD_URL)
        self.pg.refuse = None
        self.assertEqual((await self.start()).status, "done")

    async def test_a_lost_answer_after_the_panel_changed_the_link_is_finished_by_reading_it(self) -> None:
        self.pg.lose_answer = True
        outcome = await self.start()
        self.assertEqual(outcome.status, "done")
        self.assertEqual((await self.service()).subscription_token, "NEWTOKEN123")
        self.assertEqual(self.pg.revoked, [500])

    async def test_a_lost_answer_that_changed_nothing_fails_only_after_the_answer_can_no_longer_arrive(self) -> None:
        self.pg.lose_answer, self.pg.apply_when_lost = True, False
        self.assertEqual((await self.start()).status, "unknown")
        self.assertEqual(await self.run_job(), 0)
        self.assertEqual([r.status for r in await self.resets()], ["unknown"])
        await self.age_resets(6)
        await self.run_job()
        self.assertEqual([r.status for r in await self.resets()], ["failed"])
        self.assertEqual(self.pg.revoked, [500])
        self.assertEqual((await self.service()).subscription_url, OLD_URL)

    async def test_an_unreadable_panel_leaves_the_change_open_until_the_job_sees_the_new_link(self) -> None:
        self.pg.lose_answer, self.pg.read_fails = True, True
        self.assertEqual((await self.start()).status, "unknown")
        with self.assertRaisesRegex(ValueError, "در حال انجام"):
            await self.start()
        await self.age_resets(30)
        self.assertEqual(await self.run_job(), 0)
        self.assertEqual([r.status for r in await self.resets()], ["unknown"])
        self.pg.read_fails = False
        self.assertEqual(await self.run_job(), 1)
        self.assertEqual((await self.service()).subscription_token, "NEWTOKEN123")
        self.assertEqual(self.pg.revoked, [500])

    async def test_a_change_left_pending_by_a_crash_is_resolved_by_the_job_without_a_revoke(self) -> None:
        async with self.Session() as session:
            session.add(ServiceLinkReset(service_id=10, user_id=OWNER, pg_user_id=500, status="pending",
                                         old_token_hash=reset.token_hash("OLDTOKEN000")))
            await session.commit()
        self.assertEqual(await self.run_job(), 0)
        self.assertEqual([r.status for r in await self.resets()], ["pending"])
        self.pg.current = NEW_URL
        await self.age_resets(6)
        self.assertEqual(await self.run_job(), 1)
        self.assertEqual((await self.service()).subscription_url, NEW_URL)
        self.assertEqual(self.pg.revoked, [])

    async def test_another_customer_shop_or_a_blocked_customer_cannot_change_the_link(self) -> None:
        cases = ({"user_id": OTHER}, {"shop_id": SHOP}, {"service_id": 11}, {"service_id": 999})
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, "یافت نشد"):
                await self.start(**kwargs)
        async with self.Session() as session:
            (await session.get(BotUser, OWNER)).is_blocked = True
            await session.commit()
        with self.assertRaisesRegex(ValueError, "یافت نشد"):
            await self.start()
        self.assertEqual((self.pg.revoked, await self.resets()), ([], []))

    async def test_the_shops_own_service_uses_the_shops_panel_and_changes_only_its_rows(self) -> None:
        outcome = await self.start(service_id=11, shop_id=SHOP)
        self.assertEqual(outcome.status, "done")
        self.assertEqual(((await self.service(11)).subscription_url, (await self.service(10)).subscription_url),
                         (NEW_URL, OLD_URL))
        reset.get_pg_for_reseller.assert_awaited()

    async def test_off_unchosen_plans_and_trials_are_not_offered(self) -> None:
        async with self.Session() as session:
            service = await session.get(UserService, 10)
            self.assertTrue(await reset.link_reset_button(session, service, UI))
            for ui in ({}, {**UI, "service_link_reset_enabled": "0"}, {**UI, "service_link_reset_plans": "2"}):
                with self.subTest(ui=ui):
                    self.assertFalse(await reset.link_reset_button(session, service, ui))
            service.plan_id = 2
            await session.commit()
            self.assertFalse(await reset.link_reset_button(session, service, UI))
        with self.assertRaisesRegex(ValueError, "فعال نیست"):
            await self.start()

    async def test_a_frozen_service_an_open_transfer_or_an_open_order_blocks_the_change(self) -> None:
        later = datetime.now(timezone.utc) + timedelta(days=1)
        blockers = (
            (ServiceFreeze(service_id=10, user_id=OWNER, status="frozen", remaining_seconds=60, resume_by=later),
             "متوقف"),
            (ServiceTransfer(service_id=10, from_user_id=OWNER, token_hash="h", status="open", expires_at=later),
             "انتقال"),
            (Order(user_id=OWNER, plan_id=1, amount=100, status="awaiting_receipt", service_id=10), "سفارش"),
        )
        for blocker, message in blockers:
            async with self.Session() as session:
                session.add(blocker)
                await session.commit()
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                await self.start()
            async with self.Session() as session:
                await session.delete(await session.get(type(blocker), blocker.id))
                await session.commit()
        self.assertEqual(self.pg.revoked, [])

    async def test_a_service_without_a_saved_link_is_refused_before_the_panel(self) -> None:
        async with self.Session() as session:
            service = await session.get(UserService, 10)
            service.subscription_url, service.subscription_token = None, None
            await session.commit()
        with self.assertRaisesRegex(ValueError, "ثبت نشده"):
            await self.start()
        self.assertEqual((self.pg.revoked, await self.resets()), ([], []))

    async def test_the_stuck_work_page_counts_old_open_changes(self) -> None:
        from app.services.stuck_work import stuck_counts

        self.pg.lose_answer, self.pg.read_fails = True, True
        await self.start()
        async with self.Session() as session:
            self.assertEqual((await stuck_counts(session))["link_resets"], 0)
            later = datetime.now(timezone.utc) + timedelta(minutes=20)
            self.assertEqual((await stuck_counts(session, now=later))["link_resets"], 1)


class LinkResetBotTests(LinkResetHarness):
    """The bot screens: the button, the escaped warning and the new link."""

    def callback(self, data: str) -> SimpleNamespace:
        return SimpleNamespace(data=data, answer=AsyncMock(), message=SimpleNamespace())

    async def test_the_more_menu_shows_the_button_only_when_offered(self) -> None:
        from app.bot.nav_inline import service_more_keyboard

        def callbacks(markup) -> list[str]:
            return [button.callback_data for row in markup.inline_keyboard for button in row]

        self.assertIn("svc:lnk:10", callbacks(service_more_keyboard(10, {}, link_reset=True)))
        self.assertNotIn("svc:lnk:10", callbacks(service_more_keyboard(10, {})))

    async def test_the_warning_escapes_the_service_name_and_the_confirm_shows_the_new_link(self) -> None:
        from app.bot.handlers import service_link_reset as screen

        edits = AsyncMock()
        with (
            patch.object(screen, "safe_edit_text", edits),
            patch.object(screen, "get_all_settings", AsyncMock(return_value=UI)),
            patch.object(screen, "current_shop_reseller_id", return_value=None),
        ):
            async with self.Session() as session:
                owner = await session.get(BotUser, OWNER)
                await screen.link_reset_ask(self.callback("svc:lnk:10"), session, owner)
                await screen.link_reset_confirm(self.callback("svc:lnkok:10"), session, owner)
                denied = self.callback("svc:lnkok:10")
                await screen.link_reset_confirm(denied, session, await session.get(BotUser, OTHER))
        warning, done = (call.args[1] for call in edits.await_args_list)
        self.assertIn("svc&lt;b&gt;", warning)
        self.assertNotIn("svc<b>", warning)
        self.assertIn(NEW_URL, done)
        self.assertEqual(denied.answer.await_args.kwargs.get("show_alert"), True)
        self.assertEqual(self.pg.revoked, [500])


class LinkResetMigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade(self) -> None:
        path = ROOT / "alembic/versions/0142_service_link_resets.py"
        spec = importlib.util.spec_from_file_location("link_resets", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = scoped_sync_engine()
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE bot_users (id INTEGER PRIMARY KEY)"))
            conn.execute(text("CREATE TABLE user_services (id INTEGER PRIMARY KEY)"))
            conn.execute(text("INSERT INTO bot_users VALUES (1)"))
            conn.execute(text("INSERT INTO user_services VALUES (10)"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                insert = text("INSERT INTO service_link_resets "
                              "(service_id, user_id, pg_user_id, old_token_hash, status) "
                              "VALUES (10, 1, 500, 'h', :status)")
                conn.execute(insert, {"status": "done"})
                conn.execute(insert, {"status": "pending"})
                with self.assertRaises(sa.exc.IntegrityError), conn.begin_nested():
                    conn.execute(insert, {"status": "unknown"})
                migration.downgrade()
                self.assertFalse(sa.inspect(conn).has_table("service_link_resets"))
                self.assertEqual(conn.scalar(text("SELECT count(*) FROM user_services")), 1)
                migration.upgrade()
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
