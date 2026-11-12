"""«یافتن با لینک اشتراک» on the web users page: POST only, scoped to the shop, never echoes the link."""

from __future__ import annotations

import os
from urllib.parse import parse_qs, urlencode, urlsplit

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.user_link_search_pages import LINK_SEARCH_PATH, NOT_FOUND_TEXT
from app.db.models import BotUser, UserService
from tests.test_update_owner_only import OWNER_SESSION, PG_STAFF_SESSION, SHOP_SESSION, RoutesHarness

SHOP = 5
PLATFORM_TOKEN = "PlatformTokenAbcdef0123456789ABCDEF-_xyz"
SHOP_TOKEN = "ShopFiveTokenAbcdef0123456789ABCDEF-_xyzq"
OTHER_TOKEN = "ShopSixTokenAbcdef0123456789ABCDEF-_xyzqq"
URL = "https://sub.example.com/sub/{}"


class FindByLinkTests(RoutesHarness):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        async with AsyncSession(bind=self.engine, expire_on_commit=False) as session:
            session.add(BotUser(id=6, telegram_id=106, referral_code="shop6", role="reseller"))
            await session.flush()
            session.add_all([
                BotUser(id=20, telegram_id=1020, referral_code="c20", full_name="Platform customer"),
                BotUser(id=21, telegram_id=1021, referral_code="c21", reseller_id=SHOP, full_name="Shop customer"),
                BotUser(id=22, telegram_id=1022, referral_code="c22", reseller_id=6, full_name="Other shop customer"),
            ])
            await session.flush()
            session.add_all([
                UserService(id=1, bot_user_id=20, pg_user_id=500, pg_username="plat", subscription_token=PLATFORM_TOKEN),
                UserService(id=2, bot_user_id=21, pg_user_id=501, pg_username="shop", shop_id=SHOP, subscription_token=SHOP_TOKEN),
                UserService(id=3, bot_user_id=22, pg_user_id=502, pg_username="other", shop_id=6, subscription_token=OTHER_TOKEN),
            ])
            await session.commit()

    async def find(self, payload: dict, link: str):
        cookie = f"session={self.signer.dumps(payload)}; csrf=token"
        headers = {"cookie": cookie, "x-csrf-token": "token", "origin": "http://panel.test"}
        return await self.client.post(LINK_SEARCH_PATH, headers=headers, data={"link": link})

    def target(self, response) -> dict[str, list[str]]:
        self.assertEqual(response.status_code, 303)
        location = response.headers["location"]
        self.assertTrue(location.startswith("/users?"), location)
        return parse_qs(urlsplit(location).query)

    async def test_the_owner_finds_the_platform_customer(self) -> None:
        params = self.target(await self.find(OWNER_SESSION, URL.format(PLATFORM_TOKEN)))
        self.assertEqual(params, {"q": ["1020"], "uid": ["20"]})

    async def test_a_shop_finds_its_own_customer_only(self) -> None:
        params = self.target(await self.find(SHOP_SESSION, URL.format(SHOP_TOKEN)))
        self.assertEqual(params, {"q": ["1021"], "uid": ["21"]})
        for token in (OTHER_TOKEN, PLATFORM_TOKEN):
            with self.subTest(token=token):
                params = self.target(await self.find(SHOP_SESSION, URL.format(token)))
                self.assertEqual(params, {"err": [NOT_FOUND_TEXT]})

    async def test_the_owner_does_not_reach_a_shops_customer(self) -> None:
        params = self.target(await self.find(OWNER_SESSION, URL.format(SHOP_TOKEN)))
        self.assertEqual(params, {"err": [NOT_FOUND_TEXT]})

    async def test_the_link_is_never_echoed_in_the_redirect(self) -> None:
        for payload, token in ((OWNER_SESSION, PLATFORM_TOKEN), (OWNER_SESSION, OTHER_TOKEN), (SHOP_SESSION, SHOP_TOKEN)):
            response = await self.find(payload, URL.format(token))
            self.assertNotIn(token, response.headers["location"])
            self.assertNotIn(token, response.text)

    async def test_junk_and_overlong_input_find_nothing(self) -> None:
        for junk in ("", "   ", "hello", "x" * 5000, "' OR 1=1 --", URL.format("short")):
            with self.subTest(junk=junk[:20]):
                params = self.target(await self.find(OWNER_SESSION, junk))
                self.assertEqual(params, {"err": [NOT_FOUND_TEXT]})

    async def test_staff_who_may_not_open_the_users_page_cannot_look_up(self) -> None:
        self.assertNotEqual((await self.call(PG_STAFF_SESSION, "GET", "/users")).status_code, 200)
        response = await self.find(PG_STAFF_SESSION, URL.format(PLATFORM_TOKEN))
        self.assertEqual(response.status_code, 303)
        self.assertNotIn("uid=", response.headers["location"])

    async def test_a_get_with_the_link_is_not_a_lookup(self) -> None:
        cookie = f"session={self.signer.dumps(OWNER_SESSION)}; csrf=token"
        response = await self.client.get(f"{LINK_SEARCH_PATH}?link={PLATFORM_TOKEN}", headers={"cookie": cookie})
        self.assertIn(response.status_code, (404, 405))

    async def test_the_users_search_posts_a_pasted_link(self) -> None:
        response = await self.call(OWNER_SESSION, "GET", "/users")
        self.assertEqual(response.status_code, 200)
        self.assertIn("data-users-search", response.text)
        self.assertIn(f'action="{LINK_SEARCH_PATH}"', response.text)
        self.assertIn("/static/users-link-search.js", response.text)

    async def test_a_link_typed_into_the_get_search_is_answered_without_echoing_it(self) -> None:
        link = URL.format(PLATFORM_TOKEN)
        response = await self.call(OWNER_SESSION, "GET", f"/users?{urlencode({'q': link})}")
        self.assertEqual(self.target(response), {"q": ["1020"], "uid": ["20"]})
        self.assertNotIn(PLATFORM_TOKEN, response.text)
        response = await self.call(SHOP_SESSION, "GET", f"/users?{urlencode({'q': URL.format(OTHER_TOKEN)})}")
        self.assertEqual(self.target(response), {"err": [NOT_FOUND_TEXT]})

    async def test_a_plain_search_still_lists_users(self) -> None:
        response = await self.call(OWNER_SESSION, "GET", "/users?q=Platform")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Platform customer", response.text)
