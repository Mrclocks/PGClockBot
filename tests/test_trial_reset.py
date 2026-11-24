"""«ریست کاربران تست»: each group's count is exact, a reset reaches only ready customers of this shop's group and
never more than the confirmed number, runs once, needs both trial rights, and the page shows only fixed messages."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI
from sqlalchemy import func, select

from app.api.trial_reset_pages import register_trial_reset_pages
from app.db.models import BotUser, Order, ShopStaffAction, TrialClaim, UserService
from app.services.trial_reset import parse_confirmed, parse_group, reset_trial_group, retrial_group_counts
from app.services.trial_user_actions import trial_shop_key
from tests.trial_users_support import NOW, OWNER, TrialUsersTestCase, shop_staff

ENDED = NOW - timedelta(days=3)
# The trial state is read against the real clock, so a running trial must end after today.
STILL_ON = datetime.now(timezone.utc) + timedelta(days=30)
RIGHTS = ["trial_users", "trial_reset"]
CUSTOMERS = (
    "ended", "buyer", "active", "blocked_buyer", "unsynced", "no_service", "second_round", "no_claim", "other_shop",
)


class TrialResetCase(TrialUsersTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        async with self.Session() as session:
            self.ids = {name: (await self._user(session, 700 + n)).id for n, name in enumerate(CUSTOMERS)}
            await self._trials(session)
            await session.commit()

    async def _service(self, session, name: str, *, running: bool, synced: bool = True) -> UserService:
        service = UserService(
            bot_user_id=self.ids[name], plan_id=self.trial_plan.id, pg_user_id=800 + len(name), pg_username=name,
            quota_status="active" if running else "expired", quota_synced_at=NOW if synced else None,
            quota_expire_at=STILL_ON if running else ENDED,
        )
        session.add(service)
        await session.flush()
        return service

    async def _trials(self, session) -> None:
        shop = self.shop_a.id
        users = {name: await session.get(BotUser, uid) for name, uid in self.ids.items()}
        for name in ("ended", "buyer", "blocked_buyer", "second_round", "no_claim"):
            await self._take_trial(session, users[name], shop=shop, service=await self._service(session, name, running=False))
        await self._take_trial(session, users["active"], shop=shop, service=await self._service(session, "active", running=True))
        unsynced = await self._service(session, "unsynced", running=True, synced=False)
        await self._take_trial(session, users["unsynced"], shop=shop, service=unsynced)
        await self._take_trial(session, users["no_service"], shop=shop)
        # A second trial after an earlier reset is still running: the first one having ended is not enough.
        await self._take_trial(session, users["second_round"], shop=shop, service=await self._service(session, "second_round", running=True))
        await self._take_trial(session, users["other_shop"], shop=self.shop_b.id, service=await self._service(session, "other_shop", running=False))
        for name in ("buyer", "blocked_buyer"):
            await self._buy(session, users[name], shop=shop)
        users["blocked_buyer"].is_blocked = True
        for name in CUSTOMERS:
            if name != "no_claim":
                session.add(TrialClaim(user_id=self.ids[name], shop_key=trial_shop_key(self.shop_b.id if name == "other_shop" else shop)))
        # The same customer also took shop B's trial: shop A's reset must leave that claim alone.
        session.add(TrialClaim(user_id=self.ids["ended"], shop_key=trial_shop_key(self.shop_b.id)))

    async def claims_in(self, shop_id: int) -> set[str]:
        names = {uid: name for name, uid in self.ids.items()}
        async with self.Session() as session:
            rows = await session.scalars(select(TrialClaim.user_id).where(TrialClaim.shop_key == trial_shop_key(shop_id)))
            return {names[int(uid)] for uid in rows}

    async def reset(self, group: str, confirmed: int, *, staff: dict | None = None) -> int:
        async with self.Session() as session:
            return await reset_trial_group(
                session, staff=staff or shop_staff(self.shop_a.id, RIGHTS), shop_id=self.shop_a.id,
                group=group, confirmed=confirmed,
            )


class TrialResetServiceTests(TrialResetCase):
    async def test_each_group_counts_its_trial_takers_and_the_ready_ones(self) -> None:
        async with self.Session() as session:
            counts = await retrial_group_counts(session, shop_id=self.shop_a.id)
        self.assertEqual(
            [(c.key, c.label, c.trials, c.ready) for c in counts],
            [("all", "همه", 8, 3), ("buyers", "خریدار", 2, 1), ("non_buyers", "بدون خرید", 6, 2)],
        )

    async def test_non_buyers_reset_only_the_ready_ones_of_this_shop(self) -> None:
        self.assertEqual(await self.reset("non_buyers", 2), 2)
        self.assertEqual(
            await self.claims_in(self.shop_a.id), {"buyer", "active", "blocked_buyer", "unsynced", "second_round"}
        )
        self.assertEqual(await self.claims_in(self.shop_b.id), {"other_shop", "ended"})

    async def test_buyers_reset_skips_a_blocked_buyer(self) -> None:
        self.assertEqual(await self.reset("buyers", 5), 1)
        self.assertNotIn("buyer", await self.claims_in(self.shop_a.id))
        self.assertIn("blocked_buyer", await self.claims_in(self.shop_a.id))

    async def test_all_resets_every_ready_customer_and_keeps_orders_and_services(self) -> None:
        async with self.Session() as session:
            before = (await session.scalar(select(func.count(Order.id))), await session.scalar(select(func.count(UserService.id))))
        self.assertEqual(await self.reset("all", 100), 3)
        self.assertEqual(await self.claims_in(self.shop_a.id), {"active", "blocked_buyer", "unsynced", "second_round"})
        async with self.Session() as session:
            after = (await session.scalar(select(func.count(Order.id))), await session.scalar(select(func.count(UserService.id))))
        self.assertEqual(before, after)

    async def test_a_reset_never_reaches_more_than_the_confirmed_number(self) -> None:
        self.assertEqual(await self.reset("all", 1), 1)
        async with self.Session() as session:
            remaining = await retrial_group_counts(session, shop_id=self.shop_a.id)
        self.assertEqual(remaining[0].ready, 2)

    async def test_repeated_reset_changes_nothing_and_is_recorded_once(self) -> None:
        self.assertEqual(await self.reset("non_buyers", 2), 2)
        self.assertEqual(await self.reset("non_buyers", 2), 0)
        async with self.Session() as session:
            actions = list(await session.scalars(select(ShopStaffAction)))
        self.assertEqual([(a.action, a.shop_id, a.reason) for a in actions], [("trial_reset", self.shop_a.id, "2 کاربر · بدون خرید")])

    async def test_concurrent_resets_together_stay_within_one_confirmation(self) -> None:
        results = await asyncio.gather(self.reset("non_buyers", 2), self.reset("non_buyers", 2))
        self.assertEqual(sum(results), 2)
        self.assertEqual(len(await self.claims_in(self.shop_a.id)), 5)

    async def test_the_platform_counts_only_its_own_trials(self) -> None:
        async with self.Session() as session:
            counts = await retrial_group_counts(session, shop_id=None)
        self.assertEqual([(c.trials, c.ready) for c in counts], [(0, 0), (0, 0), (0, 0)])

    def test_group_and_confirmed_number_are_validated(self) -> None:
        self.assertEqual(parse_group("buyers"), "buyers")
        for bad in ("", "BUYERS", "all;", "../all"):
            with self.subTest(group=bad), self.assertRaises(ValueError):
                parse_group(bad)
        self.assertEqual(parse_confirmed("12"), 12)
        for bad in (None, "", "0", "-1", "1.5", "True", "abc", "۱۲", "99999999999", str(2**31)):
            with self.subTest(confirmed=bad), self.assertRaises(ValueError):
                parse_confirmed(bad)


class TrialResetPageTests(TrialResetCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        from app.api.app import render

        self.staff: dict = {}

        async def get_db():
            async with self.Session() as session:
                yield session

        app = FastAPI()
        register_trial_reset_pages(app, render=render, require_staff=lambda: self.staff, get_db=get_db)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await super().asyncTearDown()

    def as_staff(self, staff: dict) -> None:
        self.staff.clear()
        self.staff.update(staff)

    async def test_the_page_shows_each_group_with_its_count_and_a_confirmed_action(self) -> None:
        self.as_staff(shop_staff(self.shop_a.id, RIGHTS))
        page = (await self.client.get("/tools/retrial")).text
        self.assertIn("۳ نفر آمادهٔ تست دوباره · از ۸ تست‌گرفته", page)
        self.assertIn("۱ نفر آمادهٔ تست دوباره · از ۲ تست‌گرفته", page)
        self.assertIn("۲ نفر آمادهٔ تست دوباره · از ۶ تست‌گرفته", page)
        forms = re.findall(r'<form method="post" action="(/tools/retrial/\w+)"', page)
        self.assertEqual(forms, ["/tools/retrial/all", "/tools/retrial/buyers", "/tools/retrial/non_buyers"])
        self.assertIn('name="confirmed" value="2"', page)
        self.assertIn('data-confirm-label="ریست شود"', page)

    async def test_a_group_with_nobody_ready_has_no_action(self) -> None:
        await self.reset("buyers", 5)
        self.as_staff(shop_staff(self.shop_a.id, RIGHTS))
        page = (await self.client.get("/tools/retrial")).text
        self.assertIn("کسی برای ریست نیست · از ۲ تست‌گرفته", page)
        self.assertNotIn('action="/tools/retrial/buyers"', page)

    async def test_both_trial_rights_are_needed(self) -> None:
        for rights in (["trial_users"], ["trial_reset"], ["trial_users", "trial_users_manage"]):
            with self.subTest(rights=rights):
                self.as_staff(shop_staff(self.shop_a.id, rights))
                self.assertEqual((await self.client.get("/tools/retrial")).status_code, 403)
                response = await self.client.post("/tools/retrial/all", data={"confirmed": "10"})
                self.assertEqual(response.status_code, 403)
        self.assertEqual(len(await self.claims_in(self.shop_a.id)), 7)

    async def test_the_shop_comes_from_the_signed_in_staff_not_the_request(self) -> None:
        self.as_staff(shop_staff(self.shop_b.id, RIGHTS))
        page = (await self.client.get("/tools/retrial")).text
        self.assertIn("۱ نفر آمادهٔ تست دوباره · از ۱ تست‌گرفته", page)
        response = await self.client.post(
            "/tools/retrial/all", data={"confirmed": "10", "shop_id": str(self.shop_a.id), "reseller_id": str(self.shop_a.id)}
        )
        self.assertEqual(response.headers["location"], "/tools/retrial?reset=1")
        self.assertEqual(len(await self.claims_in(self.shop_a.id)), 7)
        self.assertEqual(await self.claims_in(self.shop_b.id), {"ended"})

    async def test_the_owner_resets_the_platform_shop_only(self) -> None:
        self.as_staff(dict(OWNER))
        response = await self.client.post("/tools/retrial/all", data={"confirmed": "10"})
        self.assertEqual(response.headers["location"], "/tools/retrial?err=none")
        self.assertEqual(len(await self.claims_in(self.shop_a.id)), 7)

    async def test_invalid_group_or_number_resets_nothing(self) -> None:
        self.as_staff(shop_staff(self.shop_a.id, RIGHTS))
        for path, data in (
            ("/tools/retrial/everyone", {"confirmed": "5"}),
            ("/tools/retrial/all", {}),
            ("/tools/retrial/all", {"confirmed": "-3"}),
            ("/tools/retrial/all", {"confirmed": "true"}),
        ):
            with self.subTest(path=path, data=data):
                response = await self.client.post(path, data=data)
                self.assertEqual(response.headers["location"], "/tools/retrial?err=invalid")
        self.assertEqual(len(await self.claims_in(self.shop_a.id)), 7)

    async def test_a_reset_reports_the_real_count_and_a_repeat_says_nobody_is_left(self) -> None:
        self.as_staff(shop_staff(self.shop_a.id, RIGHTS))
        first = await self.client.post("/tools/retrial/non_buyers", data={"confirmed": "2"})
        self.assertEqual(first.headers["location"], "/tools/retrial?reset=2")
        page = (await self.client.get(first.headers["location"])).text
        self.assertIn("۲ کاربر می‌توانند دوباره تست بگیرند", page)
        again = await self.client.post("/tools/retrial/non_buyers", data={"confirmed": "2"})
        self.assertEqual(again.headers["location"], "/tools/retrial?err=none")

    async def test_the_page_never_shows_text_from_the_address(self) -> None:
        self.as_staff(shop_staff(self.shop_a.id, RIGHTS))
        page = (await self.client.get("/tools/retrial?err=<script>x</script>&reset=<b>9</b>")).text
        self.assertNotIn("<script>x</script>", page)
        self.assertNotIn("&lt;script&gt;", page)
        self.assertNotIn("<b>9</b>", page)
        self.assertNotIn('class="flash', page)
