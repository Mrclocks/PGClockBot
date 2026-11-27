"""«کارهای گیرکرده»: only work past its time is counted, a stuck line runs the same scheduler job once on the
owner's request (recorded in the staff report), and nobody else can run a job or name one that is not listed."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest import TestCase
from unittest.mock import MagicMock, patch

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import (
    BotUser,
    CashbackGrant,
    DeliveryFailure,
    Order,
    Payment,
    PaymentSettlement,
    Plan,
    ServiceFreeze,
    ServiceLinkReset,
    ServiceTransfer,
    ShopStaffAction,
    UserService,
)
from app.jobs import scheduler as scheduler_module
from app.services import job_trigger
from app.services.payment_settlement import CHANNEL_PSP, idem_key_psp
from app.services.stuck_work import stuck_counts
from tests.test_update_owner_only import OWNER_SESSION, SHOP_SESSION, RoutesHarness

NOW = datetime.now(timezone.utc)
OLD = NOW - timedelta(hours=2)


class StuckHarness(RoutesHarness):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.runner = MagicMock(return_value=True)
        job_trigger.register_job_runner(self.runner)
        self.addCleanup(job_trigger.register_job_runner, None)

    async def seed_stuck(self) -> None:
        """One stale and one fresh item of every kind."""
        async with self.Session() as session:
            session.add_all([BotUser(id=1, telegram_id=101, referral_code="u1"), Plan(id=1, name="P", price=1000)])
            await session.flush()
            session.add_all([UserService(id=10, bot_user_id=1, plan_id=1, pg_user_id=500, pg_username="svc"),
                             Order(id=20, user_id=1, plan_id=1, amount=1000, status="paid")])
            await session.flush()
            for pid in (30, 31):
                session.add(Payment(id=pid, user_id=1, amount=1000, method="psp", status="approved"))
            await session.flush()
            for pid in (30, 31):
                session.add(PaymentSettlement(payment_id=pid, tenant_key=0, channel=CHANNEL_PSP, provider="sunpay",
                                              status="settling", amount=1000, currency="IRT",
                                              idempotency_key=idem_key_psp(pid), external_ref=f"r{pid}"))
            session.add_all([
                DeliveryFailure(order_id=20, error="send", last_attempt_at=NOW),
                ServiceFreeze(service_id=10, user_id=1, status="frozen", remaining_seconds=60, resume_by=OLD),
                CashbackGrant(shop_id=None, user_id=1, order_id=20, base_amount=100, amount=10, status="pending",
                              available_at=OLD),
                ServiceTransfer(service_id=10, from_user_id=1, token_hash="a" * 64, status="accepted", expires_at=NOW,
                                link_rotation_pending=True, accepted_at=OLD),
                ServiceLinkReset(service_id=10, user_id=1, pg_user_id=500, old_token_hash="b" * 64, status="unknown",
                                 created_at=OLD),
                ServiceLinkReset(service_id=10, user_id=1, pg_user_id=500, old_token_hash="c" * 64, status="done",
                                 created_at=OLD),
            ])
            await session.flush()
            # Only the first settlement was claimed long ago.
            await session.execute(update(PaymentSettlement).where(PaymentSettlement.payment_id == 30).values(updated_at=OLD))
            await session.commit()

    async def call(self, session_payload: dict, method: str, path: str, *, csrf: bool = True):
        cookie = f"session={self.signer.dumps(session_payload)}; csrf=token"
        headers = {"cookie": cookie, "origin": "http://panel.test"}
        if csrf:
            headers["x-csrf-token"] = "token"
        return await self.client.request(method, path, headers=headers)


class StuckCountTests(StuckHarness):
    async def test_only_work_past_its_time_is_counted(self) -> None:
        await self.seed_stuck()
        async with self.Session() as session:
            counts = await stuck_counts(session)
        self.assertEqual(counts, {
            "settlements": 1, "deliveries": 1, "freezes": 1, "cashback": 1, "transfers": 1, "link_resets": 1,
        })

    async def test_nothing_is_stuck_on_a_clean_database(self) -> None:
        async with self.Session() as session:
            self.assertEqual(set((await stuck_counts(session)).values()), {0})


class StuckPageTests(StuckHarness):
    async def test_a_stuck_line_is_a_confirmed_button_and_a_clean_line_is_plain(self) -> None:
        page = (await self.call(OWNER_SESSION, "GET", "/tools/health")).text
        self.assertIn("کارهای گیرکرده", page)
        self.assertNotIn('action="/tools/health/rerun/cashback"', page)
        await self.seed_stuck()
        page = (await self.call(OWNER_SESSION, "GET", "/tools/health")).text
        self.assertIn('action="/tools/health/rerun/cashback"', page)
        self.assertIn('data-confirm-title="اجرای دوباره"', page)
        self.assertIn('href="/finance?tab=delivery" data-health="deliveries"', page)


class RerunTests(StuckHarness):
    async def test_the_owner_runs_the_listed_job_once_and_it_is_recorded(self) -> None:
        response = await self.call(OWNER_SESSION, "POST", "/tools/health/rerun/cashback")
        self.assertEqual(response.status_code, 303)
        self.assertIn("/tools/health?ok=", response.headers["location"])
        self.runner.assert_called_once_with("cashback_mature")
        async with self.Session() as session:
            actions = list(await session.scalars(select(ShopStaffAction)))
        self.assertEqual([(a.action, a.target_type, a.shop_id) for a in actions], [("job_rerun", "job", None)])

    async def test_an_unlisted_job_is_refused(self) -> None:
        for key in ("billing_tick", "..%2Fscheduled_backup", "cashback_mature"):
            with self.subTest(key=key):
                response = await self.call(OWNER_SESSION, "POST", f"/tools/health/rerun/{key}")
                self.assertIn(response.status_code, (303, 404))
        self.runner.assert_not_called()

    async def test_a_stopped_scheduler_is_reported_and_nothing_is_recorded(self) -> None:
        job_trigger.register_job_runner(None)
        response = await self.call(OWNER_SESSION, "POST", "/tools/health/rerun/freezes")
        self.assertIn("/tools/health?err=", response.headers["location"])
        async with self.Session() as session:
            self.assertEqual(list(await session.scalars(select(ShopStaffAction))), [])

    async def test_a_shop_or_a_request_without_the_csrf_token_cannot_run_a_job(self) -> None:
        response = await self.call(SHOP_SESSION, "POST", "/tools/health/rerun/cashback")
        self.assertEqual((response.status_code, response.headers["location"]), (303, "/home"))
        response = await self.call(OWNER_SESSION, "POST", "/tools/health/rerun/cashback", csrf=False)
        self.assertEqual(response.status_code, 403)
        self.runner.assert_not_called()


class SchedulerRunnerTests(TestCase):
    def test_a_registered_job_is_moved_to_now_and_an_unknown_one_is_not(self) -> None:
        job = MagicMock()
        fake = MagicMock()
        fake.get_job.side_effect = lambda job_id: job if job_id == "cashback_mature" else None
        with patch.object(scheduler_module, "scheduler", fake):
            self.assertTrue(scheduler_module._run_job_now("cashback_mature"))
            self.assertFalse(scheduler_module._run_job_now("nope"))
        moved_to = job.modify.call_args.kwargs["next_run_time"]
        self.assertLess(abs((moved_to - datetime.now(timezone.utc)).total_seconds()), 5)
