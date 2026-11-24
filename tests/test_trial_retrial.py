"""«دور جدید تست»: only this shop's trial takers who never bought, are not blocked and whose trial has ended get
their trial back; their orders and services stay, the action needs its own permission, and it runs once."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI
from sqlalchemy import func, select

from app.api.trial_users_pages import register_trial_users_pages
from app.db.models import Order, ShopStaffAction, TrialClaim, UserService
from app.services.tools_nav import tool_sections
from app.services.trial_user_actions import reset_trials, trial_shop_key
from tests.trial_users_support import NOW, OWNER, TrialUsersTestCase, shop_staff

ENDED = NOW - timedelta(days=3)
# The trial state is read against the real clock, so a running trial must end after today.
STILL_ON = datetime.now(timezone.utc) + timedelta(days=30)


class RetrialCase(TrialUsersTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        async with self.Session() as session:
            self.ids = {}
            for name, tg in (("ended", 1), ("active", 2), ("buyer", 3), ("blocked", 4), ("other_shop", 5), ("no_service", 6)):
                self.ids[name] = (await self._user(session, 600 + tg)).id
            shop = self.shop_a.id
            for name in ("ended", "active", "buyer", "blocked"):
                state = "active" if name == "active" else "expired"
                service = UserService(bot_user_id=self.ids[name], plan_id=self.trial_plan.id, pg_user_id=900 + len(name),
                                      pg_username=name, quota_status=state, quota_synced_at=NOW,
                                      quota_expire_at=STILL_ON if state == "active" else ENDED)
                session.add(service)
                await session.flush()
                await self._order(session, await session.get(type(self.shop_a), self.ids[name]), self.trial_plan,
                                  shop=shop, service_id=service.id)
            await self._order(session, await session.get(type(self.shop_a), self.ids["no_service"]), self.trial_plan, shop=shop)
            await self._order(session, await session.get(type(self.shop_a), self.ids["other_shop"]), self.trial_plan,
                              shop=self.shop_b.id)
            await self._buy(session, await session.get(type(self.shop_a), self.ids["buyer"]), shop=shop)
            (await session.get(type(self.shop_a), self.ids["blocked"])).is_blocked = True
            for name, uid in self.ids.items():
                key = trial_shop_key(self.shop_b.id if name == "other_shop" else shop)
                session.add(TrialClaim(user_id=uid, shop_key=key))
            # The same customer also took shop B's trial: shop A's round must leave that one alone.
            session.add(TrialClaim(user_id=self.ids["ended"], shop_key=trial_shop_key(self.shop_b.id)))
            await session.commit()

    async def claims(self) -> dict[int, str]:
        """Each customer's claim in shop A, or else in another shop."""
        async with self.Session() as session:
            rows = list(await session.scalars(select(TrialClaim).order_by(TrialClaim.shop_key != str(self.shop_a.id))))
        found: dict[int, str] = {}
        for claim in rows:
            found.setdefault(int(claim.user_id), claim.shop_key)
        return found

    async def claims_in(self, shop_id: int) -> set[int]:
        async with self.Session() as session:
            rows = await session.scalars(select(TrialClaim.user_id).where(TrialClaim.shop_key == trial_shop_key(shop_id)))
            return {int(uid) for uid in rows}

    async def reset(self, staff: dict, names: list[str]):
        async with self.Session() as session:
            return await reset_trials(session, staff=staff, shop_id=self.shop_a.id, user_ids=[self.ids[n] for n in names])


class RetrialServiceTests(RetrialCase):
    async def test_only_ended_non_buyers_who_are_not_blocked_get_their_trial_back(self) -> None:
        result = await self.reset(shop_staff(self.shop_a.id, ["trial_users", "trial_reset"]), list(self.ids))
        self.assertEqual((result.reset, result.refused), (2, 4))
        in_a = await self.claims_in(self.shop_a.id)
        self.assertEqual(in_a, {self.ids[n] for n in ("active", "buyer", "blocked")})
        self.assertEqual(await self.claims_in(self.shop_b.id), {self.ids["other_shop"], self.ids["ended"]})

    async def test_orders_and_services_stay_and_a_second_run_changes_nothing(self) -> None:
        async with self.Session() as session:
            before = (await session.scalar(select(func.count(Order.id))), await session.scalar(select(func.count(UserService.id))))
        staff = shop_staff(self.shop_a.id, ["trial_users", "trial_reset"])
        await self.reset(staff, ["ended"])
        again = await self.reset(staff, ["ended"])
        self.assertEqual((again.reset, again.refused), (0, 1))
        async with self.Session() as session:
            after = (await session.scalar(select(func.count(Order.id))), await session.scalar(select(func.count(UserService.id))))
            actions = list(await session.scalars(select(ShopStaffAction)))
        self.assertEqual(before, after)
        self.assertEqual([(a.action, a.shop_id, a.reason) for a in actions], [("trial_reset", self.shop_a.id, "1 کاربر")])


class RetrialPageTests(RetrialCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        from app.api.app import render

        self.staff: dict = {}

        async def get_db():
            async with self.Session() as session:
                yield session

        app = FastAPI()
        register_trial_users_pages(app, render=render, require_staff=lambda: self.staff, get_db=get_db)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await super().asyncTearDown()

    async def act(self, staff: dict, names: list[str]):
        self.staff.clear()
        self.staff.update(staff)
        data = {"action": "retrial", "ids": [str(self.ids[n]) for n in names], "return_to": "/trial-users"}
        return await self.client.post("/trial-users/bulk-action", data=data)

    async def test_the_action_needs_its_own_permission(self) -> None:
        response = await self.act(shop_staff(self.shop_a.id, ["trial_users", "trial_users_manage"]), ["ended"])
        self.assertEqual(response.status_code, 403)
        self.assertIn(self.ids["ended"], await self.claims_in(self.shop_a.id))
        response = await self.act(shop_staff(self.shop_a.id, ["trial_users", "trial_reset"]), ["ended"])
        self.assertEqual(response.status_code, 303)
        self.assertNotIn(self.ids["ended"], await self.claims_in(self.shop_a.id))

    async def test_a_shop_cannot_reset_another_shops_trial(self) -> None:
        response = await self.act(shop_staff(self.shop_a.id, ["trial_users", "trial_reset"]), ["other_shop"])
        self.assertIn("err=", response.headers["location"])
        self.assertIn(self.ids["other_shop"], await self.claims_in(self.shop_b.id))

    async def test_the_row_offers_the_action_only_where_it_applies(self) -> None:
        self.staff.clear()
        self.staff.update(shop_staff(self.shop_a.id, ["trial_users", "trial_reset"]))
        page = (await self.client.get("/trial-users")).text
        self.assertIn('data-bulk-op="retrial"', page)
        rows = {int(uid): ops for uid, ops in __import__("re").findall(r'data-uid="(\d+)" data-bulk-ops="([^"]*)"', page)}
        self.assertIn("retrial", rows[self.ids["ended"]])
        for name in ("active", "buyer", "blocked"):
            self.assertNotIn("retrial", rows[self.ids[name]], name)


class RetrialToolRowTests(RetrialCase):
    def test_the_tools_list_offers_the_round_only_with_the_right(self) -> None:
        hrefs = lambda staff: [s.href for s in tool_sections(staff)]  # noqa: E731 - tiny local reader
        self.assertIn("/tools/retrial", hrefs(shop_staff(self.shop_a.id, ["trial_users", "trial_reset"])))
        self.assertNotIn("/tools/retrial", hrefs(shop_staff(self.shop_a.id, ["trial_users"])))
        self.assertNotIn("/tools/retrial", hrefs(shop_staff(self.shop_a.id, ["trial_reset"])))
        self.assertIn("/tools/retrial", hrefs(OWNER))
