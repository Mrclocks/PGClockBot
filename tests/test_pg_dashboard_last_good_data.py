"""During a PasarGuard outage, dashboard widgets show the last good answer, labelled.

Saved answers are per identity, at most 15 minutes old, cleared by the
client's own mutations, offered only to web pages that can show the notice,
and never used for reads that decide access or money.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from jinja2 import Environment, FileSystemLoader

from app.api.panel_connection import PanelConnectionMiddleware
from app.services import pg_read_cache
from app.services.pasarguard import PasarGuardClient

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "web" / "templates"


class _Clock:
    def __init__(self) -> None:
        self.now = 9000.0

    def monotonic(self) -> float:
        return self.now


class LastGoodReadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        pg_read_cache._STORE.clear()
        self.clock = _Clock()
        self.enterContext(patch.object(pg_read_cache, "time", SimpleNamespace(monotonic=self.clock.monotonic)))
        self.online = True
        self.status = 200

    async def asyncTearDown(self) -> None:
        pg_read_cache._STORE.clear()

    def _answer(self, request: httpx.Request) -> httpx.Response:
        if not self.online:
            raise httpx.ConnectError("offline")
        return httpx.Response(self.status, json={"hosts": [{"id": 1}, {"id": 2}]})

    async def _client(self, username: str = "owner") -> PasarGuardClient:
        client = PasarGuardClient(username=username, access_token="token")
        await client.close()
        client.base_url = "https://panel.example"
        client._api_base_resolved = True
        client._validated_base, client._validated_until = client.base_url, float("inf")
        client._client = httpx.AsyncClient(base_url=client.base_url, transport=httpx.MockTransport(self._answer))
        self.addAsyncCleanup(client.close)
        return client

    async def _saved_then_offline(self, client: PasarGuardClient, *, after: float = 60) -> None:
        await client.get_hosts()
        self.online = False
        self.clock.now += after

    async def test_page_shows_saved_answer_and_its_age(self) -> None:
        client = await self._client()
        await self._saved_then_offline(client)
        notice = pg_read_cache.begin_last_good_notice()
        hosts = await client.get_hosts(last_good_ok=True)
        self.assertEqual(len(hosts), 2)
        self.assertAlmostEqual(notice.age, 60)
        self.assertEqual(notice.minutes, 1)

    async def test_callers_without_a_notice_keep_the_error(self) -> None:
        client = await self._client()
        await self._saved_then_offline(client)
        pg_read_cache._NOTICE.set(None)
        with self.assertRaises(httpx.ConnectError):
            await client.get_hosts(last_good_ok=True)

    async def test_reads_that_did_not_opt_in_keep_the_error(self) -> None:
        client = await self._client()
        await self._saved_then_offline(client)
        pg_read_cache.begin_last_good_notice()
        with self.assertRaises(httpx.ConnectError):
            await client.get_hosts()

    async def test_saved_answer_expires(self) -> None:
        client = await self._client()
        await self._saved_then_offline(client, after=pg_read_cache.LAST_GOOD_MAX_AGE_SEC + 1)
        pg_read_cache.begin_last_good_notice()
        with self.assertRaises(httpx.ConnectError):
            await client.get_hosts(last_good_ok=True)

    async def test_definite_panel_errors_are_not_hidden(self) -> None:
        client = await self._client()
        await client.get_hosts()
        self.clock.now += 60
        self.status = 403
        pg_read_cache.begin_last_good_notice()
        with self.assertRaises(Exception) as caught:
            await client.get_hosts(last_good_ok=True)
        self.assertEqual(getattr(caught.exception, "status_code", None), 403)

    async def test_gateway_errors_use_the_saved_answer(self) -> None:
        client = await self._client()
        await client.get_hosts()
        self.clock.now += 60
        self.status = 502
        notice = pg_read_cache.begin_last_good_notice()
        self.assertEqual(len(await client.get_hosts(last_good_ok=True)), 2)
        self.assertIsNotNone(notice.age)

    async def test_saved_answers_never_cross_identities(self) -> None:
        owner = await self._client("owner")
        shop = await self._client("shop1")
        await self._saved_then_offline(owner)
        pg_read_cache.begin_last_good_notice()
        with self.assertRaises(httpx.ConnectError):
            await shop.get_hosts(last_good_ok=True)

    async def test_own_mutation_drops_saved_answers(self) -> None:
        client = await self._client()
        await client.get_hosts()
        await client.request("POST", "/api/host", json={"remark": "new"})
        self.online = False
        pg_read_cache.begin_last_good_notice()
        with self.assertRaises(httpx.ConnectError):
            await client.get_hosts(last_good_ok=True)


class ConnectionNoticeTemplateTests(unittest.TestCase):
    def _render(self, *, last_good_age: float | None) -> str:
        env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True)
        notice = pg_read_cache.LastGoodNotice(age=last_good_age)
        request = SimpleNamespace(
            state=SimpleNamespace(pg_read_budget=SimpleNamespace(failure="timeout"), pg_last_good=notice),
            url=SimpleNamespace(path="/pg/body"),
        )
        return env.get_template("_pg_connection_notice.html").render(request=request)

    def test_saved_data_is_announced_with_its_age(self) -> None:
        html = self._render(last_good_age=180)
        self.assertIn("آخرین اطلاعات دریافت‌شده نمایش داده می‌شود", html)
        self.assertIn(">3</bdi> دقیقه پیش", html)
        self.assertIn('href="/pg"', html)

    def test_plain_outage_keeps_the_existing_message(self) -> None:
        html = self._render(last_good_age=None)
        self.assertIn("اطلاعات پاسارگارد در دسترس نیست", html)
        self.assertNotIn("آخرین اطلاعات دریافت‌شده", html)


class MiddlewareNoticeTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_page_gets_a_fresh_notice(self) -> None:
        seen = []

        async def app(scope, receive, send) -> None:
            pg_read_cache.note_last_good_read(30)
            seen.append(scope["state"]["pg_last_good"])

        middleware = PanelConnectionMiddleware(app)
        await middleware({"type": "http", "path": "/pg/body"}, None, None)
        await middleware({"type": "http", "path": "/pg/body"}, None, None)
        self.assertIsNot(seen[0], seen[1])
        self.assertEqual([notice.age for notice in seen], [30, 30])

    async def test_api_requests_get_no_notice(self) -> None:
        seen = {}

        async def app(scope, receive, send) -> None:
            seen["state"] = scope.get("state", {})

        await PanelConnectionMiddleware(app)({"type": "http", "path": "/api/mini/services"}, None, None)
        self.assertNotIn("pg_last_good", seen["state"])


if __name__ == "__main__":
    unittest.main()
