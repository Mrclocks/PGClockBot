"""Mini App onboarding: opening the Mini App before /start registers the person like /start, and
the referral code, required channels and entry terms are finished inside the Mini App.

Every step keeps the bot's rules: the same referral check, the same membership check through the
bot, the same acceptance record, per shop; staff are never gated and blocked people never pass.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import urlencode

import httpx
from fastapi import Depends, FastAPI, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.miniapp_onboarding import register_miniapp_onboarding
from app.db.models import Base, BotUser, BotUserShop, ResellerProfile, TermsAcceptance
from app.services import miniapp_onboarding
from app.services.miniapp_auth import (
    BAD_SIGNATURE,
    BLOCKED,
    ERROR_HEADER,
    FORCE_JOIN,
    REFERRAL_REQUIRED,
    TERMS,
    load_mini_user,
)
from app.services.miniapp_registration import referral_code_from_start_param, telegram_profile
from app.services.miniapp_shop import PLATFORM_SWITCH, SHOP_SWITCH, mini_scope, new_shop_key
from app.services.secret_box import seal_bot_token
from app.services.users import clear_settings_cache, set_setting
from tests.postgres_support import scoped_async_engine

MAIN_TOKEN = "111111:main-bot-token"
SHOP_TOKEN = "222222:shop-bot-token"
OTHER_SHOP_TOKEN = "333333:other-shop-token"
SHOP, OTHER_SHOP = 5, 6
KEY, OTHER_KEY = new_shop_key(), new_shop_key()
PERMS = '["orders","payments","miniapp"]'
PROBE = "/api/mini/__onboarding_probe"
NEWCOMER, REFERRER_TG, BLOCKED_TG, SHOP_OWNER_TG = 7001, 7002, 7003, 5005
REFERRER_CODE = "FRIEND42"
PUBLIC_CHANNEL = "@news_channel"
PRIVATE_CHANNEL = "-1001234567890"
PRIVATE_INVITE = "https://t.me/+AbCdEf123"
NO_LINK_CHANNEL = "-1009999999999"


def signed(token: str, telegram_id: int, **extra: str) -> str:
    """initData Telegram would sign, with optional user fields and ``start_param``."""
    user = {"id": telegram_id, **{k: v for k, v in extra.items() if k != "start_param"}}
    fields = {"auth_date": str(int(time.time())), "user": json.dumps(user, separators=(",", ":"))}
    if "start_param" in extra:
        fields["start_param"] = extra["start_param"]
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class StartParamTests(unittest.TestCase):
    def test_only_a_ref_start_parameter_carries_a_referral_code(self) -> None:
        self.assertEqual(referral_code_from_start_param("ref_friend42"), "FRIEND42")
        for value in ("", None, "services", "ref_", "ref_<script>", "xref_ABC", "ref_" + "A" * 40):
            with self.subTest(value=value):
                self.assertIsNone(referral_code_from_start_param(value))

    def test_the_profile_comes_from_the_signed_user_with_bounded_text(self) -> None:
        profile = telegram_profile({"id": 5, "first_name": "  Ali\n", "last_name": "R" * 300, "username": "u" * 90})
        self.assertEqual(profile.telegram_id, 5)
        self.assertEqual(len(profile.full_name or ""), 255)
        self.assertEqual(len(profile.username or ""), 64)
        for bad in ({"id": True}, {"id": "5"}, {"id": 0}, {"id": -3}, {}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                telegram_profile(bad)


class OnboardingFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        clear_settings_cache()
        miniapp_onboarding._JOIN_CHECKS._hits.clear()
        miniapp_onboarding._REFERRAL_TRIES._hits.clear()
        # A file database: concurrent requests each get their own connection, as in production.
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        now = datetime.now(timezone.utc)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=SHOP, telegram_id=SHOP_OWNER_TG, referral_code="OWNER5", role="reseller"),
                BotUser(id=OTHER_SHOP, telegram_id=6006, referral_code="OWNER6", role="reseller"),
                BotUser(id=20, telegram_id=REFERRER_TG, referral_code=REFERRER_CODE, role="user"),
                BotUser(id=21, telegram_id=BLOCKED_TG, referral_code="BLK", role="user", is_blocked=True),
                ResellerProfile(
                    id=1, user_id=SHOP, is_active=True, setup_completed_at=now, bot_token=seal_bot_token(SHOP_TOKEN),
                    miniapp_key=KEY, web_permissions=PERMS,
                ),
                ResellerProfile(
                    id=2, user_id=OTHER_SHOP, is_active=True, setup_completed_at=now,
                    bot_token=seal_bot_token(OTHER_SHOP_TOKEN), miniapp_key=OTHER_KEY, web_permissions=PERMS,
                ),
            ])
            await session.commit()
            await set_setting(session, PLATFORM_SWITCH, "1")
            for shop in (SHOP, OTHER_SHOP):
                await set_setting(session, SHOP_SWITCH, "1", reseller_id=shop)
        clear_settings_cache()
        settings = SimpleNamespace(miniapp_enabled=True, bot_token=MAIN_TOKEN, currency="تومان")
        self.membership = AsyncMock(return_value=([], []))
        self.clear_cache = Mock()
        self.patches = [
            patch("app.services.miniapp_auth.get_settings", return_value=settings),
            patch("app.bot.middlewares.check_force_join_all", self.membership),
            patch("app.bot.middlewares.clear_force_join_member_cache", self.clear_cache),
            patch("app.services.loyalty.on_user_referred", AsyncMock()),
        ]
        for p in self.patches:
            p.start()

        async def get_db():
            async with self.Session() as session:
                yield session

        app = FastAPI()

        @app.get(PROBE)
        async def probe(request: Request, session: AsyncSession = Depends(get_db)):
            user = await load_mini_user(session, request)
            return {"user": int(user.id), "shop": mini_scope(request).shop_id}

        register_miniapp_onboarding(app, get_db=get_db)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        self.probe_patch = patch("app.services.miniapp_shop.SHOP_ROUTES", frozenset({
            PROBE, "/api/mini/onboarding", "/api/mini/onboarding/referral",
            "/api/mini/onboarding/join-check", "/api/mini/onboarding/terms",
        }))
        self.probe_patch.start()

    async def asyncTearDown(self) -> None:
        self.probe_patch.stop()
        await self.client.aclose()
        for p in reversed(self.patches):
            p.stop()
        clear_settings_cache()
        await self.engine.dispose()

    def headers(self, init_data: str, key: str | None = None) -> dict[str, str]:
        headers = {"X-Telegram-Init-Data": init_data}
        if key:
            headers["X-Mini-Shop"] = key
        return headers

    async def get(self, path: str, init_data: str, *, key: str | None = None) -> httpx.Response:
        return await self.client.get(path, headers=self.headers(init_data, key))

    async def post(self, path: str, init_data: str, *, key: str | None = None, body: object = None) -> httpx.Response:
        return await self.client.post(path, headers=self.headers(init_data, key), json=body if body is not None else {})

    async def setting(self, key: str, value: str, *, shop: int | None = None) -> None:
        async with self.Session() as session:
            await set_setting(session, key, value, reseller_id=shop)
        clear_settings_cache()

    async def user(self, telegram_id: int) -> BotUser | None:
        async with self.Session() as session:
            return await session.scalar(select(BotUser).where(BotUser.telegram_id == telegram_id))

    async def count(self, model, *where) -> int:
        async with self.Session() as session:
            return int(await session.scalar(select(func.count()).select_from(model).where(*where)) or 0)

    def assert_refused(self, response: httpx.Response, status: int, code: str) -> None:
        self.assertEqual((response.status_code, response.headers.get(ERROR_HEADER)), (status, code), response.text)


class RegistrationTests(OnboardingFixture):
    async def test_opening_before_start_registers_the_person_from_signed_data(self) -> None:
        init = signed(MAIN_TOKEN, NEWCOMER, first_name="Sara", last_name="K", username="sara_k")
        response = await self.get(PROBE, init)
        self.assertEqual(response.status_code, 200, response.text)
        user = await self.user(NEWCOMER)
        self.assertEqual((user.full_name, user.username, user.role, user.reseller_id), ("Sara K", "sara_k", "user", None))
        self.assertEqual(response.json(), {"user": int(user.id), "shop": None})

    async def test_opening_again_or_at_once_keeps_one_account(self) -> None:
        init = signed(MAIN_TOKEN, NEWCOMER)
        responses = await asyncio.gather(*(self.get(PROBE, init) for _ in range(4)))
        responses.append(await self.get(PROBE, init))
        self.assertEqual({r.status_code for r in responses}, {200}, [r.text for r in responses])
        self.assertEqual(await self.count(BotUser, BotUser.telegram_id == NEWCOMER), 1)
        self.assertEqual(len({r.json()["user"] for r in responses}), 1)

    async def test_a_forged_or_foreign_signature_registers_nobody(self) -> None:
        for init in (signed(SHOP_TOKEN, NEWCOMER), signed(MAIN_TOKEN, NEWCOMER).replace("7001", "7009")):
            with self.subTest(init=init[:20]):
                self.assert_refused(await self.get(PROBE, init), 401, BAD_SIGNATURE)
                self.assert_refused(await self.get("/api/mini/onboarding", init), 401, BAD_SIGNATURE)
        self.assertIsNone(await self.user(NEWCOMER))
        self.assertIsNone(await self.user(7009))

    async def test_the_referral_link_registers_the_referrer(self) -> None:
        response = await self.get(PROBE, signed(MAIN_TOKEN, NEWCOMER, start_param=f"ref_{REFERRER_CODE.lower()}"))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((await self.user(NEWCOMER)).referred_by_id, 20)

    async def test_a_blocked_person_is_refused_with_its_own_reason(self) -> None:
        self.assert_refused(await self.get(PROBE, signed(MAIN_TOKEN, BLOCKED_TG)), 403, BLOCKED)
        self.assert_refused(await self.get("/api/mini/onboarding", signed(MAIN_TOKEN, BLOCKED_TG)), 403, BLOCKED)
        self.assert_refused(await self.post("/api/mini/onboarding/terms", signed(MAIN_TOKEN, BLOCKED_TG)), 403, BLOCKED)

    async def test_a_shop_mini_app_registers_its_newcomer_in_that_shop(self) -> None:
        response = await self.get(PROBE, signed(SHOP_TOKEN, NEWCOMER), key=KEY)
        self.assertEqual(response.status_code, 200, response.text)
        user = await self.user(NEWCOMER)
        self.assertEqual(user.reseller_id, SHOP)
        self.assertEqual(response.json(), {"user": int(user.id), "shop": SHOP})
        # The same person in a second shop joins it as well, and keeps their first shop.
        response = await self.get(PROBE, signed(OTHER_SHOP_TOKEN, NEWCOMER), key=OTHER_KEY)
        self.assertEqual(response.json()["shop"], OTHER_SHOP)
        self.assertEqual((await self.user(NEWCOMER)).reseller_id, SHOP)
        self.assertEqual(await self.count(BotUserShop, BotUserShop.bot_user_id == user.id, BotUserShop.shop_id == OTHER_SHOP), 1)


    async def test_a_route_closed_to_shops_registers_nobody(self) -> None:
        with patch("app.services.miniapp_shop.SHOP_ROUTES", frozenset()):
            response = await self.get(PROBE, signed(SHOP_TOKEN, NEWCOMER), key=KEY)
        self.assertEqual(response.status_code, 404)
        self.assertIsNone(await self.user(NEWCOMER))


class ReferralRequiredTests(OnboardingFixture):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.setting("referral_required", "1")
        await self.setting("referral_required_text", "<b>کد معرف</b> لازم است <script>x</script>")

    async def test_without_a_code_the_page_gets_the_referral_step(self) -> None:
        init = signed(MAIN_TOKEN, NEWCOMER)
        self.assert_refused(await self.get(PROBE, init), 403, REFERRAL_REQUIRED)
        state = (await self.get("/api/mini/onboarding", init)).json()
        self.assertEqual(state["step"], "referral")
        self.assertNotIn("<", state["text"], "the admin's text reaches the page as plain text")
        self.assertIn("کد معرف", state["text"])
        self.assertIsNone(await self.user(NEWCOMER))
        self.assert_refused(await self.post("/api/mini/onboarding/terms", init), 403, REFERRAL_REQUIRED)

    async def test_a_wrong_code_registers_nobody_and_a_right_one_registers_once(self) -> None:
        init = signed(MAIN_TOKEN, NEWCOMER)
        for body in ({"code": "NOPE99"}, {"code": ""}, {"code": 42}, {"code": "x" * 65}, {"code": "<b>"}, []):
            with self.subTest(body=body):
                response = await self.post("/api/mini/onboarding/referral", init, body=body)
                self.assertEqual(response.status_code, 400, response.text)
        self.assertIsNone(await self.user(NEWCOMER))
        miniapp_onboarding._REFERRAL_TRIES._hits.clear()
        for _ in range(2):
            response = await self.post("/api/mini/onboarding/referral", init, body={"code": REFERRER_CODE})
            self.assertEqual(response.json(), {"step": "ready"})
        self.assertEqual((await self.user(NEWCOMER)).referred_by_id, 20)
        self.assertEqual(await self.count(BotUser, BotUser.telegram_id == NEWCOMER), 1)

    async def test_guessing_codes_is_slowed_down_per_person(self) -> None:
        init = signed(MAIN_TOKEN, NEWCOMER)
        statuses = [
            (await self.post("/api/mini/onboarding/referral", init, body={"code": f"GUESS{i}"})).status_code
            for i in range(6)
        ]
        self.assertEqual(statuses, [400] * 5 + [429])
        # Another person is not slowed down by the first one's guesses.
        response = await self.post("/api/mini/onboarding/referral", signed(MAIN_TOKEN, 7010), body={"code": REFERRER_CODE})
        self.assertEqual(response.status_code, 200)

    async def test_a_platform_code_does_not_open_a_shop_that_requires_its_own(self) -> None:
        await self.setting("referral_required", "1", shop=SHOP)
        init = signed(SHOP_TOKEN, NEWCOMER)
        response = await self.post("/api/mini/onboarding/referral", init, key=KEY, body={"code": REFERRER_CODE})
        self.assertEqual(response.status_code, 400, "the referrer is not this shop's customer")
        response = await self.post("/api/mini/onboarding/referral", init, key=KEY, body={"code": "OWNER5"})
        self.assertEqual(response.json(), {"step": "ready"})
        self.assertEqual((await self.user(NEWCOMER)).reseller_id, SHOP)


class ForceJoinTests(OnboardingFixture):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        channels = [
            {"id": PUBLIC_CHANNEL, "required": True},
            {"id": PRIVATE_CHANNEL, "link": PRIVATE_INVITE, "title": "کانال <i>خصوصی</i>", "required": True},
            {"id": NO_LINK_CHANNEL, "required": True},
            {"id": "@optional_one", "required": False},
        ]
        await self.setting("force_join_enabled", "1")
        await self.setting("force_join_channel", json.dumps(channels))
        self.init = signed(MAIN_TOKEN, NEWCOMER)

    async def test_the_page_lists_the_channels_to_join_without_private_ids(self) -> None:
        self.membership.return_value = ([PUBLIC_CHANNEL, PRIVATE_CHANNEL], [NO_LINK_CHANNEL])
        self.assert_refused(await self.get(PROBE, self.init), 403, FORCE_JOIN)
        state = (await self.get("/api/mini/onboarding", self.init)).json()
        self.assertEqual(state["step"], "force_join")
        self.assertEqual(
            [(c["url"], c["status"]) for c in state["channels"]],
            [("https://t.me/news_channel", "missing"), (PRIVATE_INVITE, "missing"), ("", "unverified")],
        )
        self.assertEqual(state["channels"][1]["label"], "کانال <i>خصوصی</i>", "escaped by the page")
        self.assertNotIn("-100", json.dumps(state), "private chat ids stay on the server")
        self.assertEqual(self.membership.await_args.args[1], NEWCOMER)

    async def test_joined_asks_telegram_afresh_then_lets_the_person_in(self) -> None:
        self.membership.return_value = ([PUBLIC_CHANNEL], [])
        self.assertEqual((await self.post("/api/mini/onboarding/join-check", self.init)).json()["step"], "force_join")
        self.clear_cache.assert_called_with(NEWCOMER)
        self.membership.return_value = ([], [])
        miniapp_onboarding._JOIN_CHECKS._hits.clear()
        self.assertEqual((await self.post("/api/mini/onboarding/join-check", self.init)).json(), {"step": "ready"})
        self.assertEqual((await self.get(PROBE, self.init)).status_code, 200)

    async def test_joined_cannot_be_hammered(self) -> None:
        self.membership.return_value = ([PUBLIC_CHANNEL], [])
        first = await self.post("/api/mini/onboarding/join-check", self.init)
        second = await self.post("/api/mini/onboarding/join-check", self.init)
        self.assertEqual((first.status_code, second.status_code), (200, 429))
        self.assertEqual(self.membership.await_count, 1)

    async def test_only_unverifiable_channels_say_so(self) -> None:
        self.membership.return_value = ([], [PUBLIC_CHANNEL])
        state = (await self.get("/api/mini/onboarding", self.init)).json()
        self.assertEqual(state["text"], miniapp_onboarding.FORCE_JOIN_UNVERIFIED_TEXT)

    async def test_staff_are_never_gated(self) -> None:
        self.membership.return_value = ([PUBLIC_CHANNEL], [])
        await self.setting("force_join_enabled", "1", shop=SHOP)
        await self.setting("force_join_channel", json.dumps([{"id": PUBLIC_CHANNEL}]), shop=SHOP)
        response = await self.get(PROBE, signed(SHOP_TOKEN, SHOP_OWNER_TG), key=KEY)
        self.assertEqual(response.json(), {"user": SHOP, "shop": SHOP})
        self.assertEqual((await self.get("/api/mini/onboarding", signed(SHOP_TOKEN, SHOP_OWNER_TG), key=KEY)).json(),
                         {"step": "ready"})

    async def test_a_shop_checks_its_own_channels_through_its_own_bot(self) -> None:
        response = await self.get(PROBE, signed(SHOP_TOKEN, NEWCOMER), key=KEY)
        self.assertEqual(response.status_code, 200, "the platform's channels do not gate a shop")
        await self.setting("force_join_enabled", "1", shop=SHOP)
        await self.setting("force_join_channel", json.dumps([{"id": "@shop_news"}]), shop=SHOP)
        self.membership.return_value = (["@shop_news"], [])
        state = (await self.get("/api/mini/onboarding", signed(SHOP_TOKEN, NEWCOMER), key=KEY)).json()
        self.assertEqual([c["url"] for c in state["channels"]], ["https://t.me/shop_news"])
        self.assertEqual(self.membership.await_args.args[0].token, SHOP_TOKEN)


class TermsTests(OnboardingFixture):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.setting("terms_entry_enabled", "1")
        await self.setting("terms_entry_text", "قوانین <b>پلتفرم</b><script>alert(1)</script>")
        await self.setting("terms_entry_btn", "می‌پذیرم")
        self.init = signed(MAIN_TOKEN, NEWCOMER)

    async def test_terms_are_shown_then_accepted_once(self) -> None:
        self.assert_refused(await self.get(PROBE, self.init), 403, TERMS)
        state = (await self.get("/api/mini/onboarding", self.init)).json()
        self.assertEqual(state["step"], "terms")
        self.assertEqual(state["button"], "می‌پذیرم")
        self.assertNotIn("<", state["text"])
        responses = await asyncio.gather(*(self.post("/api/mini/onboarding/terms", self.init) for _ in range(3)))
        self.assertEqual({json.dumps(r.json()) for r in responses}, {json.dumps({"step": "ready"})})
        user = await self.user(NEWCOMER)
        self.assertEqual(await self.count(TermsAcceptance, TermsAcceptance.bot_user_id == user.id), 1)
        self.assertEqual((await self.get(PROBE, self.init)).status_code, 200)

    async def test_channels_come_before_terms(self) -> None:
        await self.setting("force_join_enabled", "1")
        await self.setting("force_join_channel", json.dumps([{"id": PUBLIC_CHANNEL}]))
        self.membership.return_value = ([PUBLIC_CHANNEL], [])
        self.assertEqual((await self.get("/api/mini/onboarding", self.init)).json()["step"], "force_join")

    async def test_each_shop_keeps_its_own_acceptance(self) -> None:
        await self.post("/api/mini/onboarding/terms", self.init)
        await self.setting("terms_entry_enabled", "1", shop=SHOP)
        await self.setting("terms_entry_text", "قوانین فروشگاه", shop=SHOP)
        shop_init = signed(SHOP_TOKEN, NEWCOMER)
        self.assert_refused(await self.get(PROBE, shop_init, key=KEY), 403, TERMS)
        self.assertEqual((await self.post("/api/mini/onboarding/terms", shop_init, key=KEY)).json(), {"step": "ready"})
        user = await self.user(NEWCOMER)
        async with self.Session() as session:
            shops = set(await session.scalars(select(TermsAcceptance.shop_owner_id).where(TermsAcceptance.bot_user_id == user.id)))
        self.assertEqual(shops, {0, SHOP})
        # A second shop's terms are still unaccepted.
        await self.setting("terms_entry_enabled", "1", shop=OTHER_SHOP)
        await self.setting("terms_entry_text", "قوانین فروشگاه دوم", shop=OTHER_SHOP)
        self.assert_refused(await self.get(PROBE, signed(OTHER_SHOP_TOKEN, NEWCOMER), key=OTHER_KEY), 403, TERMS)

    async def test_accepting_while_terms_are_off_records_nothing(self) -> None:
        await self.setting("terms_entry_enabled", "0")
        self.assertEqual((await self.post("/api/mini/onboarding/terms", self.init)).json(), {"step": "ready"})
        self.assertEqual(await self.count(TermsAcceptance), 0)


if __name__ == "__main__":
    unittest.main()
