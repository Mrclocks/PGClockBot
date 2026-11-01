"""Mini App sign-in: how long Telegram's initData is accepted, and the reason sent with a refusal.

Telegram signs initData once, when the Mini App opens, so a window left open must keep
working for hours; stale, future-dated and forged data is still refused, and every refusal
names its reason in ``X-Mini-Error`` with Persian text for the page.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Base, BotUser
from app.services.miniapp_auth import (
    BAD_SIGNATURE,
    ERROR_HEADER,
    EXPIRED,
    INIT_DATA_MAX_AGE_SEC,
    INIT_DATA_MAX_FUTURE_SKEW_SEC,
    NO_INIT_DATA,
    NOT_STARTED,
    load_mini_user,
    validate_webapp_init_data,
)
from app.services.users import clear_settings_cache

TOKEN = "123456:MAIN"
OTHER_BOT_TOKEN = "654321:SHOP"
CUSTOMER = 9001
STRANGER = 9002
PROBE = "/api/mini/probe"


def signed(token: str, telegram_id: int, *, age: int = 0) -> str:
    fields = {"auth_date": str(int(time.time()) - age), "user": json.dumps({"id": telegram_id}, separators=(",", ":"))}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class InitDataWindowTests(unittest.TestCase):
    def refusal(self, init_data: str) -> HTTPException:
        with self.assertRaises(HTTPException) as ctx:
            validate_webapp_init_data(init_data, bot_token=TOKEN)
        return ctx.exception

    def test_a_window_left_open_for_hours_still_signs_in(self) -> None:
        for age in (11 * 60, 3 * 3600, INIT_DATA_MAX_AGE_SEC - 60):
            with self.subTest(age=age):
                self.assertEqual(validate_webapp_init_data(signed(TOKEN, CUSTOMER, age=age), bot_token=TOKEN)["id"], CUSTOMER)

    def test_initdata_older_than_the_window_is_refused_as_expired(self) -> None:
        exc = self.refusal(signed(TOKEN, CUSTOMER, age=INIT_DATA_MAX_AGE_SEC + 60))
        self.assertEqual((exc.status_code, exc.headers[ERROR_HEADER]), (401, EXPIRED))

    def test_a_server_clock_slightly_behind_telegram_is_tolerated(self) -> None:
        self.assertEqual(validate_webapp_init_data(signed(TOKEN, CUSTOMER, age=-300), bot_token=TOKEN)["id"], CUSTOMER)

    def test_initdata_dated_far_ahead_is_refused(self) -> None:
        exc = self.refusal(signed(TOKEN, CUSTOMER, age=-(INIT_DATA_MAX_FUTURE_SKEW_SEC + 60)))
        self.assertEqual((exc.status_code, exc.headers[ERROR_HEADER]), (401, EXPIRED))

    def test_forged_or_foreign_signatures_are_refused_whatever_their_age(self) -> None:
        cases = {
            "another bot": signed(OTHER_BOT_TOKEN, CUSTOMER),
            "tampered": signed(TOKEN, CUSTOMER).replace("9001", "9002"),
            "no hash": urlencode({"auth_date": str(int(time.time())), "user": '{"id":1}'}),
            "no date": signed(TOKEN, CUSTOMER).replace("auth_date", "x_date"),
        }
        for name, init_data in cases.items():
            with self.subTest(name):
                exc = self.refusal(init_data)
                self.assertEqual((exc.status_code, exc.headers[ERROR_HEADER]), (401, BAD_SIGNATURE))

    def test_a_refusal_never_echoes_the_initdata(self) -> None:
        init_data = signed(OTHER_BOT_TOKEN, CUSTOMER)
        exc = self.refusal(init_data)
        self.assertNotIn(init_data.split("hash=")[-1], str(exc.detail) + json.dumps(exc.headers))


class LoadMiniUserRefusalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        clear_settings_cache()
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with Session() as session:
            session.add(BotUser(id=1, telegram_id=CUSTOMER, referral_code="c1", role="user"))
            await session.commit()
        settings = SimpleNamespace(miniapp_enabled=True, bot_token=TOKEN, currency="تومان")
        self.settings_patch = patch("app.services.miniapp_auth.get_settings", return_value=settings)
        self.settings_patch.start()

        async def get_db():
            async with Session() as session:
                yield session

        app = FastAPI()

        @app.get(PROBE)
        async def probe(request: Request, session: AsyncSession = Depends(get_db)):
            return {"user": int((await load_mini_user(session, request)).id)}

        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.settings_patch.stop()
        clear_settings_cache()
        await self.engine.dispose()

    async def call(self, init_data: str | None) -> httpx.Response:
        headers = {} if init_data is None else {"X-Telegram-Init-Data": init_data}
        return await self.client.get(PROBE, headers=headers)

    def assert_refused(self, response: httpx.Response, status: int, code: str) -> None:
        self.assertEqual((response.status_code, response.headers.get(ERROR_HEADER)), (status, code))
        detail = response.json()["detail"]
        # Persian text the page can show as is (it hides ASCII-only server text).
        self.assertTrue(any(ord(c) > 127 for c in detail), detail)

    async def test_an_hour_old_session_still_works(self) -> None:
        response = await self.call(signed(TOKEN, CUSTOMER, age=3600))
        self.assertEqual(response.json(), {"user": 1})

    async def test_each_refusal_names_its_reason(self) -> None:
        self.assert_refused(await self.call(None), 401, NO_INIT_DATA)
        self.assert_refused(await self.call(signed(OTHER_BOT_TOKEN, CUSTOMER)), 401, BAD_SIGNATURE)
        self.assert_refused(await self.call(signed(TOKEN, CUSTOMER, age=INIT_DATA_MAX_AGE_SEC + 60)), 401, EXPIRED)
        self.assert_refused(await self.call(signed(TOKEN, STRANGER)), 404, NOT_STARTED)


if __name__ == "__main__":
    unittest.main()
