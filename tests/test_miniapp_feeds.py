"""Mini App screen feeds: launch cost, customer and shop isolation, bounds and roles."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.miniapp_feeds import register_miniapp_feeds
from app.api.miniapp_pages import _serialize_service, register_miniapp_pages
from app.db.models import Base, BotUser, Plan, ResellerProfile, UserService, WalletTransaction
from app.services.miniapp_home import customer_home_summary
from app.services.users import reset_shop_reseller_id, set_shop_reseller_id

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
LIVE_INFO = {"status": "active", "used_traffic": 1, "data_limit": 10, "expire": 0}


class MiniAppFeedTestCase(unittest.IsolatedAsyncioTestCase):
    """In-memory database, the real routes, and a counting fake for panel reads."""

    caller_id = 1

    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add_all([
            BotUser(id=1, telegram_id=101, referral_code="buyer", role="user", wallet_balance=5000, full_name="buyer"),
            BotUser(id=2, telegram_id=102, referral_code="other", role="user", wallet_balance=9000),
            BotUser(id=3, telegram_id=103, referral_code="shop", role="reseller", wallet_balance=0),
            BotUser(id=4, telegram_id=104, referral_code="admin", role="admin"),
        ])
        await self.session.commit()
        self.shop_token = set_shop_reseller_id(None)
        self.panel_reads: list[int] = []

        async def fake_info(token, *, subscription_url=None, service_id=None, expected_pg_user_id=None):
            self.panel_reads.append(service_id)
            return dict(LIVE_INFO)

        async def load_user(*_args):
            return await self.session.scalar(select(BotUser).where(BotUser.id == self.caller_id))

        self.patches = [
            patch("app.api.miniapp_pages.load_mini_user", AsyncMock(side_effect=load_user)),
            patch("app.api.miniapp_feeds.load_mini_user", AsyncMock(side_effect=load_user)),
            patch("app.api.miniapp_pages._fetch_pg_info", AsyncMock(side_effect=fake_info)),
            patch("app.api.miniapp_pages.get_all_settings", AsyncMock(return_value={"pay_wallet_enabled": "1"})),
            patch("app.api.miniapp_feeds.get_all_settings", AsyncMock(return_value={"pay_wallet_enabled": "1"})),
            patch("app.services.miniapp_auth.is_bot_platform_admin", side_effect=lambda u: u.role == "admin"),
        ]
        for p in self.patches:
            p.start()
        app = FastAPI()

        async def get_db():
            yield self.session

        register_miniapp_pages(app, render=lambda *args: "", get_db=get_db)
        register_miniapp_feeds(app, get_db=get_db)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        for p in reversed(self.patches):
            p.stop()
        reset_shop_reseller_id(self.shop_token)
        await self.session.close()
        await self.engine.dispose()

    def as_user(self, user_id: int) -> None:
        self.caller_id = user_id

    async def add_services(self, *services: UserService) -> None:
        self.session.add_all(list(services))
        await self.session.commit()


def svc(service_id: int, owner: int = 1, **extra) -> UserService:
    return UserService(
        id=service_id, bot_user_id=owner, pg_user_id=1000 + service_id,
        pg_username=extra.pop("name", f"svc-{service_id}"),
        subscription_url=f"https://sub.example.com/sub/token{service_id}",
        subscription_token=f"token{service_id}", **extra,
    )


class LaunchPayloadTests(MiniAppFeedTestCase):
    async def test_launch_never_reads_the_panel(self) -> None:
        await self.add_services(*(svc(i) for i in range(1, 16)))
        response = await self.client.get("/api/mini/me")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.panel_reads, [])
        customer = response.json()["customer"]
        self.assertEqual(customer["wallet"], 5000)
        self.assertEqual(customer["home"]["service_count"], 15)
        self.assertNotIn("services", customer)
        self.assertNotIn("token", response.text)

    async def test_admin_launch_has_no_commerce_payload(self) -> None:
        await self.add_services(svc(1))
        self.as_user(4)
        body = (await self.client.get("/api/mini/me")).json()
        self.assertFalse(body["commerce_allowed"])
        self.assertEqual(body["customer"]["home"]["service_count"], 0)
        self.assertEqual([n["id"] for n in body["nav"]], ["home", "ops"])

    async def test_reseller_nav_has_shop_and_account(self) -> None:
        self.as_user(3)
        body = (await self.client.get("/api/mini/me")).json()
        self.assertEqual([n["id"] for n in body["nav"]], ["home", "services", "shop", "ops", "account"])


class ServicesFeedTests(MiniAppFeedTestCase):
    async def test_lists_only_the_callers_platform_services(self) -> None:
        await self.add_services(
            svc(1), svc(2, owner=2, name="private-other"), svc(3, shop_id=3, name="sold-by-shop"),
            svc(4, shop_needs_review=True, name="owner-unknown"), svc(5),
        )
        response = await self.client.get("/api/mini/services")
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertEqual([row["id"] for row in response.json()["items"]], [5, 1])
        for leak in ("private-other", "sold-by-shop", "owner-unknown", "subscription_token"):
            self.assertNotIn(leak, response.text)

    async def test_second_customer_cannot_page_into_first_customers_services(self) -> None:
        await self.add_services(svc(1), svc(2))
        self.as_user(2)
        response = await self.client.get("/api/mini/services?before_id=99")
        self.assertEqual(response.json(), {"items": [], "next_before_id": None})
        self.assertEqual(self.panel_reads, [])

    async def test_pages_newest_first_and_reads_the_panel_for_the_page_only(self) -> None:
        await self.add_services(*(svc(i) for i in range(1, 8)))
        first = (await self.client.get("/api/mini/services?limit=3")).json()
        self.assertEqual([r["id"] for r in first["items"]], [7, 6, 5])
        self.assertEqual(first["next_before_id"], 5)
        self.assertEqual(sorted(self.panel_reads), [5, 6, 7])
        last = (await self.client.get("/api/mini/services?limit=3&before_id=2")).json()
        self.assertEqual([r["id"] for r in last["items"]], [1])
        self.assertIsNone(last["next_before_id"])

    async def test_search_narrows_by_name(self) -> None:
        await self.add_services(svc(1, name="home-net"), svc(2, name="WORK-net"), svc(3, name="phone"))
        body = (await self.client.get("/api/mini/services?q=work")).json()
        self.assertEqual([r["id"] for r in body["items"]], [2])

    async def test_rejects_out_of_range_input(self) -> None:
        for query in ("limit=0", "limit=11", "limit=abc", "before_id=0", "before_id=-5",
                      "before_id=99999999999", "q=" + "x" * 65):
            with self.subTest(query=query):
                response = await self.client.get("/api/mini/services?" + query)
                self.assertEqual(response.status_code, 422)
        self.assertEqual(self.panel_reads, [])

    async def test_admin_cannot_list_services(self) -> None:
        self.as_user(4)
        self.assertEqual((await self.client.get("/api/mini/services")).status_code, 403)

    async def test_marks_add_on_eligibility(self) -> None:
        await self.add_services(svc(1), svc(2, remark="linked"))
        rows = {r["id"]: r["addons_allowed"] for r in (await self.client.get("/api/mini/services")).json()["items"]}
        self.assertEqual(rows, {1: True, 2: False})


class PlansAndActivityFeedTests(MiniAppFeedTestCase):
    async def test_catalog_is_active_platform_plans_with_trial_first(self) -> None:
        self.session.add_all([
            Plan(id=1, name="monthly", price=100, sort_order=2),
            Plan(id=2, name="weekly", price=50, sort_order=1),
            Plan(id=3, name="trial", price=0, is_trial=True),
            Plan(id=4, name="shop-plan", price=10, owner_reseller_id=3),
            Plan(id=5, name="retired", price=10, is_active=False),
        ])
        await self.session.commit()
        body = (await self.client.get("/api/mini/plans")).json()
        self.assertTrue(body["wallet_pay_enabled"])
        self.assertEqual([p["id"] for p in body["plans"]], [3, 2, 1])
        self.as_user(4)
        self.assertEqual((await self.client.get("/api/mini/plans")).status_code, 403)

    async def test_activity_is_the_callers_platform_ledger_only(self) -> None:
        self.session.add_all([
            WalletTransaction(user_id=1, amount=-100, balance_after=900, reason="mine"),
            WalletTransaction(user_id=2, amount=500, balance_after=500, reason="other customer"),
            WalletTransaction(user_id=1, amount=700, balance_after=700, reason="shop purse", reseller_id=3),
        ])
        await self.session.commit()
        response = await self.client.get("/api/mini/activity")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["reason"] for row in response.json()["items"]], ["mine"])
        for query in ("limit=0", "limit=51"):
            self.assertEqual((await self.client.get("/api/mini/activity?" + query)).status_code, 422)


class OpsFeedTests(MiniAppFeedTestCase):
    async def test_customer_is_denied(self) -> None:
        self.assertEqual((await self.client.get("/api/mini/ops")).status_code, 403)

    async def test_reseller_gets_own_shop_and_admin_gets_platform(self) -> None:
        self.session.add(ResellerProfile(user_id=3, bot_username="myshop_bot"))
        await self.session.commit()
        self.as_user(3)
        with patch("app.api.home_pages._reseller_shop_stats", AsyncMock(return_value={"users": 7})) as stats:
            body = (await self.client.get("/api/mini/ops")).json()
        stats.assert_awaited_once()
        self.assertEqual(stats.await_args.args[1], 3)
        self.assertEqual(body["shop"]["bot_username"], "myshop_bot")
        self.as_user(4)
        with patch("app.services.home_overview.bot_panel_summary", AsyncMock(return_value={"users": 2})):
            body = (await self.client.get("/api/mini/ops")).json()
        self.assertEqual(body["stats"]["users"], 2)
        self.assertNotIn("shop", body)


class HomeSummaryTests(MiniAppFeedTestCase):
    async def test_attention_lists_ending_services_soonest_first(self) -> None:
        synced = {"quota_synced_at": NOW}
        await self.add_services(
            svc(1, quota_expire_at=NOW + timedelta(days=2), **synced),
            svc(2, quota_expire_at=NOW + timedelta(hours=5), **synced),
            svc(3, quota_expire_at=NOW + timedelta(days=20), **synced),
            svc(4, quota_status="limited", quota_expire_at=NOW + timedelta(days=20), **synced),
            svc(5, quota_expire_at=NOW + timedelta(days=1), is_cancelled=True, **synced),
            svc(6, quota_expire_at=NOW + timedelta(days=1)),
            svc(7, owner=2, quota_expire_at=NOW + timedelta(days=1), **synced),
            svc(8, shop_id=3, quota_expire_at=NOW + timedelta(days=1), **synced),
        )
        summary = await customer_home_summary(self.session, 1, now=NOW)
        self.assertEqual(summary.service_count, 6)
        self.assertEqual([g.id for g in summary.attention], [2, 1, 4])
        self.assertEqual(summary.attention[0].days_left, 1)
        self.assertEqual([g.id for g in summary.recent], [6, 5, 4])

    async def test_unsynced_and_on_hold_services_are_never_unlimited(self) -> None:
        await self.add_services(svc(1), svc(2, quota_status="on_hold"))
        rows = {g.id: g for g in (await customer_home_summary(self.session, 1, now=NOW)).recent}
        self.assertEqual(rows[1].remaining_label, "—")
        self.assertEqual(rows[2].remaining_label, "پس از اتصال")
        self.assertEqual(rows[1].status_fa, "نامشخص")

    async def test_customer_without_services(self) -> None:
        summary = await customer_home_summary(self.session, 1, now=NOW)
        self.assertEqual(summary.payload(), {"service_count": 0, "attention": [], "recent": []})



class ServiceTimesTests(unittest.TestCase):
    def test_last_connection_is_a_raw_time_never_labelled_expired(self) -> None:
        now = datetime.now(timezone.utc)
        info = {"status": "active", "used_traffic": 1, "data_limit": 10,
                "expire": int((now + timedelta(days=3)).timestamp()),
                "online_at": (now - timedelta(minutes=5)).isoformat()}
        row = _serialize_service(UserService(id=1, bot_user_id=1, pg_username="a"), info)
        online = datetime.fromisoformat(row["online_at"])
        self.assertLess(abs((online - (now - timedelta(minutes=5))).total_seconds()), 2)
        self.assertNotIn("منقضی", row["online_at"])
        self.assertLess(abs((datetime.fromisoformat(row["expire_at"]) - (now + timedelta(days=3))).total_seconds()), 2)

    def test_unknown_times_stay_empty(self) -> None:
        row = _serialize_service(UserService(id=1, bot_user_id=1, pg_username="a"), {"status": "on_hold"})
        self.assertIsNone(row["expire_at"])
        self.assertIsNone(row["online_at"])


if __name__ == "__main__":
    unittest.main()
