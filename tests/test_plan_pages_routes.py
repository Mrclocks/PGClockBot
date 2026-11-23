"""The /plans pages, checked through their routes: what they render, save and refuse.

These replace checks that read the source of app/api/plan_pages.py for the same promises.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import BotUser, Order, OrderStatus, Plan, UserService
from tests.test_update_owner_only import OWNER_SESSION, RoutesHarness

LEAKY = "password=hunter2 at /app/services/resellers.py line 7"


class PlanPagesHarness(RoutesHarness):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add(BotUser(id=1, telegram_id=101, referral_code="u1", role="user"))
            session.add(Plan(id=7, name="ماهانه", price=100_000, duration_days=30, data_limit_gb=10,
                             is_active=True, pg_template_id=1))
            await session.commit()
        self.panel = MagicMock()
        self.panel.get_admin_roles = AsyncMock(return_value=[])
        self.panel.create_user_template = AsyncMock(return_value={"id": 999})
        for target, value in (
            ("app.services.plans_catalog.load_pg_plan_options", AsyncMock(return_value=([], [], None))),
            ("app.services.pasarguard.get_pg", MagicMock(return_value=self.panel)),
            # The quota check reads the staff member's limits from the panel.
            ("app.services.pg_quota.assert_user_plan_within_limits", AsyncMock(return_value=None)),
            # The owner's PasarGuard role, read from the panel on every request: an unrestricted owner
            # who may create templates.
            ("app.services.pg_access.enrich_platform_admin_staff",
             AsyncMock(side_effect=lambda user: {**user, "pg_is_owner": True,
                                                 "pg_actions": {"templates": {"create": True}}})),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def post(self, path: str, data: dict) -> object:
        cookie = f"session={self.signer.dumps(OWNER_SESSION)}; csrf=token"
        headers = {"cookie": cookie, "x-csrf-token": "token", "origin": "http://panel.test"}
        return await self.client.post(path, headers=headers, data={"csrf_token": "token", **data})

    async def plan(self, plan_id: int) -> Plan | None:
        async with self.Session() as session:
            return await session.get(Plan, plan_id)

    @staticmethod
    def flash(response, key: str) -> str:
        return parse_qs(urlsplit(response.headers.get("location", "")).query).get(key, [""])[0]


class PlansPageTests(PlanPagesHarness):
    async def test_the_page_still_renders_when_the_reseller_plan_list_fails_and_hides_why(self) -> None:
        with patch("app.services.resellers.list_reseller_plans", AsyncMock(side_effect=RuntimeError(LEAKY))):
            response = await self.call(OWNER_SESSION, "GET", "/plans")
        self.assertEqual(response.status_code, 200)
        self.assertIn("ماهانه", response.text)
        self.assertNotIn("hunter2", response.text)
        self.assertNotIn("/app/services", response.text)

    async def plans_page_with_live_templates(self, template_ids: list[int]) -> str:
        options = AsyncMock(return_value=([{"id": tid, "name": f"t{tid}"} for tid in template_ids], [], None))
        with patch("app.services.plans_catalog.load_pg_plan_options", options):
            response = await self.call(OWNER_SESSION, "GET", "/plans")
        self.assertEqual(response.status_code, 200)
        return response.text

    async def test_a_plan_whose_template_is_gone_from_the_panel_is_marked(self) -> None:
        self.assertIn("plan-pg-warn-dot", await self.plans_page_with_live_templates([2]))
        self.assertNotIn("plan-pg-warn-dot", await self.plans_page_with_live_templates([1]))


class CreatePlanTests(PlanPagesHarness):
    async def test_an_empty_duration_saves_an_unlimited_plan(self) -> None:
        response = await self.post("/plans", {"name": "بی‌زمان", "price": "50000", "duration_days": "",
                                              "data_limit_gb": "5", "mode": "group", "group_ids": "3"})
        self.assertEqual(response.status_code, 303, response.text[:300])
        async with self.Session() as session:
            plan = await session.scalar(select(Plan).where(Plan.name == "بی‌زمان"))
        self.assertIsNotNone(plan, self.flash(response, "err"))
        self.assertEqual(plan.duration_days, 0)

    async def test_a_template_made_with_the_plan_uses_the_staff_members_panel_client(self) -> None:
        staff_panel = MagicMock()
        staff_panel.create_user_template = AsyncMock(return_value={"id": 41})
        with patch("app.api.pg_pages._staff_pg", AsyncMock(return_value=(staff_panel, False))):
            response = await self.post("/plans", {"name": "با تمپلیت", "price": "50000", "duration_days": "30",
                                                  "data_limit_gb": "5", "mode": "group", "group_ids": "3",
                                                  "also_create_template": "1"})
        self.assertEqual(response.status_code, 303, response.text[:300])
        staff_panel.create_user_template.assert_awaited_once()
        self.panel.create_user_template.assert_not_awaited()
        async with self.Session() as session:
            plan = await session.scalar(select(Plan).where(Plan.name == "با تمپلیت"))
        self.assertIsNotNone(plan, self.flash(response, "err"))
        self.assertEqual(plan.pg_template_id, 41)


class EditPlanTests(PlanPagesHarness):
    async def test_clearing_the_duration_on_edit_makes_the_plan_unlimited(self) -> None:
        response = await self.post("/plans/7/edit", {"name": "ماهانه", "price": "100000", "duration_days": "",
                                                     "data_limit_gb": "10", "mode": "template",
                                                     "pg_template_id": "1"})
        self.assertEqual(response.status_code, 303, response.text[:300])
        self.assertEqual((await self.plan(7)).duration_days, 0, self.flash(response, "err"))


class DeletePlanTests(PlanPagesHarness):
    async def test_the_trial_plan_is_not_deleted_from_this_page(self) -> None:
        async with self.Session() as session:
            session.add(Plan(id=8, name="آزمایشی", price=0, duration_days=1, data_limit_gb=1, is_active=True,
                             is_trial=True))
            await session.commit()
        response = await self.post("/plans/8/delete", {})
        self.assertEqual((response.status_code, response.headers.get("location")), (303, "/plans"))
        self.assertIsNotNone(await self.plan(8))

    async def test_a_plan_with_a_paid_order_waiting_for_delivery_is_not_deleted(self) -> None:
        async with self.Session() as session:
            session.add(Order(user_id=1, plan_id=7, amount=100_000, status=OrderStatus.PAID.value))
            await session.commit()
        response = await self.post("/plans/7/delete", {})
        self.assertEqual(response.status_code, 303)
        self.assertIsNotNone(await self.plan(7))
        self.assertIn("در انتظار تحویل", self.flash(response, "err"))

    async def test_a_plan_a_service_uses_is_deleted_and_the_service_kept(self) -> None:
        async with self.Session() as session:
            session.add(UserService(id=70, bot_user_id=1, plan_id=7, pg_username="svc7"))
            await session.commit()
        response = await self.post("/plans/7/delete", {})
        self.assertEqual(response.status_code, 303)
        self.assertIsNone(await self.plan(7))
        async with self.Session() as session:
            service = await session.get(UserService, 70)
        self.assertIsNotNone(service)
        self.assertIsNone(service.plan_id)
