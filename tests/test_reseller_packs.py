"""Shop owner capacity packs: category fit, one purchase per generation, live-limit raise, tenancy."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.miniapp_reseller import register_miniapp_reseller
from app.db.models import (
    Base,
    BotUser,
    PgAdminSubscription,
    PlanCategory,
    ResellerPlan,
    ResellerProfile,
    WalletTransaction,
)
from app.services.billing import GB
from app.services.miniapp_shop import MiniScope
from app.services.pasarguard import PasarGuardError
from app.services.pg_admin_subscription import list_addon_plans
from app.services.reseller_capacity import EXTRA_FAILED_REFUNDED, EXTRA_OUTCOME_UNKNOWN, STALE_PURCHASE
from app.services.reseller_package import AdminUsage
from app.services.reseller_packs import NO_PACK, PACK_EXPIRED, buy_addon_pack

HOSTILE = "<img src=x onerror=alert(1)>"


def _pack(pack_id: int, *, kind: str, amount: int, price: int, category_id: int | None = None, active: bool = True):
    return ResellerPlan(
        id=pack_id, name=f"pack{pack_id}", plan_kind=kind, price=price, category_id=category_id, is_active=active,
        addon_gb=amount if kind == "addon_volume" else 0, addon_users=amount if kind == "addon_users" else 0,
    )


class PackTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=3, telegram_id=33, referral_code="s3", role="reseller", wallet_balance=1_000_000),
                BotUser(id=4, telegram_id=44, referral_code="s4", role="reseller", wallet_balance=1_000_000),
                BotUser(id=5, telegram_id=55, referral_code="s5", role="user", wallet_balance=1_000_000),
                PlanCategory(id=1, name="vip", audience="resellers"),
                PlanCategory(id=2, name="eco", audience="resellers"),
            ])
            await session.flush()
            session.add_all([
                ResellerPlan(id=1, name="vip plan", plan_kind="subscription", billing_mode="fixed", included_users=10,
                             included_gb=100, price=1, is_active=True, category_id=1, duration_days=30, renew_price=500_000),
                ResellerPlan(id=2, name="plain plan", plan_kind="subscription", billing_mode="fixed", included_users=10,
                             included_gb=100, price=1, is_active=True, category_id=None),
                _pack(10, kind="addon_volume", amount=200, price=250_000),
                _pack(11, kind="addon_volume", amount=200, price=200_000, category_id=1),
                _pack(12, kind="addon_volume", amount=50, price=60_000, category_id=2),
                _pack(13, kind="addon_users", amount=5, price=40_000),
                _pack(14, kind="addon_volume", amount=10, price=1_000, active=False),
                ResellerProfile(id=1, user_id=3, pg_admin_username="shop3", plan_id=1, is_active=True),
                ResellerProfile(id=2, user_id=4, pg_admin_username="shop4", plan_id=2, is_active=True),
                PgAdminSubscription(pg_username="shop3", plan_id=1, access_status="active", base_users=10, base_gb=100),
                PgAdminSubscription(pg_username="shop4", plan_id=2, access_status="active", base_users=10, base_gb=100),
            ])
            await session.commit()
        self.live = {"data_limit": 100 * GB, "max_users": 10}
        self.pg = SimpleNamespace(
            get_admin=AsyncMock(side_effect=lambda _name: dict(self.live)), modify_admin=AsyncMock(return_value={}),
            reset_admin_usage=AsyncMock(return_value={}), reset_user_by_id=AsyncMock(),
            get_users=AsyncMock(return_value={"users": []}), set_disabled_by_id=AsyncMock(),
        )
        self.patches = [
            patch("app.services.pasarguard.get_pg", return_value=self.pg),
            patch("app.services.reseller_capacity.assert_child_growth_fits", AsyncMock()),
            patch("app.services.reseller_packs.assert_child_growth_fits", AsyncMock()),
            patch("app.services.pg_admin_subscription.assert_child_growth_fits", AsyncMock()),
            patch("app.services.billing_suspend._set_admin_enabled", AsyncMock()),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()
        await self.engine.dispose()

    async def buy(self, pack_id: int, *, user_id: int = 3, profile_id: int = 1, generation: int | None = None) -> dict:
        async with self.Session() as session:
            user = await session.get(BotUser, user_id)
            profile = await session.get(ResellerProfile, profile_id)
            plan = await session.get(ResellerPlan, int(profile.plan_id))
            if generation is None:
                sub = await session.scalar(select(PgAdminSubscription).where(
                    PgAdminSubscription.pg_username == profile.pg_admin_username))
                generation = int(sub.renew_generation or 0)
            return await buy_addon_pack(
                session, user=user, profile=profile, plan=plan, pack_id=pack_id, generation=generation,
            )

    async def state(self, uname: str = "shop3", user_id: int = 3) -> tuple[int, int, int, int]:
        async with self.Session() as session:
            wallet = int((await session.get(BotUser, user_id)).wallet_balance)
            sub = await session.scalar(select(PgAdminSubscription).where(PgAdminSubscription.pg_username == uname))
            rows = len(list(await session.scalars(select(WalletTransaction).where(WalletTransaction.user_id == user_id))))
            return wallet, int(sub.extra_gb_purchased or 0), int(sub.renew_generation or 0), rows


class PackCatalogTests(PackTestBase):
    async def names(self, category_id: int | None) -> set[int]:
        async with self.Session() as session:
            return {int(p.id) for p in await list_addon_plans(session, category_id=category_id)}

    async def test_pack_without_category_is_offered_to_every_category(self) -> None:
        self.assertIn(13, await self.names(1))
        self.assertIn(13, await self.names(2))
        self.assertIn(13, await self.names(None))

    async def test_category_pack_hides_the_general_pack_of_the_same_bundle(self) -> None:
        vip = await self.names(1)
        self.assertEqual(vip, {11, 13}, "the vip price replaces the general 200 GB price; eco and inactive are hidden")

    async def test_plan_without_category_sees_general_packs_only(self) -> None:
        self.assertEqual(await self.names(None), {10, 13})

    async def test_other_category_pack_cannot_be_bought_by_id(self) -> None:
        with self.assertRaises(ValueError) as caught:
            await self.buy(12)
        self.assertEqual(str(caught.exception), NO_PACK)
        self.assertEqual(await self.state(), (1_000_000, 0, 0, 0))


class PackPurchaseTests(PackTestBase):
    async def test_purchase_raises_the_live_limit_and_charges_once(self) -> None:
        result = await self.buy(11)
        self.assertEqual(result["gb"], 200)
        self.pg.modify_admin.assert_awaited_once_with("shop3", {"data_limit": 300 * GB})
        self.assertEqual(await self.state(), (800_000, 200, 1, 1))

    async def test_recorded_base_of_zero_does_not_replace_the_panel_limit(self) -> None:
        self.live["data_limit"] = 500 * GB
        async with self.Session() as session:
            sub = await session.scalar(select(PgAdminSubscription).where(PgAdminSubscription.pg_username == "shop3"))
            sub.base_gb = 0
            await session.commit()
        await self.buy(11)
        self.pg.modify_admin.assert_awaited_once_with("shop3", {"data_limit": 700 * GB})

    async def test_repeated_tap_with_the_same_generation_buys_once(self) -> None:
        await self.buy(11, generation=0)
        with self.assertRaises(ValueError) as caught:
            await self.buy(11, generation=0)
        self.assertEqual(str(caught.exception), STALE_PURCHASE)
        self.assertEqual(await self.state(), (800_000, 200, 1, 1))
        self.assertEqual(self.pg.modify_admin.await_count, 1)

    async def test_panel_refusal_refunds_and_forgets_the_pack(self) -> None:
        self.pg.modify_admin = AsyncMock(side_effect=PasarGuardError("bad", 422))
        with self.assertRaises(ValueError) as caught:
            await self.buy(11)
        self.assertEqual(str(caught.exception), EXTRA_FAILED_REFUNDED)
        wallet, extra, _generation, _rows = await self.state()
        self.assertEqual((wallet, extra), (1_000_000, 0))

    async def test_unknown_outcome_keeps_the_purchase(self) -> None:
        self.pg.modify_admin = AsyncMock(side_effect=httpx.ReadTimeout("slow"))
        with self.assertRaises(ValueError) as caught:
            await self.buy(11)
        self.assertEqual(str(caught.exception), EXTRA_OUTCOME_UNKNOWN)
        wallet, extra, _generation, _rows = await self.state()
        self.assertEqual((wallet, extra), (800_000, 200), "no blind refund")

    async def test_users_pack_raises_the_user_cap(self) -> None:
        await self.buy(13)
        payload = self.pg.modify_admin.await_args.args[1]
        self.assertEqual(payload["max_users"], 15)

    async def test_expired_subscription_cannot_buy(self) -> None:
        async with self.Session() as session:
            sub = await session.scalar(select(PgAdminSubscription).where(PgAdminSubscription.pg_username == "shop3"))
            sub.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
            await session.commit()
        with self.assertRaises(ValueError) as caught:
            await self.buy(11)
        self.assertEqual(str(caught.exception), PACK_EXPIRED)
        self.assertEqual((await self.state())[0], 1_000_000)

    async def test_profile_of_another_shop_cannot_be_used(self) -> None:
        async with self.Session() as session:
            user = await session.get(BotUser, 3)
            profile = await session.get(ResellerProfile, 2)
            plan = await session.get(ResellerPlan, 2)
            with self.assertRaises(ValueError):
                await buy_addon_pack(session, user=user, profile=profile, plan=plan, pack_id=10, generation=0)
        self.assertEqual(await self.state("shop4", 4), (1_000_000, 0, 0, 0))
        self.assertEqual((await self.state())[0], 1_000_000)

    async def test_short_wallet_records_nothing(self) -> None:
        async with self.Session() as session:
            (await session.get(BotUser, 3)).wallet_balance = 1_000
            await session.commit()
        with self.assertRaises(ValueError):
            await self.buy(11)
        wallet, extra, generation, _rows = await self.state()
        self.assertEqual((wallet, extra, generation), (1_000, 0, 0))


class MiniResellerApiTests(PackTestBase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.current_id = 3
        self.scope = MiniScope()
        self.session = self.Session()
        app = FastAPI()

        async def db():
            yield self.session

        register_miniapp_reseller(app, get_db=db)

        async def load_user(_session, _request):
            return await self.session.get(BotUser, self.current_id)

        self.patches_api = [
            patch("app.api.miniapp_reseller.load_mini_user", AsyncMock(side_effect=load_user)),
            patch("app.api.miniapp_reseller.mini_scope", side_effect=lambda _request: self.scope),
            patch("app.services.reseller_subscription_overview.read_admin_usage",
                  AsyncMock(return_value=AdminUsage(users=3, used_bytes=40 * GB))),
        ]
        for p in self.patches_api:
            p.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        for p in reversed(self.patches_api):
            p.stop()
        await self.session.close()
        await super().asyncTearDown()

    async def test_overview_lists_the_category_packs_and_the_wallet(self) -> None:
        response = await self.client.get("/api/mini/reseller/subscription")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["wallet"], 1_000_000)
        self.assertEqual({p["id"] for p in data["packs"]}, {11, 13})
        self.assertEqual(data["usage"], {"users": 3, "used_gb": 40})
        self.assertEqual(response.headers["cache-control"], "no-store, private")

    async def test_overview_degrades_when_the_panel_does_not_answer(self) -> None:
        with patch("app.services.reseller_subscription_overview.read_admin_usage", AsyncMock(return_value=None)):
            data = (await self.client.get("/api/mini/reseller/subscription")).json()
        self.assertIsNone(data["usage"])
        self.assertTrue(data["packs"])

    async def test_customer_and_other_roles_are_refused(self) -> None:
        self.current_id = 5
        for method, url, body in (
            ("GET", "/api/mini/reseller/subscription", None),
            ("POST", "/api/mini/reseller/pack", {"pack_id": 10, "generation": 0}),
            ("POST", "/api/mini/reseller/extra", {"kind": "gb", "amount": 1, "generation": 0}),
            ("POST", "/api/mini/reseller/renew", {"generation": 0}),
        ):
            response = await self.client.request(method, url, json=body)
            self.assertEqual(response.status_code, 403, url)
        self.assertEqual((await self.state("shop3", 3))[0], 1_000_000)
        self.pg.modify_admin.assert_not_awaited()

    async def test_shop_mini_app_is_refused(self) -> None:
        self.scope = MiniScope(shop_id=9)
        self.assertEqual((await self.client.get("/api/mini/reseller/subscription")).status_code, 403)

    async def test_inactive_profile_is_refused(self) -> None:
        profile = await self.session.get(ResellerProfile, 1)
        profile.is_active = False
        await self.session.commit()
        self.assertEqual((await self.client.get("/api/mini/reseller/subscription")).status_code, 403)

    async def test_shop_comes_from_the_caller_not_the_body(self) -> None:
        response = await self.client.post(
            "/api/mini/reseller/pack", json={"pack_id": 10, "generation": 0, "shop_id": 4, "pg_username": "shop4"},
        )
        self.assertEqual(response.status_code, 422, "unknown fields are rejected")
        self.assertEqual((await self.state("shop4", 4)), (1_000_000, 0, 0, 0))

    async def test_invalid_numbers_are_rejected(self) -> None:
        for body in (
            {"pack_id": True, "generation": 0}, {"pack_id": 0, "generation": 0}, {"pack_id": 10, "generation": -1},
            {"pack_id": 10, "generation": "0"}, {"pack_id": 2**40, "generation": 0},
        ):
            response = await self.client.post("/api/mini/reseller/pack", json=body)
            self.assertEqual(response.status_code, 422, body)
        for body in ({"kind": "gb", "amount": 0, "generation": 0}, {"kind": "x", "amount": 1, "generation": 0},
                     {"kind": "gb", "amount": 10**9, "generation": 0}, {"kind": "gb", "amount": 1.5, "generation": 0}):
            response = await self.client.post("/api/mini/reseller/extra", json=body)
            self.assertEqual(response.status_code, 422, body)
        self.assertEqual((await self.state())[0], 1_000_000)

    async def test_pack_purchase_charges_once_and_a_repeat_is_stale(self) -> None:
        first = await self.client.post("/api/mini/reseller/pack", json={"pack_id": 11, "generation": 0})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["wallet"], 800_000)
        second = await self.client.post("/api/mini/reseller/pack", json={"pack_id": 11, "generation": 0})
        self.assertEqual(second.status_code, 400)
        self.assertEqual(second.json()["detail"], STALE_PURCHASE)
        self.assertEqual((await self.state())[0], 800_000)

    async def test_hostile_text_is_data_not_markup(self) -> None:
        async with self.Session() as other:
            pack = await other.get(ResellerPlan, 11)
            pack.name = HOSTILE
            await other.commit()
        data = (await self.client.get("/api/mini/reseller/subscription")).json()
        self.assertIn(HOSTILE, [p["name"] for p in data["packs"]], "returned verbatim as JSON; the page escapes it")

    async def test_renewal_through_the_mini_app_refills_the_admin_only(self) -> None:
        response = await self.client.post("/api/mini/reseller/renew", json={"generation": 0})
        self.assertEqual(response.status_code, 200)
        self.assertIn("دست‌نخورده", response.json()["message"])
        self.assertEqual(response.json()["wallet"], 500_000)
        self.pg.reset_admin_usage.assert_awaited_once_with("shop3")
        self.pg.reset_user_by_id.assert_not_awaited()
        repeated = await self.client.post("/api/mini/reseller/renew", json={"generation": 0})
        self.assertEqual(repeated.status_code, 400, "the old screen cannot renew twice")

    async def test_plan_without_time_has_no_renewal_and_says_why(self) -> None:
        async with self.Session() as other:
            (await other.get(ResellerPlan, 1)).duration_days = 0
            await other.commit()
        data = (await self.client.get("/api/mini/reseller/subscription")).json()
        self.assertIsNone(data["renew_total"])
        self.assertIn("زمان ندارد", data["renew_note"])
        refused = await self.client.post("/api/mini/reseller/renew", json={"generation": 0})
        self.assertEqual(refused.status_code, 400)
        self.assertEqual((await self.state())[0], 1_000_000)

    async def test_extra_volume_purchase_uses_the_plan_rate(self) -> None:
        async with self.Session() as other:
            plan = await other.get(ResellerPlan, 1)
            plan.allow_buy_extra = True
            plan.extra_gb_price = 1_000
            await other.commit()
        response = await self.client.post("/api/mini/reseller/extra", json={"kind": "gb", "amount": 5, "generation": 0})
        self.assertEqual(response.status_code, 200)
        self.assertEqual((await self.state())[:2], (995_000, 5))
        refused = await self.client.post("/api/mini/reseller/extra", json={"kind": "gb", "amount": 7, "generation": 1})
        self.assertEqual(refused.status_code, 400, "only the offered amounts are sold")


if __name__ == "__main__":
    unittest.main()
