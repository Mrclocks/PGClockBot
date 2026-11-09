"""The cleanup deletes only what is certainly due, fails closed on every doubt, and never deletes twice."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.db.models import BotUser, CustomerNotice, Order, ServiceAutomation, ShopStaffAction, UserService
from app.services import service_cleanup_delete as delete
from app.services.pasarguard import PasarGuardError
from app.services.service_cleanup import KIND_TIME, KIND_VOLUME, CleanupPolicy
from app.services.users import clear_settings_cache

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
SHOP = 5
ENABLED = NOW - timedelta(days=90)
OLD = NOW - timedelta(days=10)  # expired 10 days ago: due with a 7-day rule


def policy(**kw) -> CleanupPolicy:
    base = dict(time_on=True, time_days=7, time_enabled_at=ENABLED, volume_on=True, volume_days=30, volume_enabled_at=ENABLED)
    return CleanupPolicy(**{**base, **kw})


class FakePanel:
    def __init__(self, users: dict[int, dict], *, delete_error: Exception | None = None) -> None:
        self.users = users
        self.delete_error = delete_error
        self.deleted: list[int] = []
        self.disabled: list[int] = []
        self.reads: list[int] = []

    async def get_user_by_id(self, user_id: int):
        self.reads.append(user_id)
        if user_id not in self.users:
            raise PasarGuardError("not found", 404)
        return self.users[user_id]

    async def delete_user_by_id(self, user_id: int) -> None:
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append(user_id)
        self.users.pop(user_id, None)

    async def set_disabled_by_id(self, user_id: int, disabled: bool) -> dict:
        self.disabled.append(user_id)
        return {}


def expired(expire: datetime = OLD) -> dict:
    return {"status": "expired", "expire": int(expire.timestamp()), "data_limit": 100, "used_traffic": 5}


class DeleteCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 'd.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        clear_settings_cache()
        async with self.Session() as session:
            session.add_all([
                BotUser(id=SHOP, telegram_id=105, referral_code="s5", role="reseller"),
                BotUser(id=6, telegram_id=106, referral_code="s6", role="reseller"),
                BotUser(id=20, telegram_id=120, referral_code="c20", reseller_id=SHOP),
                BotUser(id=21, telegram_id=121, referral_code="c21", reseller_id=6),
            ])
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self._tmp.cleanup()
        clear_settings_cache()

    async def add(self, service_id: int, owner: int = 20, shop: int | None = SHOP, **fields) -> None:
        fields.setdefault("cleanup_warn_stage", 1)
        fields.setdefault("cleanup_warned_at", NOW - timedelta(days=3))
        async with self.Session() as session:
            session.add(UserService(id=service_id, bot_user_id=owner, pg_user_id=1000 + service_id, pg_username=f"svc{service_id}",
                                    shop_id=shop, quota_expire_at=fields.pop("quota_expire_at", OLD), **fields))
            await session.commit()

    async def sweep(self, panel: FakePanel, *, shop: int | None = SHOP, kind: str = KIND_TIME, pol: CleanupPolicy | None = None,
                  at: datetime = NOW, limit: int = 25):
        with patch("app.services.bot_user_admin._pg_client_for_bot_service", AsyncMock(return_value=panel)):
            async with self.Session() as session:
                return await delete.delete_due_services(session, shop_id=shop, policy=pol or policy(), kind=kind, now=at, limit=limit)

    async def exists(self, service_id: int) -> bool:
        async with self.Session() as session:
            return await session.get(UserService, service_id) is not None

    async def table(self, model):
        async with self.Session() as session:
            return list((await session.scalars(select(model).order_by(model.id))).all())


class DeletionTests(DeleteCase):
    async def test_a_due_service_is_deleted_logged_and_the_customer_told(self) -> None:
        await self.add(1)
        panel = FakePanel({1001: expired()})
        result = await self.sweep(panel)
        self.assertEqual((result.deleted, result.failed), (1, 0))
        self.assertEqual(panel.deleted, [1001])
        self.assertFalse(await self.exists(1))
        log = (await self.table(ShopStaffAction))[0]
        self.assertEqual((log.action, log.actor_kind, log.target_type, log.target_id, log.shop_id), ("service_auto_delete", "system", "service", 1, SHOP))
        self.assertIn("7 روز", log.reason)
        notice = (await self.table(CustomerNotice))[0]
        self.assertEqual((notice.action, notice.telegram_id, notice.shop_id, notice.service_id), ("cleanup_deleted", 120, SHOP, None))

    async def test_a_second_run_deletes_nothing_more(self) -> None:
        await self.add(1)
        panel = FakePanel({1001: expired()})
        await self.sweep(panel)
        again = await self.sweep(panel)
        self.assertEqual((again.deleted, panel.deleted), (0, [1001]))
        self.assertEqual(len(await self.table(CustomerNotice)), 1)

    async def test_a_volume_finished_service_is_deleted_on_its_own_days_and_notice(self) -> None:
        await self.add(1, quota_expire_at=NOW + timedelta(days=60), volume_exhausted_at=NOW - timedelta(days=31), cleanup_volume_warn_stage=1)
        panel = FakePanel({1001: {"status": "limited", "expire": int((NOW + timedelta(days=60)).timestamp()), "data_limit": 100, "used_traffic": 100}})
        result = await self.sweep(panel, kind=KIND_VOLUME)
        self.assertEqual(result.deleted, 1)
        self.assertEqual((await self.table(CustomerNotice))[0].action, "cleanup_volume_deleted")

    async def test_the_batch_is_bounded(self) -> None:
        for i in range(1, 6):
            await self.add(i)
        panel = FakePanel({1000 + i: expired() for i in range(1, 6)})
        self.assertEqual((await self.sweep(panel, limit=2)).deleted, 2)
        self.assertEqual(len(panel.deleted), 2)


class LastMomentChecksTests(DeleteCase):
    async def test_a_warning_queued_a_moment_ago_is_never_followed_by_the_deletion(self) -> None:
        await self.add(1, cleanup_warned_at=NOW - timedelta(hours=1))
        panel = FakePanel({1001: expired()})
        self.assertEqual((await self.sweep(panel)).deleted, 0)
        self.assertEqual((await self.sweep(panel, at=NOW + timedelta(hours=6))).deleted, 1)

    async def test_a_service_exempted_while_the_batch_runs_is_not_deleted(self) -> None:
        await self.add(1)
        await self.add(2)
        original = delete._claim_lease

        async def exempt_then_claim(session, service_id, **kwargs):
            if service_id == 2:
                async with self.Session() as other:
                    await other.execute(update(UserService).where(UserService.id == 2).values(cleanup_exempt=True))
                    await other.commit()
            return await original(session, service_id, **kwargs)

        panel = FakePanel({1001: expired(), 1002: expired()})
        with patch.object(delete, "_claim_lease", exempt_then_claim):
            result = await self.sweep(panel)
        self.assertEqual((result.deleted, panel.deleted), (1, [1001]))
        self.assertTrue(await self.exists(2))

    async def test_a_service_given_more_days_while_the_batch_runs_is_not_deleted(self) -> None:
        await self.add(1)
        original = delete._claim_lease

        async def hold_then_claim(session, service_id, **kwargs):
            async with self.Session() as other:
                await other.execute(update(UserService).where(UserService.id == 1).values(cleanup_hold_until=NOW + timedelta(days=7)))
                await other.commit()
            return await original(session, service_id, **kwargs)

        panel = FakePanel({1001: expired()})
        with patch.object(delete, "_claim_lease", hold_then_claim):
            self.assertEqual((await self.sweep(panel)).deleted, 0)
        self.assertEqual(panel.deleted, [])

    async def test_an_order_opened_while_the_batch_runs_stops_the_deletion(self) -> None:
        await self.add(1)
        panel = FakePanel({1001: expired()})
        checks = AsyncMock(side_effect=[set(), {1}])  # clear at the start of the batch, open right before the delete
        with patch.object(delete, "_blocked_by_other_state", checks):
            result = await self.sweep(panel)
        self.assertEqual((result.deleted, panel.deleted), (0, []))
        self.assertTrue(await self.exists(1))

    async def test_the_log_record_is_written_with_the_delete_or_not_at_all(self) -> None:
        await self.add(1)
        failing = FakePanel({1001: expired()}, delete_error=PasarGuardError("bad gateway", 502))
        await self.sweep(failing)
        self.assertEqual(await self.table(ShopStaffAction), [])
        await self.sweep(FakePanel({1001: expired()}), at=NOW + timedelta(hours=2))
        self.assertEqual([a.action for a in await self.table(ShopStaffAction)], ["service_auto_delete"])


class NotYetDueTests(DeleteCase):
    async def test_only_services_past_the_deadline_with_a_sent_notice_are_deleted(self) -> None:
        await self.add(1, quota_expire_at=NOW - timedelta(days=3))                  # deadline in 4 days
        await self.add(2, cleanup_warn_stage=0)                                      # customer never got the notice
        await self.add(3, quota_expire_at=NOW + timedelta(days=5))                  # not expired
        panel = FakePanel({1001: expired(NOW - timedelta(days=3)), 1002: expired(), 1003: expired(NOW + timedelta(days=5))})
        result = await self.sweep(panel)
        self.assertEqual(result.deleted, 0)
        self.assertEqual(panel.deleted, [])
        for sid in (1, 2, 3):
            self.assertTrue(await self.exists(sid))

    async def test_switching_the_rule_on_today_protects_an_old_backlog(self) -> None:
        await self.add(1, quota_expire_at=NOW - timedelta(days=100))
        panel = FakePanel({1001: expired(NOW - timedelta(days=100))})
        self.assertEqual((await self.sweep(panel, pol=policy(time_enabled_at=NOW - timedelta(days=1)))).deleted, 0)
        self.assertEqual((await self.sweep(panel, pol=policy(time_enabled_at=NOW - timedelta(days=8)))).deleted, 1)

    async def test_a_rule_that_is_off_deletes_nothing(self) -> None:
        await self.add(1)
        panel = FakePanel({1001: expired()})
        self.assertEqual(await self.sweep(panel, pol=CleanupPolicy()), delete.DeleteResult())
        self.assertEqual(panel.reads, [])


class ProtectedTests(DeleteCase):
    async def test_exempt_held_cancelled_review_and_blocked_customers_are_never_deleted(self) -> None:
        await self.add(1, cleanup_exempt=True)
        await self.add(2, cleanup_hold_until=NOW + timedelta(days=2))
        await self.add(3, is_cancelled=True)
        await self.add(4, cancellation_pending=True)
        await self.add(5, shop_needs_review=True)
        async with self.Session() as session:
            session.add(BotUser(id=23, telegram_id=123, referral_code="c23", reseller_id=SHOP, is_blocked=True))
            await session.commit()
        await self.add(6, owner=23)
        panel = FakePanel({1000 + i: expired() for i in range(1, 7)})
        result = await self.sweep(panel)
        self.assertEqual((result.deleted, panel.deleted, panel.reads), (0, [], []))

    async def test_an_open_order_or_automatic_renewal_blocks_the_deletion(self) -> None:
        await self.add(1)
        await self.add(2)
        await self.add(3)
        async with self.Session() as session:
            session.add(Order(user_id=20, service_id=1, amount=100, status="awaiting_receipt", reseller_id=SHOP))
            session.add(Order(user_id=20, service_id=3, amount=100, status="delivered", reseller_id=SHOP))  # closed: does not block
            session.add(ServiceAutomation(service_id=2, shop_id=SHOP, renew_enabled=True, renew_plan_id=None))
            await session.commit()
        panel = FakePanel({1001: expired(), 1002: expired(), 1003: expired()})
        result = await self.sweep(panel)
        self.assertEqual((result.deleted, panel.deleted), (1, [1003]))
        self.assertTrue(await self.exists(1) and await self.exists(2))

    async def test_another_shop_and_the_platform_are_never_touched(self) -> None:
        await self.add(1, owner=21, shop=6)
        await self.add(2, owner=20, shop=None)
        panel = FakePanel({1001: expired(), 1002: expired()})
        result = await self.sweep(panel, shop=SHOP)
        self.assertEqual((result.deleted, panel.deleted, panel.reads), (0, [], []))


class LiveCheckTests(DeleteCase):
    async def test_a_service_renewed_in_the_panel_is_not_deleted(self) -> None:
        await self.add(1)
        panel = FakePanel({1001: {"status": "active", "expire": int((NOW + timedelta(days=30)).timestamp()), "data_limit": 100, "used_traffic": 1}})
        result = await self.sweep(panel)
        self.assertEqual((result.deleted, panel.deleted), (0, []))
        self.assertTrue(await self.exists(1))

    async def test_an_unreadable_panel_deletes_nothing(self) -> None:
        await self.add(1)
        class Down(FakePanel):
            async def get_user_by_id(self, user_id):
                raise TimeoutError()

        panel = Down({1001: expired()})
        self.assertEqual((await self.sweep(panel)).deleted, 0)
        self.assertEqual(panel.deleted, [])
        self.assertTrue(await self.exists(1))


class FailureTests(DeleteCase):
    async def test_a_panel_error_keeps_the_row_and_the_lease_and_retries_after_it_expires(self) -> None:
        await self.add(1)
        panel = FakePanel({1001: expired()}, delete_error=PasarGuardError("bad gateway", 502))
        first = await self.sweep(panel)
        self.assertEqual((first.deleted, first.failed), (0, 1))
        self.assertTrue(await self.exists(1))
        async with self.Session() as session:
            self.assertIsNotNone((await session.get(UserService, 1)).cleanup_claimed_at)
        self.assertEqual(await self.table(CustomerNotice), [])
        again = await self.sweep(panel, at=NOW + timedelta(minutes=30))   # lease still held: not retried
        self.assertEqual((again.deleted, again.failed), (0, 0))
        panel.delete_error = None
        retry = await self.sweep(panel, at=NOW + timedelta(hours=2))      # lease expired: retried and succeeds
        self.assertEqual((retry.deleted, panel.deleted), (1, [1001]))
        self.assertFalse(await self.exists(1))

    async def test_a_panel_user_that_can_only_be_disabled_keeps_the_local_row(self) -> None:
        await self.add(1)
        panel = FakePanel({1001: expired()}, delete_error=PasarGuardError("forbidden", 403))
        result = await self.sweep(panel)
        self.assertEqual((result.deleted, result.failed, panel.disabled), (0, 1, []))
        self.assertTrue(await self.exists(1))

    async def test_a_failure_on_one_service_does_not_stop_the_others(self) -> None:
        await self.add(1)
        await self.add(2)

        class Flaky(FakePanel):
            async def delete_user_by_id(self, user_id):
                if user_id == 1001:
                    raise PasarGuardError("bad gateway", 502)
                await super().delete_user_by_id(user_id)

        panel = Flaky({1001: expired(), 1002: expired()})
        result = await self.sweep(panel)
        self.assertEqual((result.deleted, result.failed, panel.deleted), (1, 1, [1002]))
        self.assertTrue(await self.exists(1))
        self.assertFalse(await self.exists(2))

    async def test_an_already_deleted_panel_user_is_a_clean_success(self) -> None:
        await self.add(1)
        first = FakePanel({1001: expired()})
        # The panel read sees the user, then the delete finds it gone (404) and the lookup confirms absence.
        class Gone(FakePanel):
            async def delete_user_by_id(self, user_id):
                self.users.pop(user_id, None)
                raise PasarGuardError("not found", 404)

        panel = Gone({1001: expired()})
        result = await self.sweep(panel)
        self.assertEqual((result.deleted, result.failed), (1, 0))
        del first


if __name__ == "__main__":
    unittest.main()
