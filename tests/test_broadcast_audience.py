"""Group-message audiences: each segment picks the right people, filters stay inside one tenant, customers who
turned promotional messages off are respected, input is validated, and the worker sends to the saved audience."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import (
    Base,
    BotUser,
    BroadcastLog,
    MarketingPreference,
    Order,
    Plan,
    ResellerProfile,
    ShopBroadCampaignDay,
    ShopWallet,
    UserService,
)
from app.services.broadcast_audience import (
    AudienceSpec,
    StoredAudienceError,
    audience_clause,
    checked_spec,
    is_broad,
    legacy_spec,
    load_spec,
    make_spec,
    plan_choices,
    spec_from_form,
    stored_label,
)
from app.services.broadcast_queue import process_broadcast_batch, queue_broadcast
from app.services.shop_broadcast import new_shop_request_key, queue_shop_broadcast

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime.now(timezone.utc)
OLD = NOW - timedelta(days=60)
SHOP = 50
OTHER_SHOP = 60
PLATFORM_PLAN = 1
TRIAL_PLAN = 2
SHOP_PLAN = 3
OTHER_SHOP_PLAN = 4


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[int] = []

    async def send_message(self, chat_id, body, parse_mode=None, reply_markup=None):
        self.sent.append(chat_id)


def _user(user_id: int, *, role: str = "user", shop: int | None = None, created: datetime = OLD, **extra) -> BotUser:
    return BotUser(id=user_id, telegram_id=1000 + user_id, referral_code=f"r{user_id}", role=role,
                   reseller_id=shop, created_at=created, **extra)


def _service(user_id: int, *, expires: datetime | None, plan: int = PLATFORM_PLAN, shop: int | None = None,
             status: str | None = "active") -> UserService:
    return UserService(bot_user_id=user_id, plan_id=plan, shop_id=shop, pg_username=f"u{user_id}-{plan}",
                       quota_expire_at=expires, quota_status=status, quota_synced_at=NOW)


def _order(user_id: int, *, amount: int, plan: int = PLATFORM_PLAN, shop: int | None = None, at: datetime = OLD) -> Order:
    return Order(user_id=user_id, plan_id=plan, reseller_id=shop, amount=amount, status="delivered", created_at=at)


class AudienceCase(unittest.IsolatedAsyncioTestCase):
    """Platform: 1 active buyer, 2 expiring, 3 expired, 4 trial only, 5 loyal, 6 new, 7 opted out, 8 reseller,
    9 admin, 10 blocked; the shop owners 50 and 60 are resellers on the platform. Shop 50: 21 active with a shop wallet, 22 never bought. Shop 60: 31 active."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.tmp.name) / 'aud.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.session = self.Session()
        s = self.session
        s.add_all([
            _user(SHOP, role="reseller"), _user(OTHER_SHOP, role="reseller"),
            _user(1, wallet_balance=5000), _user(2), _user(3), _user(4), _user(5), _user(6, created=NOW),
            _user(7), _user(8, role="reseller"), _user(9, role="admin"), _user(10, is_blocked=True),
            _user(21, shop=SHOP, wallet_balance=999_999), _user(22, shop=SHOP), _user(31, shop=OTHER_SHOP),
        ])
        await s.flush()
        s.add_all([
            Plan(id=PLATFORM_PLAN, name="ماهانه", price=100),
            Plan(id=TRIAL_PLAN, name="تست", price=0, is_trial=True),
            Plan(id=SHOP_PLAN, name="پلن فروشگاه", price=100, owner_reseller_id=SHOP),
            Plan(id=OTHER_SHOP_PLAN, name="<b>دیگری</b>", price=100, owner_reseller_id=OTHER_SHOP),
            ResellerProfile(user_id=SHOP, bot_token="enc", is_active=True),
            ResellerProfile(user_id=OTHER_SHOP, bot_token="enc", is_active=True),
        ])
        await s.flush()
        s.add_all([
            _service(1, expires=NOW + timedelta(days=20)), _order(1, amount=100),
            _service(2, expires=NOW + timedelta(days=2)), _order(2, amount=100),
            _service(3, expires=NOW - timedelta(days=3), status="expired"), _order(3, amount=100),
            _order(4, amount=0, plan=TRIAL_PLAN),
            _service(5, expires=NOW + timedelta(days=40)),
            _order(5, amount=100), _order(5, amount=100), _order(5, amount=100, at=NOW - timedelta(days=2)),
            _service(7, expires=NOW + timedelta(days=20)),
            _service(21, expires=NOW + timedelta(days=20), plan=SHOP_PLAN, shop=SHOP),
            _order(21, amount=100, plan=SHOP_PLAN, shop=SHOP),
            ShopWallet(user_id=21, reseller_id=SHOP, balance=5000),
            _service(31, expires=NOW + timedelta(days=20), plan=OTHER_SHOP_PLAN, shop=OTHER_SHOP),
            MarketingPreference(user_id=7, shop_key=0, enabled=False),
        ])
        await s.commit()

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.engine.dispose()
        self.tmp.cleanup()

    async def ids(self, spec: AudienceSpec, shop_id: int | None = None) -> list[int]:
        rows = await self.session.scalars(
            select(BotUser.id).where(*audience_clause(spec, shop_id=shop_id)).order_by(BotUser.id)
        )
        return list(rows)

    async def platform(self, segment: str, **kw) -> list[int]:
        return await self.ids(make_spec(segment, shop_id=None, as_of=NOW, **kw))


class PlatformSegmentTests(AudienceCase):
    async def test_each_segment_picks_its_customers(self) -> None:
        expected = {
            # The shop owners (50, 60) are resellers on the platform bot; 7 turned promotions off.
            "all": [1, 2, 3, 4, 5, 6, 8, 9, SHOP, OTHER_SHOP],
            "users": [1, 2, 3, 4, 5, 6, 8, SHOP, OTHER_SHOP],
            "active": [1, 2, 5],
            "expiring": [2],
            "expired": [3],
            "no_service": [3, 4, 6, 8, SHOP, OTHER_SHOP],
            "buyers": [1, 2, 3, 5],
            "loyal": [5],
            "never_bought": [4, 6, 8, SHOP, OTHER_SHOP],
            "trial": [4],
            "new": [6],
            "resellers": [8, SHOP, OTHER_SHOP],
            "admins": [9],
        }
        for segment, ids in expected.items():
            with self.subTest(segment=segment):
                self.assertEqual(await self.platform(segment), ids)

    async def test_inactive_leaves_out_recent_buyers_and_new_users(self) -> None:
        self.assertEqual(await self.platform("inactive", days=30), [1, 2, 3, 4, 8, SHOP, OTHER_SHOP])

    async def test_day_window_changes_who_is_expiring(self) -> None:
        self.assertEqual(await self.platform("expiring", days=25), [1, 2])

    async def test_opted_out_customer_is_left_out_unless_it_is_a_service_notice(self) -> None:
        self.assertNotIn(7, await self.platform("active"))
        self.assertIn(7, await self.platform("active", notice=True))
        # «همه» still reaches staff; the opted-out customer stays out.
        self.assertNotIn(7, await self.platform("all"))

    async def test_blocked_users_and_shop_customers_never_get_a_platform_message(self) -> None:
        everyone = await self.platform("all", notice=True)
        for user_id in (10, 21, 22, 31):
            self.assertNotIn(user_id, everyone)

    async def test_plan_and_wallet_filters_narrow_the_segment(self) -> None:
        self.assertEqual(await self.platform("buyers", plan_id="1"), [1, 2, 3, 5])
        self.assertEqual(await self.platform("buyers", min_wallet="1000"), [1])

    async def test_staff_segments_ignore_customer_filters(self) -> None:
        spec = make_spec("admins", shop_id=None, plan_id="1", min_wallet="5")
        self.assertIsNone(spec.plan_id)
        self.assertIsNone(spec.min_wallet)


class ShopTenancyTests(AudienceCase):
    async def test_a_shop_segment_holds_only_its_own_customers(self) -> None:
        self.assertEqual(await self.ids(make_spec("all", shop_id=SHOP), SHOP), [21, 22])
        self.assertEqual(await self.ids(make_spec("active", shop_id=SHOP), SHOP), [21])
        self.assertEqual(await self.ids(make_spec("never_bought", shop_id=SHOP), SHOP), [22])
        self.assertEqual(await self.ids(make_spec("active", shop_id=OTHER_SHOP), OTHER_SHOP), [31])

    async def test_a_shop_wallet_filter_reads_the_shop_wallet_not_the_platform_balance(self) -> None:
        self.assertEqual(await self.ids(make_spec("all", shop_id=SHOP, min_wallet="5000"), SHOP), [21])
        # 22 has no shop wallet; 21's platform balance (999 999) never counts in the shop.
        self.assertEqual(await self.ids(make_spec("all", shop_id=SHOP, min_wallet="6000"), SHOP), [])

    async def test_a_plan_of_another_shop_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "متعلق به این فروشگاه نیست"):
            await checked_spec(self.session, make_spec("active", shop_id=SHOP, plan_id=str(OTHER_SHOP_PLAN)), shop_id=SHOP)
        with self.assertRaisesRegex(ValueError, "متعلق به این فروشگاه نیست"):
            await checked_spec(self.session, make_spec("active", shop_id=None, plan_id=str(SHOP_PLAN)), shop_id=None)
        checked = await checked_spec(self.session, make_spec("active", shop_id=SHOP, plan_id=str(SHOP_PLAN)), shop_id=SHOP)
        self.assertIn("پلن فروشگاه", checked.label)

    async def test_plan_choices_list_only_the_tenants_paid_plans(self) -> None:
        self.assertEqual(await plan_choices(self.session, shop_id=SHOP), [(SHOP_PLAN, "پلن فروشگاه")])
        self.assertEqual(await plan_choices(self.session, shop_id=None), [(PLATFORM_PLAN, "ماهانه")])

    async def test_platform_only_segments_and_notices_are_refused_for_a_shop(self) -> None:
        for segment in ("users", "resellers", "admins"):
            with self.subTest(segment=segment), self.assertRaises(ValueError):
                make_spec(segment, shop_id=SHOP)
        with self.assertRaisesRegex(ValueError, "مدیر ربات"):
            make_spec("all", shop_id=SHOP, notice=True)

    async def test_a_forged_notice_in_a_stored_shop_spec_is_ignored(self) -> None:
        stored = json.dumps({"segment": "all", "notice": True, "as_of": NOW.isoformat()})
        self.assertFalse(load_spec(stored, "all", shop_id=SHOP).notice)


class ValidationTests(unittest.TestCase):
    def test_days_and_money_reject_bool_nan_fractions_negatives_and_out_of_range(self) -> None:
        for bad in (True, float("nan"), 1.5, "-3", "0", "366", "abc", "1e3"):
            with self.subTest(days=bad), self.assertRaises(ValueError):
                make_spec("expiring", shop_id=None, days=bad)
        for bad in (True, "-1", "0", "2000000001", "1.5"):
            with self.subTest(wallet=bad), self.assertRaises(ValueError):
                make_spec("all", shop_id=None, min_wallet=bad)

    def test_blank_values_fall_back_to_defaults(self) -> None:
        spec = spec_from_form({"segment": "expiring", "days": "", "plan_id": "", "min_wallet": ""}, shop_id=None)
        self.assertEqual((spec.days, spec.plan_id, spec.min_wallet, spec.notice), (3, None, None, False))
        self.assertEqual(make_spec("expiring", shop_id=None, days=10.0).days, 10)

    def test_an_unknown_segment_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            make_spec("everyone_everywhere", shop_id=None)

    def test_a_saved_spec_round_trips_and_a_row_without_one_keeps_the_legacy_audience(self) -> None:
        spec = make_spec("expired", shop_id=SHOP, days="14", min_wallet="10", as_of=NOW)
        again = load_spec(spec.to_json(), "expired", shop_id=SHOP)
        self.assertEqual((again.segment, again.days, again.min_wallet, again.as_of), ("expired", 14, 10, NOW))
        self.assertEqual(load_spec(None, "admins", shop_id=SHOP).segment, "all")

    def test_an_unreadable_saved_spec_is_refused_rather_than_widened(self) -> None:
        unknown = json.dumps({"segment": "gone", "as_of": NOW.isoformat()})
        for stored in ("{broken", "[]", json.dumps({"segment": "expiring"}), unknown):
            with self.subTest(stored=stored), self.assertRaises(StoredAudienceError):
                load_spec(stored, "users", shop_id=None)
        self.assertEqual(stored_label("{broken", "expiring", shop_id=SHOP), "نزدیک انقضا")

    def test_rows_queued_before_filters_keep_their_old_platform_audience(self) -> None:
        # Old platform rows did not apply the promotional opt-out; they still do not.
        self.assertTrue(legacy_spec("users", shop_id=None).notice)
        self.assertFalse(legacy_spec("all", shop_id=SHOP).notice)

    def test_only_an_unfiltered_all_or_active_message_is_broad(self) -> None:
        self.assertTrue(is_broad(make_spec("all", shop_id=SHOP)))
        self.assertTrue(is_broad(make_spec("active", shop_id=SHOP)))
        self.assertFalse(is_broad(make_spec("active", shop_id=SHOP, min_wallet="1")))
        self.assertFalse(is_broad(make_spec("expiring", shop_id=SHOP)))

    def test_history_label_of_a_row_without_a_spec_is_the_segment_name(self) -> None:
        self.assertEqual(stored_label(None, "users", shop_id=None), "مشتریان")


class QueueAndWorkerTests(AudienceCase):
    async def send_all(self, log_id: int) -> FakeBot:
        bot = FakeBot()
        for _ in range(20):
            async with self.Session() as session:
                if not await process_broadcast_batch(session, log_id, bot, delay=0):
                    break
        return bot

    async def test_a_platform_message_goes_only_to_its_segment(self) -> None:
        spec = make_spec("expiring", shop_id=None, days="3")
        log = await queue_broadcast(self.session, text="تمدید کنید", audience=spec, created_by="owner")
        self.assertEqual((log.total, log.audience), (1, "expiring"))
        self.assertIn("نزدیک انقضا", json.loads(log.audience_spec)["label"])
        self.assertEqual((await self.send_all(log.id)).sent, [1002])

    async def test_time_windows_are_measured_from_the_saved_time(self) -> None:
        later = make_spec("expiring", shop_id=None, days="3", as_of=NOW + timedelta(days=18))
        self.assertEqual(await self.ids(later), [1])
        stored = load_spec(later.to_json(), "expiring", shop_id=None)
        self.assertEqual(await self.ids(stored), [1])

    async def test_a_narrow_shop_segment_does_not_use_the_daily_broad_slot(self) -> None:
        narrow = await queue_shop_broadcast(self.session, shop_id=SHOP, text="x", created_by="s",
                                            request_key=new_shop_request_key(), audience=make_spec("never_bought", shop_id=SHOP))
        self.assertEqual(narrow.total, 1)
        broad = await queue_shop_broadcast(self.session, shop_id=SHOP, text="y", created_by="s",
                                           request_key=new_shop_request_key(), audience=make_spec("all", shop_id=SHOP))
        self.assertEqual(broad.total, 2)
        with self.assertRaisesRegex(ValueError, "روزی یک‌بار"):
            await queue_shop_broadcast(self.session, shop_id=SHOP, text="z", created_by="s",
                                       request_key=new_shop_request_key(), audience=make_spec("active", shop_id=SHOP))
        await self.session.rollback()
        days = await self.session.scalars(select(ShopBroadCampaignDay.used))
        self.assertEqual(list(days), [1])

    async def test_a_shop_message_never_reaches_another_shop_or_the_platform(self) -> None:
        log = await queue_shop_broadcast(self.session, shop_id=SHOP, text="x", created_by="s",
                                         request_key=new_shop_request_key(), audience=make_spec("all", shop_id=SHOP))
        bot = FakeBot()
        for _ in range(5):
            async with self.Session() as session:
                if not await process_broadcast_batch(session, log.id, bot, delay=0):
                    break
        self.assertEqual(sorted(bot.sent), [1021, 1022])

    async def test_a_row_whose_saved_audience_is_unreadable_fails_without_sending(self) -> None:
        stored = json.dumps({"segment": "removed_segment", "as_of": NOW.isoformat()})
        self.session.add(BroadcastLog(id=98, audience="all", audience_spec=stored, text="hi", total=5, status="queued"))
        await self.session.commit()
        self.assertEqual((await self.send_all(98)).sent, [])
        async with self.Session() as session:
            self.assertEqual(await session.scalar(select(BroadcastLog.status).where(BroadcastLog.id == 98)), "failed")

    async def test_an_old_row_without_a_spec_still_sends_to_its_audience(self) -> None:
        self.session.add(BroadcastLog(id=99, audience="admins", text="hi", total=1, status="queued"))
        await self.session.commit()
        self.assertEqual((await self.send_all(99)).sent, [1009])


class MigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade_keep_broadcasts(self) -> None:
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        spec = importlib.util.spec_from_file_location("m0117", ROOT / "alembic/versions/0117_broadcast_audience_spec.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE broadcast_logs (id INTEGER PRIMARY KEY, audience TEXT, text TEXT)"))
            conn.execute(sa.text("INSERT INTO broadcast_logs VALUES (1, 'users', 'hi')"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                self.assertIn("audience_spec", {c["name"] for c in sa.inspect(conn).get_columns("broadcast_logs")})
                migration.downgrade()
                migration.downgrade()
                self.assertNotIn("audience_spec", {c["name"] for c in sa.inspect(conn).get_columns("broadcast_logs")})
                self.assertEqual(conn.execute(sa.text("SELECT audience, text FROM broadcast_logs")).one(), ("users", "hi"))
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
