"""Background jobs read many services with few panel calls, and never worse than one call each."""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from unittest.mock import patch

import httpx

from app.config import Settings
from app.services import pg_read_resilience as resilience
from app.services.panel_user_batch import ID_CHUNK, BatchStats, panel_infos
from app.services.pasarguard import PasarGuardClient, PasarGuardError


@dataclass
class Service:
    pg_user_id: object
    pg_username: str | None
    subscription_token: str | None


def services(n: int, start: int = 1) -> list[Service]:
    return [Service(i, f"user{i}", f"tok{i}") for i in range(start, start + n)]


class FakePanel:
    """Panel users by id; ``ignore_filter`` answers like a panel without the ids filter."""

    def __init__(self, users: dict[int, str], *, ignore_filter: bool = False, failing_chunks: int = 0):
        self.users = users
        self.ignore_filter = ignore_filter
        self.failing_chunks = failing_chunks
        self.list_calls: list[list[int]] = []
        self.single_calls: list[str] = []

    def row(self, pg_id: int) -> dict:
        return {"id": pg_id, "username": self.users[pg_id], "used_traffic": pg_id, "data_limit": 100}

    async def list_users_by_ids(self, ids: list[int]) -> dict:
        self.list_calls.append(list(ids))
        if len(self.list_calls) <= self.failing_chunks:
            raise PasarGuardError("GET /api/users failed (502)", 502, "")
        if self.ignore_filter:
            return {"users": [self.row(i) for i in sorted(self.users)[: len(ids)]]}
        return {"users": [self.row(i) for i in ids if i in self.users], "total": len(ids)}

    async def subscription_info(self, token: str) -> dict:
        self.single_calls.append(token)
        pg_id = int(token.removeprefix("tok"))
        return {"username": self.users.get(pg_id, "?"), "used_traffic": pg_id, "source": "subscription"}


class PanelInfosTests(unittest.IsolatedAsyncioTestCase):
    async def test_many_services_take_one_list_call_per_chunk_and_no_single_lookups(self) -> None:
        rows = services(ID_CHUNK * 2 + 5)
        panel = FakePanel({s.pg_user_id: s.pg_username for s in rows})
        stats = BatchStats()
        infos = await panel_infos(panel, rows, stats=stats)
        self.assertEqual((stats.list_calls, stats.single_calls), (3, 0))
        self.assertEqual([info["id"] for info in infos], [s.pg_user_id for s in rows])
        self.assertEqual(panel.single_calls, [])

    async def test_a_row_of_another_user_is_never_used(self) -> None:
        rows = services(2)
        panel = FakePanel({1: "user1", 2: "renamed-elsewhere"})
        infos = await panel_infos(panel, rows)
        self.assertEqual(infos[0]["id"], 1)
        self.assertEqual(infos[1]["source"], "subscription")
        self.assertEqual(panel.single_calls, ["tok2"])

    async def test_a_panel_that_ignores_the_filter_falls_back_for_every_service(self) -> None:
        rows = services(ID_CHUNK * 2, start=1000)
        users = {s.pg_user_id: s.pg_username for s in rows} | {1: "someone", 2: "else"}
        panel = FakePanel(users, ignore_filter=True)
        stats = BatchStats()
        infos = await panel_infos(panel, rows, stats=stats)
        self.assertTrue(stats.filter_ignored)
        self.assertEqual(len(panel.list_calls), 1)
        self.assertEqual(stats.single_calls, len(rows))
        self.assertTrue(all(info["source"] == "subscription" for info in infos))

    async def test_a_failed_list_call_falls_back_for_that_chunk_only(self) -> None:
        rows = services(ID_CHUNK + 3)
        panel = FakePanel({s.pg_user_id: s.pg_username for s in rows}, failing_chunks=1)
        stats = BatchStats()
        infos = await panel_infos(panel, rows, stats=stats)
        self.assertEqual(stats.list_calls, 2)
        self.assertEqual(stats.single_calls, ID_CHUNK)
        self.assertEqual(infos[-1]["id"], rows[-1].pg_user_id)

    async def test_services_without_a_valid_panel_id_or_token(self) -> None:
        rows = [Service(None, "a", "tok1"), Service(True, "user1", "tok1"), Service(-3, "x", None)]
        panel = FakePanel({1: "user1"})
        infos = await panel_infos(panel, rows)
        self.assertEqual(panel.list_calls, [])
        self.assertEqual(panel.single_calls, ["tok1", "tok1"])
        self.assertIsNone(infos[2])

    async def test_a_failed_single_lookup_is_none(self) -> None:
        class Down(FakePanel):
            async def subscription_info(self, token: str) -> dict:
                raise TimeoutError

        infos = await panel_infos(Down({}), services(2))
        self.assertEqual(infos, [None, None])


if __name__ == "__main__":
    unittest.main()


class ClientRequestTests(unittest.IsolatedAsyncioTestCase):
    """The real client asks for the ids the way the panel reads a list query, and skips the read cache."""

    async def asyncSetUp(self) -> None:
        resilience.reset_read_circuits()
        settings = Settings(
            _env_file=None, PG_BASE_URL="https://127.0.0.1", PG_USERNAME="owner",
            PG_PASSWORD="test-password", PG_API_KEY="", PG_ACCESS_TOKEN="",
        )
        self.requests: list[httpx.Request] = []

        def answer(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(200, json={"users": [{"id": 7, "username": "user7"}], "total": 1})

        with patch("app.services.pasarguard.get_settings", return_value=settings):
            self.client = PasarGuardClient(username="owner", access_token="test-token")
        await self.client._client.aclose()
        self.client._api_base_resolved = True
        self.client._client = httpx.AsyncClient(base_url=self.client.base_url, transport=httpx.MockTransport(answer))

    async def asyncTearDown(self) -> None:
        await self.client.close()
        resilience.reset_read_circuits()

    async def test_ids_are_sent_as_a_repeated_query_and_never_cached(self) -> None:
        for _ in range(2):
            data = await self.client.list_users_by_ids([7, 9])
        self.assertEqual(data["users"][0]["id"], 7)
        self.assertEqual(len(self.requests), 2)
        query = self.requests[0].url.params
        self.assertEqual(self.requests[0].url.path, "/api/users")
        self.assertEqual(query.get_list("ids"), ["7", "9"])
        self.assertEqual((query.get("limit"), query.get("offset")), ("2", "0"))
