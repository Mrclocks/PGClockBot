"""Blocking and messaging trial users: only own trial takers, blocking stays with the Owner."""

from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import httpx
from fastapi import FastAPI
from sqlalchemy import func, select
from trial_users_support import OWNER, TrialUsersTestCase, shop_staff

from app.api.trial_users_pages import register_trial_users_pages
from app.db.models import BotUser, CampaignRecipient, MarketingPreference, TargetedCampaign
from app.services.trial_user_actions import block_trial_users, message_trial_users


class ActionTestCase(TrialUsersTestCase):
    async def _customers(self, session) -> dict[str, BotUser]:
        """Two trial takers of shop A, one of shop B and one platform customer without a trial."""
        people = {
            "a1": await self._user(session, 11, shop=self.shop_a.id),
            "a2": await self._user(session, 12, shop=self.shop_a.id),
            "b1": await self._user(session, 13, shop=self.shop_b.id),
            "no_trial": await self._user(session, 14, shop=self.shop_a.id),
        }
        await self._take_trial(session, people["a1"], shop=self.shop_a.id)
        await self._take_trial(session, people["a2"], shop=self.shop_a.id)
        await self._take_trial(session, people["b1"], shop=self.shop_b.id)
        await session.commit()
        return people


class MessageTests(ActionTestCase):
    async def test_a_message_reaches_only_trial_takers_of_the_acting_shop(self) -> None:
        async with self.Session() as session:
            people = await self._customers(session)
            staff = shop_staff(self.shop_a.id, ["trial_users", "trial_users_manage"])
            ids = [people[k].id for k in ("a1", "b1", "no_trial")]
            campaign = await message_trial_users(session, staff=staff, shop_id=self.shop_a.id, user_ids=ids, text="سلام")
            recipients = (await session.scalars(select(CampaignRecipient.user_id).where(CampaignRecipient.campaign_id == campaign.id))).all()
        self.assertEqual(list(recipients), [people["a1"].id])
        self.assertEqual((campaign.reseller_id, campaign.audience, campaign.status), (self.shop_a.id, "selected", "running"))

    async def test_blocked_and_opted_out_customers_are_left_out(self) -> None:
        async with self.Session() as session:
            people = await self._customers(session)
            people["a1"].is_blocked = True
            session.add(MarketingPreference(user_id=people["a2"].id, shop_key=self.shop_a.id, enabled=False))
            await session.commit()
            with self.assertRaisesRegex(ValueError, "گیرنده"):
                await message_trial_users(
                    session, staff=OWNER, shop_id=self.shop_a.id,
                    user_ids=[people["a1"].id, people["a2"].id], text="hello",
                )
            self.assertEqual(await session.scalar(select(func.count()).select_from(TargetedCampaign)), 0)

    async def test_hostile_or_empty_text_is_refused_before_anything_is_queued(self) -> None:
        async with self.Session() as session:
            people = await self._customers(session)
            for text in ("", "   ", "<script>x</script>", "x" * 4000):
                with self.subTest(text=text[:12]), self.assertRaises(ValueError):
                    await message_trial_users(
                        session, staff=OWNER, shop_id=self.shop_a.id, user_ids=[people["a1"].id], text=text,
                    )
            self.assertEqual(await session.scalar(select(func.count()).select_from(TargetedCampaign)), 0)


class BlockTests(ActionTestCase):
    async def test_the_owner_blocks_trial_takers_and_nobody_else(self) -> None:
        async with self.Session() as session:
            taker = await self._user(session, 21)
            bystander = await self._user(session, 22)
            await self._take_trial(session, taker)
            await session.commit()
            with patch("app.services.notifications.notify_account_edit", AsyncMock()):
                result = await block_trial_users(
                    session, staff=OWNER, shop_id=None, user_ids=[taker.id, bystander.id], block=True,
                )
            await session.refresh(taker)
            await session.refresh(bystander)
        self.assertEqual((result.changed, result.refused), (1, 1))
        self.assertTrue(taker.is_blocked)
        self.assertFalse(bystander.is_blocked)

    async def test_a_shop_cannot_block_because_the_flag_is_global(self) -> None:
        async with self.Session() as session:
            people = await self._customers(session)
            staff = shop_staff(self.shop_a.id, ["trial_users", "trial_users_manage"])
            with self.assertRaisesRegex(ValueError, "مالک"):
                await block_trial_users(session, staff=staff, shop_id=self.shop_a.id, user_ids=[people["a1"].id], block=True)
            await session.refresh(people["a1"])
        self.assertFalse(people["a1"].is_blocked)


class ActionRouteTests(ActionTestCase):
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

    async def _post(self, staff: dict, **form):
        self.staff.clear()
        self.staff.update(staff)
        return await self.client.post("/trial-users/bulk-action", data=form, follow_redirects=False)

    async def test_view_permission_alone_cannot_act(self) -> None:
        async with self.Session() as session:
            people = await self._customers(session)
        for staff in (shop_staff(self.shop_a.id, ["trial_users"]), shop_staff(self.shop_a.id, ["campaigns", "orders"])):
            with self.subTest(staff=staff):
                response = await self._post(staff, action="message", ids=[people["a1"].id], reason="hi")
                self.assertEqual(response.status_code, 403)

    async def test_a_shop_messages_its_takers_and_a_forged_id_is_ignored(self) -> None:
        async with self.Session() as session:
            people = await self._customers(session)
        staff = shop_staff(self.shop_a.id, ["trial_users", "trial_users_manage"])
        response = await self._post(
            staff, action="message", ids=[people["a1"].id, people["b1"].id], reason="تخفیف ویژه", return_to="/trial-users",
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("ok=", response.headers["location"])
        async with self.Session() as session:
            recipients = (await session.scalars(select(CampaignRecipient.user_id))).all()
        self.assertEqual(list(recipients), [people["a1"].id])

    async def test_a_shop_cannot_block_through_the_route(self) -> None:
        async with self.Session() as session:
            people = await self._customers(session)
        staff = shop_staff(self.shop_a.id, ["trial_users", "trial_users_manage"])
        response = await self._post(staff, action="block", ids=[people["a1"].id])
        self.assertIn("err=", response.headers["location"])
        async with self.Session() as session:
            self.assertFalse((await session.get(BotUser, people["a1"].id)).is_blocked)

    async def test_the_return_address_cannot_leave_the_panel(self) -> None:
        async with self.Session() as session:
            people = await self._customers(session)
        for target in ("https://evil.example/x", "//evil.example", "\\\\evil"):
            with self.subTest(target=target):
                response = await self._post(OWNER, action="nonsense", ids=[people["a1"].id], return_to=target)
                self.assertTrue(response.headers["location"].startswith("/trial-users"))

    async def test_nothing_selected_and_unknown_actions_are_reported_not_run(self) -> None:
        for form in ({"action": "message"}, {"action": "delete", "ids": [1]}):
            with self.subTest(form=form):
                response = await self._post(OWNER, **form)
                self.assertIn("err=", response.headers["location"])


if __name__ == "__main__":
    unittest.main()
