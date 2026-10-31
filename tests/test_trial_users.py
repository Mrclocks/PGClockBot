"""Trial users page: who took the trial, who then bought, and what each shop may see."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import httpx
from fastapi import FastAPI
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.trial_users_pages import register_trial_users_pages
from app.db import Base
from app.db.models import BotUser, MarketingPreference, Order, Plan, Role, UserService
from app.services.marketing_nav import TRIAL_USERS, marketing_sections
from app.services.trial_users import (
    list_trial_users,
    normalize_filter,
    trial_state,
    trial_summary,
)

OWNER = {
    "role": "admin",
    "permissions": [],
    "org_principal_id": 1,
    "org_depth": 0,
    "org_parent_id": None,
    "org_status": "active",
}
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)


def _shop_staff(shop_id: int, permissions: list[str]) -> dict:
    return {"role": "reseller", "permissions": permissions, "bot_user_id": shop_id}


class TrialUsersTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmpdir.name) / 't.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            self.shop_a = await self._user(session, 9001, role=Role.RESELLER.value)
            self.shop_b = await self._user(session, 9002, role=Role.RESELLER.value)
            self.trial_plan = Plan(name="trial", price=0, is_trial=True)
            self.paid_plan = Plan(name="paid", price=50000)
            session.add_all([self.trial_plan, self.paid_plan])
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self._tmpdir.cleanup()

    async def _user(self, session, telegram_id: int, *, name: str | None = None, shop: int | None = None, role: str = Role.USER.value) -> BotUser:
        user = BotUser(
            telegram_id=telegram_id,
            username=f"u{telegram_id}",
            full_name=name or f"user {telegram_id}",
            role=role,
            referral_code=f"R{telegram_id}",
            reseller_id=shop,
        )
        session.add(user)
        await session.flush()
        return user

    async def _order(self, session, user, plan, *, shop=None, amount=0, status="delivered", note=None, service_id=None) -> Order:
        order = Order(
            user_id=user.id, plan_id=plan.id, reseller_id=shop, amount=amount,
            status=status, note=note, service_id=service_id,
        )
        session.add(order)
        await session.flush()
        return order

    async def _take_trial(self, session, user, *, shop=None, service=None) -> Order:
        order = await self._order(session, user, self.trial_plan, shop=shop, service_id=service.id if service else None)
        return order

    async def _buy(self, session, user, *, shop=None, amount=50000, **kwargs) -> Order:
        return await self._order(session, user, self.paid_plan, shop=shop, amount=amount, **kwargs)

    async def _list(self, session, staff, shop_id, **kwargs):
        return await list_trial_users(session, staff=staff, shop_id=shop_id, now=NOW, **kwargs)


class BuyerTagTests(TrialUsersTestCase):
    async def test_buyer_tag_follows_delivered_paid_orders_of_the_same_shop(self) -> None:
        async with self.Session() as session:
            browsing = await self._user(session, 1)
            buyer = await self._user(session, 2)
            pending_only = await self._user(session, 3)
            other_shop_buyer = await self._user(session, 4)
            agency_payer = await self._user(session, 5)
            for user in (browsing, buyer, pending_only, other_shop_buyer, agency_payer):
                await self._take_trial(session, user)
            await self._buy(session, buyer)
            await self._buy(session, pending_only, status="pending")
            await self._buy(session, other_shop_buyer, shop=self.shop_a.id)
            await self._buy(session, agency_payer, note="reseller_app:7")
            await session.commit()

            page = await self._list(session, OWNER, None)

        by_id = {row.user_id: row for row in page.rows}
        self.assertTrue(by_id[buyer.id].is_buyer)
        self.assertEqual(by_id[buyer.id].total_spent, 50000)
        for user in (browsing, pending_only, other_shop_buyer, agency_payer):
            self.assertFalse(by_id[user.id].is_buyer, user.telegram_id)

    async def test_a_renewal_counts_as_a_purchase_and_the_trial_itself_does_not(self) -> None:
        async with self.Session() as session:
            user = await self._user(session, 10)
            await self._take_trial(session, user)
            await self._buy(session, user, amount=40000, note="renew:12")
            await session.commit()
            page = await self._list(session, OWNER, None)
        row = page.rows[0]
        self.assertTrue(row.is_buyer)
        self.assertEqual((row.purchase_count, row.total_spent), (1, 40000))

    async def test_filters_split_buyers_from_the_rest(self) -> None:
        async with self.Session() as session:
            buyer = await self._user(session, 20)
            other = await self._user(session, 21)
            await self._take_trial(session, buyer)
            await self._take_trial(session, other)
            await self._buy(session, buyer)
            await session.commit()
            buyers = await self._list(session, OWNER, None, trial_filter="buyers")
            non_buyers = await self._list(session, OWNER, None, trial_filter="non_buyers")
            summary = await trial_summary(session, shop_id=None)
        self.assertEqual([r.user_id for r in buyers.rows], [buyer.id])
        self.assertEqual([r.user_id for r in non_buyers.rows], [other.id])
        self.assertEqual((summary.trials, summary.buyers, summary.conversion_percent), (2, 1, 50))

    def test_unknown_filter_falls_back_to_everyone(self) -> None:
        self.assertEqual(normalize_filter("'; drop table"), "all")
        self.assertEqual(normalize_filter("buyers"), "buyers")


class ShopIsolationTests(TrialUsersTestCase):
    async def test_each_shop_lists_only_the_trials_its_own_bot_delivered(self) -> None:
        async with self.Session() as session:
            platform_user = await self._user(session, 30)
            a_user = await self._user(session, 31, shop=self.shop_a.id)
            b_user = await self._user(session, 32, shop=self.shop_b.id)
            await self._take_trial(session, platform_user)
            await self._take_trial(session, a_user, shop=self.shop_a.id)
            await self._take_trial(session, b_user, shop=self.shop_b.id)
            await session.commit()

            platform = await self._list(session, OWNER, None)
            shop_a = await self._list(session, _shop_staff(self.shop_a.id, ["trial_users"]), self.shop_a.id)
            summary_a = await trial_summary(session, shop_id=self.shop_a.id)

        self.assertEqual([r.user_id for r in platform.rows], [platform_user.id])
        self.assertEqual([r.user_id for r in shop_a.rows], [a_user.id])
        self.assertEqual(summary_a.trials, 1)

    async def test_a_purchase_in_another_shop_does_not_tag_a_buyer(self) -> None:
        async with self.Session() as session:
            user = await self._user(session, 40, shop=self.shop_a.id)
            await self._take_trial(session, user, shop=self.shop_a.id)
            await self._buy(session, user, shop=self.shop_b.id)
            await session.commit()
            page = await self._list(session, _shop_staff(self.shop_a.id, ["trial_users"]), self.shop_a.id)
        self.assertFalse(page.rows[0].is_buyer)

    async def test_a_shop_manages_only_customers_attributed_to_it(self) -> None:
        async with self.Session() as session:
            own = await self._user(session, 50, shop=self.shop_a.id)
            foreign = await self._user(session, 51, shop=self.shop_b.id)
            await self._take_trial(session, own, shop=self.shop_a.id)
            await self._take_trial(session, foreign, shop=self.shop_a.id)
            await session.commit()
            page = await self._list(session, _shop_staff(self.shop_a.id, ["trial_users"]), self.shop_a.id)
        manageable = {row.user_id: row.can_manage for row in page.rows}
        self.assertEqual(manageable, {own.id: True, foreign.id: False})


class ListBehaviourTests(TrialUsersTestCase):
    async def test_pages_are_bounded_and_follow_the_cursor_without_overlap(self) -> None:
        async with self.Session() as session:
            for index in range(5):
                await self._take_trial(session, await self._user(session, 100 + index))
            await session.commit()
            first = await self._list(session, OWNER, None, limit=2)
            second = await self._list(session, OWNER, None, limit=2, before=first.next_before)
            third = await self._list(session, OWNER, None, limit=2, before=second.next_before)
        seen = [r.user_id for page in (first, second, third) for r in page.rows]
        self.assertEqual(len(seen), 5)
        self.assertEqual(len(set(seen)), 5)
        self.assertEqual([len(p.rows) for p in (first, second, third)], [2, 2, 1])
        self.assertIsNone(third.next_before)

    async def test_a_page_costs_the_same_number_of_queries_whatever_its_size(self) -> None:
        async with self.Session() as session:
            for index in range(30):
                user = await self._user(session, 700 + index)
                await self._take_trial(session, user)
                await self._buy(session, user)
            await session.commit()
            counts: list[int] = []
            for limit in (2, 30):
                statements: list[str] = []
                listener = lambda *args: statements.append(args[2])  # noqa: E731 - test shorthand
                event.listen(self.engine.sync_engine, "before_cursor_execute", listener)
                try:
                    await self._list(session, OWNER, None, limit=limit)
                finally:
                    event.remove(self.engine.sync_engine, "before_cursor_execute", listener)
                counts.append(len(statements))
        self.assertEqual(counts[0], counts[1])

    async def test_search_treats_wildcards_as_text(self) -> None:
        async with self.Session() as session:
            await self._take_trial(session, await self._user(session, 200, name="Ali"))
            await self._take_trial(session, await self._user(session, 201, name="100% real"))
            await session.commit()
            wildcard = await self._list(session, OWNER, None, search="%")
            by_name = await self._list(session, OWNER, None, search="ali")
            by_telegram = await self._list(session, OWNER, None, search="201")
        self.assertEqual([r.name for r in wildcard.rows], ["100% real"])
        self.assertEqual([r.name for r in by_name.rows], ["Ali"])
        self.assertEqual([r.telegram_id for r in by_telegram.rows], [201])

    async def test_blocked_and_opted_out_customers_are_marked(self) -> None:
        async with self.Session() as session:
            user = await self._user(session, 300)
            user.is_blocked = True
            await self._take_trial(session, user)
            session.add(MarketingPreference(user_id=user.id, shop_key=0, enabled=False))
            await session.commit()
            row = (await self._list(session, OWNER, None)).rows[0]
        self.assertTrue(row.is_blocked)
        self.assertTrue(row.is_opted_out)

    async def test_trial_state_comes_from_the_cached_quota(self) -> None:
        async with self.Session() as session:
            user = await self._user(session, 400)
            service = UserService(
                bot_user_id=user.id, pg_username="t400", quota_status="active",
                quota_expire_at=NOW - timedelta(days=1), quota_synced_at=NOW,
            )
            session.add(service)
            await session.flush()
            await self._take_trial(session, user, service=service)
            await session.commit()
            row = (await self._list(session, OWNER, None)).rows[0]
        self.assertEqual(row.trial_state, "ended")

    def test_state_is_unknown_until_the_quota_was_synced(self) -> None:
        self.assertEqual(trial_state(None, now=NOW)[0], "unknown")
        unsynced = UserService(bot_user_id=1, pg_username="x", quota_status="active")
        self.assertEqual(trial_state(unsynced, now=NOW)[0], "unknown")
        live = UserService(
            bot_user_id=1, pg_username="x", quota_status="active",
            quota_expire_at=NOW + timedelta(days=2), quota_synced_at=NOW,
        )
        self.assertEqual(trial_state(live, now=NOW)[0], "active")


class TrialUsersPageTests(TrialUsersTestCase):
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

    async def _get(self, staff: dict, url: str = "/trial-users"):
        self.staff.clear()
        self.staff.update(staff)
        return await self.client.get(url)

    async def test_staff_without_the_permission_get_nothing(self) -> None:
        for staff in (
            _shop_staff(self.shop_a.id, ["orders", "campaigns"]),
            _shop_staff(self.shop_a.id, ["trial_users_manage"]),
            {"role": "pg_staff", "permissions": ["trial_users"]},
            {"role": "reseller", "permissions": ["trial_users"], "bot_user_id": None},
        ):
            with self.subTest(staff=staff):
                self.assertEqual((await self._get(staff)).status_code, 403)

    async def test_hostile_names_are_escaped_and_the_phone_never_appears(self) -> None:
        async with self.Session() as session:
            user = await self._user(session, 500, name="<script>alert(1)</script>")
            await self._take_trial(session, user)
            await session.commit()
        response = await self._get(OWNER)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("<script>alert(1)</script>", response.text)
        self.assertIn("&lt;script&gt;", response.text)

    async def test_shop_staff_see_their_shop_whatever_the_query_says(self) -> None:
        async with self.Session() as session:
            mine = await self._user(session, 600, name="mine", shop=self.shop_a.id)
            theirs = await self._user(session, 601, name="theirs", shop=self.shop_b.id)
            await self._take_trial(session, mine, shop=self.shop_a.id)
            await self._take_trial(session, theirs, shop=self.shop_b.id)
            await session.commit()
        response = await self._get(
            _shop_staff(self.shop_a.id, ["trial_users"]),
            f"/trial-users?shop_id={self.shop_b.id}&reseller_id={self.shop_b.id}",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("mine", response.text)
        self.assertNotIn("theirs", response.text)

    async def test_the_settings_shortcut_points_each_role_to_its_own_settings(self) -> None:
        owner = await self._get(OWNER)
        self.assertIn('href="/settings?tab=limits"', owner.text)
        with_settings = await self._get(_shop_staff(self.shop_a.id, ["trial_users", "shop_settings"]))
        self.assertIn('href="/shop-settings?tab=limits"', with_settings.text)
        self.assertNotIn("/settings?tab=limits", with_settings.text.replace("/shop-settings?tab=limits", ""))
        without = await self._get(_shop_staff(self.shop_a.id, ["trial_users"]))
        self.assertNotIn("tab=limits", without.text)

    async def test_a_malformed_cursor_or_filter_is_ignored(self) -> None:
        response = await self._get(OWNER, "/trial-users?before=abc&filter=%27%3B--&q=" + "x" * 500)
        self.assertEqual(response.status_code, 200)

    async def test_the_tab_is_offered_only_with_the_permission(self) -> None:
        keys = lambda staff: [s.key for s in marketing_sections(staff)]  # noqa: E731 - test shorthand
        self.assertIn(TRIAL_USERS, keys(OWNER))
        self.assertIn(TRIAL_USERS, keys(_shop_staff(5, ["trial_users"])))
        self.assertNotIn(TRIAL_USERS, keys(_shop_staff(5, ["campaigns", "trial_users_manage"])))


if __name__ == "__main__":
    unittest.main()
