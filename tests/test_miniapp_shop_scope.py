"""A shop's Mini App: found by its key, verified with its own bot, its customers only, closed by default."""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

import httpx
import sqlalchemy as sa
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.miniapp_pages import register_miniapp_pages
from app.db.models import Base, BotUser, BotUserShop, ResellerProfile
from app.services import miniapp_shop
from app.services.miniapp_auth import INIT_DATA_MAX_AGE_SEC, load_mini_user
from app.services.miniapp_shop import (
    NOT_A_CUSTOMER,
    PLATFORM_SWITCH,
    ROUTE_UNAVAILABLE,
    SHOP_SWITCH,
    SHOP_UNAVAILABLE,
    mini_scope,
    new_shop_key,
)
from app.services.secret_box import seal_bot_token
from app.services.users import clear_settings_cache, set_setting

ROOT = Path(__file__).resolve().parents[1]
MAIN_TOKEN = "111111:main-bot-token"
SHOP_TOKEN = "222222:shop-bot-token"
OTHER_SHOP_TOKEN = "333333:other-shop-token"
SHOP, OTHER_SHOP = 5, 6
KEY, OTHER_KEY = new_shop_key(), new_shop_key()
PROBE = "/api/mini/__scope_probe"
PERMS = '["orders","payments","miniapp"]'
# telegram ids
CUSTOMER, PLATFORM_CUSTOMER, OTHER_CUSTOMER, MEMBER, BOT_ADMIN, OWNER = 1010, 1011, 1012, 1013, 2000, 5005


def signed(token: str, telegram_id: int, *, age: int = 0) -> str:
    fields = {"auth_date": str(int(time.time()) - age), "user": json.dumps({"id": telegram_id}, separators=(",", ":"))}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class ShopScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        clear_settings_cache()
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        now = datetime.now(timezone.utc)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=SHOP, telegram_id=OWNER, referral_code="o5", role="reseller"),
                BotUser(id=OTHER_SHOP, telegram_id=6006, referral_code="o6", role="reseller"),
                BotUser(id=10, telegram_id=CUSTOMER, referral_code="c10", role="user", reseller_id=SHOP),
                BotUser(id=11, telegram_id=PLATFORM_CUSTOMER, referral_code="c11", role="user"),
                BotUser(id=12, telegram_id=OTHER_CUSTOMER, referral_code="c12", role="user", reseller_id=OTHER_SHOP),
                BotUser(id=13, telegram_id=MEMBER, referral_code="c13", role="user"),
                BotUser(id=14, telegram_id=BOT_ADMIN, referral_code="a14", role="user"),
                BotUserShop(bot_user_id=13, shop_id=SHOP),
                ResellerProfile(
                    id=1, user_id=SHOP, is_active=True, setup_completed_at=now, bot_token=seal_bot_token(SHOP_TOKEN),
                    miniapp_key=KEY, web_permissions=PERMS, bot_admin_ids=str(BOT_ADMIN),
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
        self.patches = [patch("app.services.miniapp_auth.get_settings", return_value=settings)]
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

        register_miniapp_pages(app, render=lambda *_a, **_k: HTMLResponse("page"), get_db=get_db)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        for p in reversed(self.patches):
            p.stop()
        clear_settings_cache()
        await self.engine.dispose()

    async def call(self, init_data: str, *, key: str | None = None, path: str = PROBE) -> httpx.Response:
        headers = {"X-Telegram-Init-Data": init_data}
        if key is not None:
            headers["X-Mini-Shop"] = key
        return await self.client.get(path, headers=headers)

    def open_probe(self):
        return patch.object(miniapp_shop, "SHOP_ROUTES", frozenset({PROBE}))

    async def test_platform_requests_are_unchanged(self) -> None:
        response = await self.call(signed(MAIN_TOKEN, PLATFORM_CUSTOMER))
        self.assertEqual(response.json(), {"user": 11, "shop": None})

    async def test_a_shop_customer_is_verified_with_the_shop_bot(self) -> None:
        with self.open_probe():
            for telegram_id, user_id in ((CUSTOMER, 10), (MEMBER, 13), (OWNER, SHOP), (BOT_ADMIN, 14)):
                with self.subTest(telegram_id=telegram_id):
                    response = await self.call(signed(SHOP_TOKEN, telegram_id), key=KEY)
                    self.assertEqual(response.json(), {"user": user_id, "shop": SHOP})

    async def test_shop_routes_stay_closed_until_opened(self) -> None:
        for path in (PROBE, "/api/mini/service/1/automation", "/api/mini/service/1/cancellation"):
            with self.subTest(path=path):
                response = await self.call(signed(SHOP_TOKEN, CUSTOMER), key=KEY, path=path)
                self.assertEqual((response.status_code, response.json()["detail"]), (404, ROUTE_UNAVAILABLE))

    async def test_a_signature_from_another_bot_is_refused(self) -> None:
        with self.open_probe():
            cases = (
                (signed(MAIN_TOKEN, CUSTOMER), KEY),
                (signed(OTHER_SHOP_TOKEN, CUSTOMER), KEY),
                (signed(SHOP_TOKEN, CUSTOMER), OTHER_KEY),
                (signed(SHOP_TOKEN, CUSTOMER), None),
                (signed(SHOP_TOKEN, CUSTOMER, age=INIT_DATA_MAX_AGE_SEC + 60), KEY),
            )
            for init_data, key in cases:
                with self.subTest(key=key):
                    self.assertEqual((await self.call(init_data, key=key)).status_code, 401)

    async def test_only_the_shops_own_customers_get_in(self) -> None:
        with self.open_probe():
            for telegram_id in (PLATFORM_CUSTOMER, OTHER_CUSTOMER):
                with self.subTest(telegram_id=telegram_id):
                    response = await self.call(signed(SHOP_TOKEN, telegram_id), key=KEY)
                    self.assertEqual((response.status_code, response.json()["detail"]), (403, NOT_A_CUSTOMER))

    async def test_every_switch_and_bad_key_closes_the_shop_the_same_way(self) -> None:
        async def turn_off(session):
            await set_setting(session, PLATFORM_SWITCH, "0")

        async def shop_off(session):
            await set_setting(session, SHOP_SWITCH, "0", reseller_id=SHOP)

        async def no_permission(session):
            (await session.get(ResellerProfile, 1)).web_permissions = '["orders","payments"]'

        async def inactive(session):
            (await session.get(ResellerProfile, 1)).is_active = False

        async def no_bot(session):
            (await session.get(ResellerProfile, 1)).bot_token = None

        with self.open_probe():
            for name, key, change in (
                ("unknown key", new_shop_key(), None), ("malformed key", "short", None),
                ("injected key", KEY + "'--", None), ("platform switch", KEY, turn_off),
                ("shop switch", KEY, shop_off), ("permission", KEY, no_permission),
                ("inactive", KEY, inactive), ("no bot", KEY, no_bot),
            ):
                with self.subTest(name):
                    if change is not None:
                        await self.asyncSetUp_reset(change)
                    response = await self.call(signed(SHOP_TOKEN, CUSTOMER), key=key)
                    self.assertEqual((response.status_code, response.json()["detail"]), (404, SHOP_UNAVAILABLE))
                    page = await self.client.get(f"/miniapp/s/{key}/")
                    self.assertEqual(page.status_code, 404)

    async def asyncSetUp_reset(self, change) -> None:
        await self.asyncTearDown()
        await self.asyncSetUp()
        async with self.Session() as session:
            await change(session)
            await session.commit()
        clear_settings_cache()

    async def test_the_shop_page_opens_only_while_the_shop_is_on(self) -> None:
        self.assertEqual((await self.client.get(f"/miniapp/s/{KEY}/")).status_code, 200)
        self.assertEqual((await self.client.get(f"/miniapp/s/{KEY}")).status_code, 200)


class ShopKeyMigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade_keep_rows(self) -> None:
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        spec = importlib.util.spec_from_file_location("m0070", ROOT / "alembic/versions/0070_shop_miniapp_key.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE reseller_profiles (id INTEGER PRIMARY KEY, user_id INTEGER)"))
            conn.execute(text("INSERT INTO reseller_profiles VALUES (1, 5), (2, 6)"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                inspector = sa.inspect(conn)
                self.assertIn("miniapp_key", {c["name"] for c in inspector.get_columns("reseller_profiles")})
                unique = {i["name"]: i["unique"] for i in inspector.get_indexes("reseller_profiles")}
                self.assertTrue(unique["ix_reseller_profiles_miniapp_key"])
                migration.downgrade()
                migration.downgrade()
                self.assertNotIn("miniapp_key", {c["name"] for c in sa.inspect(conn).get_columns("reseller_profiles")})
                self.assertEqual(conn.execute(text("SELECT COUNT(*) FROM reseller_profiles")).scalar(), 2)
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
