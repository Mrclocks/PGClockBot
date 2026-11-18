"""The Owner page that assigns services with an unknown shop."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit


from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, UserService
from tests.test_update_owner_only import OWNER_SESSION, SHOP_SESSION, RoutesHarness

SHOP_ID, CUSTOMER, REVIEW_SVC = 5, 7, 41
HOSTILE_NAME = "<script>alert(1)</script>"


class ServiceShopReviewPageTests(RoutesHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        async with AsyncSession(bind=self.engine, expire_on_commit=False) as session:
            session.add(BotUser(id=CUSTOMER, telegram_id=107, referral_code="cust", role="user", reseller_id=SHOP_ID))
            await session.flush()
            session.add(UserService(
                id=REVIEW_SVC, bot_user_id=CUSTOMER, pg_user_id=1, pg_username=HOSTILE_NAME,
                remark="imported", shop_needs_review=True,
            ))
            await session.commit()

    async def request(self, session_payload: dict, method: str, path: str, **form: str):
        cookie = f"session={self.signer.dumps(session_payload)}; csrf=token"
        headers = {"cookie": cookie, "x-csrf-token": "token", "origin": "http://panel.test"}
        return await self.client.request(method, path, headers=headers, data=form or None)

    async def review_state(self) -> tuple[bool, int | None]:
        async with AsyncSession(bind=self.engine) as session:
            service = await session.get(UserService, REVIEW_SVC)
            return service.shop_needs_review, service.shop_id

    async def test_the_owner_sees_the_waiting_service_and_its_name_is_escaped(self):
        response = await self.request(OWNER_SESSION, "GET", "/service-shop-review")
        self.assertEqual(response.status_code, 200)
        self.assertIn(f"#{REVIEW_SVC}", response.text)
        self.assertNotIn(HOSTILE_NAME, response.text)
        self.assertIn("&lt;script&gt;", response.text)

    async def test_a_shop_owner_is_turned_away_from_the_page_and_the_assignment(self):
        for method, path in (("GET", "/service-shop-review"), ("POST", f"/service-shop-review/{REVIEW_SVC}/assign")):
            response = await self.request(SHOP_SESSION, method, path, shop="platform")
            self.assertEqual(response.status_code, 303, path)
            self.assertNotIn("/service-shop-review", response.headers["location"], path)
        self.assertEqual(await self.review_state(), (True, None))

    async def test_the_owner_assigns_a_shop_and_the_service_leaves_the_list(self):
        response = await self.request(OWNER_SESSION, "POST", f"/service-shop-review/{REVIEW_SVC}/assign", shop=str(SHOP_ID))
        self.assertIn("ok", parse_qs(urlsplit(response.headers["location"]).query))
        self.assertEqual(await self.review_state(), (False, SHOP_ID))
        page = await self.request(OWNER_SESSION, "GET", "/service-shop-review")
        self.assertNotIn(f"#{REVIEW_SVC}", page.text)

    async def test_an_invalid_choice_changes_nothing(self):
        for choice in ("", "abc", "-3", "0", "999"):
            response = await self.request(OWNER_SESSION, "POST", f"/service-shop-review/{REVIEW_SVC}/assign", shop=choice)
            self.assertIn("err", parse_qs(urlsplit(response.headers["location"]).query), choice)
        self.assertEqual(await self.review_state(), (True, None))

    async def test_a_second_assignment_is_refused(self):
        await self.request(OWNER_SESSION, "POST", f"/service-shop-review/{REVIEW_SVC}/assign", shop="platform")
        response = await self.request(OWNER_SESSION, "POST", f"/service-shop-review/{REVIEW_SVC}/assign", shop=str(SHOP_ID))
        self.assertIn("err", parse_qs(urlsplit(response.headers["location"]).query))
        self.assertEqual(await self.review_state(), (False, None))
