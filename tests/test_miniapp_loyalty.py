"""Mini App club: what the customer sees, spending points once, and shop/customer isolation."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import miniapp_loyalty
from app.api.miniapp_loyalty import register_miniapp_loyalty
from app.db.models import (
    Base,
    BotUser,
    LoyaltyReward,
    PointsTransaction,
    RewardRedemption,
    UserService,
    WalletTransaction,
)
from app.services.customer_loyalty import HISTORY_PAGE_SIZE, UNKNOWN_OUTCOME_MESSAGE
from app.services.users import reset_shop_reseller_id, set_shop_reseller_id


def reward(reward_id: int, reward_type: str, value: int, cost: int, **extra) -> LoyaltyReward:
    fields = {"enabled": True, "archived": False, "redemption_count": 0, **extra}
    return LoyaltyReward(
        id=reward_id, name=f"reward {reward_id}", reward_type=reward_type, reward_value=value, points_cost=cost, **fields,
    )


def points_entry(entry_id: int, user_id: int, amount: int, *, shop_id: int | None = None) -> PointsTransaction:
    return PointsTransaction(
        id=entry_id, user_id=user_id, amount=amount, balance_after=0, tx_type="earn", source="rule:test",
        description=f"entry {entry_id}", idempotency_key=f"seed:{entry_id}", reseller_id=shop_id,
    )


class MiniAppClubTestCase(unittest.IsolatedAsyncioTestCase):
    caller_id = 1

    async def asyncSetUp(self) -> None:
        # A file database so concurrent requests each get their own session and connection.
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.tmp.name) / 'club.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.session = self.Session()
        self.session.add_all([
            BotUser(id=1, telegram_id=101, referral_code="REF1", role="user", points_balance=500, wallet_balance=0),
            BotUser(id=2, telegram_id=102, referral_code="REF2", role="user", points_balance=500),
            BotUser(id=3, telegram_id=103, referral_code="SHOP", role="reseller"),
            BotUser(id=4, telegram_id=104, referral_code="ADM", role="admin"),
            BotUser(id=5, telegram_id=105, referral_code="F1", role="user", referred_by_id=1),
            reward(1, "wallet_credit", 20_000, 100),
            reward(2, "discount_percent", 15, 50),
            reward(3, "traffic_gb", 5, 200),
            reward(4, "wallet_credit", 99_000, 100, reseller_id=3),
            reward(5, "wallet_credit", 1_000, 10, enabled=False),
            reward(6, "wallet_credit", 1_000, 900),
            UserService(id=1, bot_user_id=1, pg_user_id=76, pg_username="first"),
            UserService(id=10, bot_user_id=1, pg_user_id=77, pg_username="mine"),
            UserService(id=11, bot_user_id=2, pg_user_id=78, pg_username="theirs"),
            UserService(id=12, bot_user_id=1, pg_user_id=79, pg_username="shop_sold", shop_id=3),
            points_entry(1, 1, 30),
            points_entry(2, 1, 40, shop_id=3),
            points_entry(3, 2, 50),
        ])
        await self.session.commit()
        self.shop_token = set_shop_reseller_id(None)
        miniapp_loyalty._bot_username_cache.clear()
        self.apply_service_reward = AsyncMock()

        async def load_user(session, _request):
            return await session.scalar(select(BotUser).where(BotUser.id == self.caller_id))

        self.bot = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(username="ClockBot")),
                                   session=SimpleNamespace(close=AsyncMock()))
        self.patches = [
            patch("app.api.miniapp_loyalty.load_mini_user", AsyncMock(side_effect=load_user)),
            patch("app.api.miniapp_pages.assert_mini_force_join", AsyncMock()),
            patch("app.services.miniapp_auth.is_bot_platform_admin", side_effect=lambda u: u.role == "admin"),
            patch("app.api.miniapp_loyalty.get_settings", return_value=SimpleNamespace(bot_username="")),
            patch("app.api.miniapp_loyalty.open_notify_bot_for_user", AsyncMock(return_value=(self.bot, True))),
            # The panel is not under test: the grant is recorded by the mock.
            patch("app.services.loyalty.apply_service_reward", self.apply_service_reward),
        ]
        for p in self.patches:
            p.start()
        app = FastAPI()

        async def get_db():
            async with self.Session() as session:
                yield session

        register_miniapp_loyalty(app, get_db=get_db)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        for p in reversed(self.patches):
            p.stop()
        reset_shop_reseller_id(self.shop_token)
        await self.session.close()
        await self.engine.dispose()
        self.tmp.cleanup()

    async def club(self) -> dict:
        response = await self.client.get("/api/mini/club")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["club"]

    async def redeem(self, reward_id: int, **body):
        return await self.client.post(f"/api/mini/club/rewards/{reward_id}/redeem", json=body)

    async def user(self, user_id: int = 1) -> BotUser:
        self.session.expire_all()
        return await self.session.get(BotUser, user_id)

    async def redemptions(self) -> int:
        return int(await self.session.scalar(select(func.count()).select_from(RewardRedemption)))


class ClubSummaryTests(MiniAppClubTestCase):
    async def test_shows_the_callers_platform_club_only(self) -> None:
        club = await self.club()
        self.assertTrue(club["enabled"])
        self.assertEqual(club["points"], 500)
        self.assertEqual(club["referral"]["code"], "REF1")
        self.assertEqual(club["referral"]["link"], "https://t.me/ClockBot?start=ref_REF1")
        self.assertEqual(club["referral"]["invited"], 1)
        self.assertEqual([r["id"] for r in club["rewards"]], [1, 2, 3, 6], "shop and disabled rewards stay hidden")
        self.assertEqual({r["id"]: r["affordable"] for r in club["rewards"]}, {1: True, 2: True, 3: True, 6: False})
        self.assertEqual(club["services"], [{"id": 10, "name": "mine"}, {"id": 1, "name": "first"}], "only the caller's platform services")
        self.assertEqual([h["id"] for h in club["history"]], [1], "only the caller's platform points entries")

    async def test_bot_username_is_asked_from_telegram_once(self) -> None:
        await self.club()
        await self.club()
        self.bot.get_me.assert_awaited_once()
        self.bot.session.close.assert_awaited()

    async def test_disabled_club_keeps_invites_and_refuses_rewards(self) -> None:
        with patch("app.services.customer_loyalty.loyalty_enabled", AsyncMock(return_value=False)):
            club = await self.club()
            self.assertEqual(set(club), {"enabled", "referral"})
            self.assertEqual((await self.redeem(1)).status_code, 400)
        self.assertEqual((await self.user()).points_balance, 500)

    async def test_history_pages_by_id_within_the_callers_entries(self) -> None:
        self.session.add_all(points_entry(100 + i, 1, i + 1) for i in range(HISTORY_PAGE_SIZE + 1))
        await self.session.commit()
        first = (await self.client.get("/api/mini/club/history")).json()
        self.assertEqual(len(first["items"]), HISTORY_PAGE_SIZE)
        self.assertTrue(first["has_more"])
        rest = (await self.client.get(f"/api/mini/club/history?before={first['items'][-1]['id']}")).json()
        self.assertEqual([i["id"] for i in rest["items"]], [100, 1])
        self.assertFalse(rest["has_more"])
        self.assertEqual((await self.client.get("/api/mini/club/history?before=-1")).status_code, 400)

    async def test_admin_is_denied(self) -> None:
        self.caller_id = 4
        self.assertEqual((await self.client.get("/api/mini/club")).status_code, 403)
        self.assertEqual((await self.redeem(1)).status_code, 403)


class ClubRedeemTests(MiniAppClubTestCase):
    async def test_wallet_reward_spends_points_and_credits_once(self) -> None:
        response = await self.redeem(1)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["points"], 400)
        user = await self.user()
        self.assertEqual((user.points_balance, user.wallet_balance), (400, 20_000))
        credits = await self.session.scalar(select(func.count()).select_from(WalletTransaction).where(WalletTransaction.user_id == 1))
        self.assertEqual(credits, 1)

    async def test_concurrent_taps_spend_once(self) -> None:
        results = await asyncio.gather(*(self.redeem(1) for _ in range(3)))
        self.assertTrue(all(r.status_code in (200, 400) for r in results), [r.text for r in results])
        self.assertEqual(await self.redemptions(), 1)
        user = await self.user()
        self.assertEqual((user.points_balance, user.wallet_balance), (400, 20_000))

    async def test_discount_reward_returns_a_code_the_club_then_lists(self) -> None:
        response = await self.redeem(2)
        self.assertEqual(response.status_code, 200, response.text)
        code = response.json()["discount_code"]
        self.assertTrue(code)
        self.assertEqual([d["code"] for d in (await self.club())["discounts"]], [code])
        self.caller_id = 2
        self.assertEqual((await self.club())["discounts"], [], "another customer never sees the code")

    async def test_unreachable_rewards_spend_nothing(self) -> None:
        for reward_id in (4, 5, 6, 999):
            with self.subTest(reward_id=reward_id):
                self.assertEqual((await self.redeem(reward_id)).status_code, 400)
        self.assertEqual((await self.user()).points_balance, 500)
        self.assertEqual(await self.redemptions(), 0)

    async def test_service_reward_goes_to_the_callers_platform_service_only(self) -> None:
        self.assertEqual((await self.redeem(3)).status_code, 400, "a service must be chosen")
        for service_id in (11, 12, 999):
            with self.subTest(service_id=service_id):
                self.assertEqual((await self.redeem(3, service_id=service_id)).status_code, 400)
        self.apply_service_reward.assert_not_awaited()
        self.assertEqual((await self.redeem(3, service_id=10)).status_code, 200)
        self.apply_service_reward.assert_awaited_once()
        self.assertEqual(self.apply_service_reward.await_args.args[2].id, 10)
        self.assertEqual((await self.user()).points_balance, 300)

    async def test_panel_timeout_reports_unknown_outcome_and_keeps_points(self) -> None:
        self.apply_service_reward.side_effect = httpx.ReadTimeout("slow panel")
        response = await self.redeem(3, service_id=10)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], UNKNOWN_OUTCOME_MESSAGE)
        self.assertNotIn("slow panel", response.text)
        self.assertEqual((await self.user()).points_balance, 500)
        self.assertEqual(await self.redemptions(), 0)

    async def test_failed_grant_without_the_panel_spends_nothing(self) -> None:
        with patch("app.services.wallet.credit_wallet", AsyncMock(side_effect=RuntimeError("db detail"))):
            response = await self.redeem(1)
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("db detail", response.text)
        self.assertNotEqual(response.json()["detail"], UNKNOWN_OUTCOME_MESSAGE, "no panel call, so the outcome is known")
        user = await self.user()
        self.assertEqual((user.points_balance, user.wallet_balance), (500, 0))

    async def test_bad_ids_are_rejected(self) -> None:
        for body in ({"service_id": True}, {"service_id": "10"}, {"service_id": -1}, {"service_id": 2**31}):
            with self.subTest(body=body):
                self.assertEqual((await self.redeem(3, **body)).status_code, 400)
        self.assertEqual((await self.redeem(0)).status_code, 400)
        self.assertEqual((await self.client.post("/api/mini/club/rewards/1/redeem", content=b"[]")).status_code, 400)
        self.assertEqual((await self.user()).points_balance, 500)


if __name__ == "__main__":
    unittest.main()
