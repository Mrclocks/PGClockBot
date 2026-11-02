"""PasarGuard admin lookups must return the requested admin or nothing.

Billing ticks, PAYG watermarks, dashboards and the pg_staff web gate all act
on the record returned by ``get_admin_gate``. PasarGuard filters
``/api/admins?username=`` as a substring, so a similar name must never be
accepted in place of the requested admin, and an unfinished directory scan
must never be reported as a confirmed absence.
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

from app.services.pasarguard import (
    ADMIN_DIRECTORY_MAX_PAGES,
    ADMIN_DIRECTORY_PAGE_SIZE,
    PasarGuardClient,
    PasarGuardError,
)


def _admins(prefix: str, count: int) -> list[dict[str, Any]]:
    return [{"id": index + 1, "username": f"{prefix}{index}"} for index in range(count)]


class _FakePanel:
    """Answers lookup paths; ``directory`` maps a page offset to its admins."""

    def __init__(self, directory: Any = None, *, direct: Any = None, reject_filter: bool = False) -> None:
        self.directory = directory
        self.direct = direct
        self.reject_filter = reject_filter
        self.directory_calls: list[dict[str, Any]] = []

    async def answer(self, method: str, path: str, **kwargs: Any) -> Any:
        if path == "/api/admin":
            return {"username": "owner", "id": 1}
        if path.startswith("/api/admin/"):
            if self.direct is None:
                raise PasarGuardError("not found", 404)
            return self.direct
        if path == "/api/admins":
            params = dict(kwargs.get("params") or {})
            self.directory_calls.append(params)
            if self.reject_filter and "username" in params:
                raise PasarGuardError("unprocessable", 422)
            if callable(self.directory):
                return self.directory(params)
            return {"admins": list(self.directory or [])}
        raise AssertionError(f"unexpected path {path}")


class AdminExactLookupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.client = PasarGuardClient(username="owner", password="x")

    async def asyncTearDown(self) -> None:
        await self.client.close()

    async def _gate(self, panel: _FakePanel, username: str) -> tuple[str, dict | None]:
        with patch.object(self.client, "request", new=AsyncMock(side_effect=panel.answer)):
            return await self.client.get_admin_gate(username)

    async def test_single_similar_name_is_not_accepted_as_the_admin(self) -> None:
        panel = _FakePanel([{"id": 9, "username": "alireza", "used_traffic": 10**12}])
        gate, admin = await self._gate(panel, "ali")
        self.assertEqual(gate, "missing")
        self.assertIsNone(admin)

    async def test_direct_record_without_username_is_not_accepted(self) -> None:
        panel = _FakePanel([], direct={"id": 9, "used_traffic": 10**12})
        gate, admin = await self._gate(panel, "ali")
        self.assertEqual(gate, "missing")
        self.assertIsNone(admin)

    async def test_direct_record_for_another_username_is_not_accepted(self) -> None:
        panel = _FakePanel([], direct={"id": 9, "username": "alireza"})
        gate, admin = await self._gate(panel, "ali")
        self.assertEqual(gate, "missing")
        self.assertIsNone(admin)

    async def test_username_match_ignores_case(self) -> None:
        panel = _FakePanel([{"id": 4, "username": "Ali"}])
        gate, admin = await self._gate(panel, "ali")
        self.assertEqual(gate, "ok")
        self.assertEqual((admin or {}).get("id"), 4)

    async def test_exact_admin_on_a_later_page_is_found(self) -> None:
        def directory(params: dict[str, Any]) -> dict[str, Any]:
            if params["offset"] == 0:
                return {"admins": _admins("ali", ADMIN_DIRECTORY_PAGE_SIZE)}
            return {"admins": [{"id": 777, "username": "ali"}]}

        panel = _FakePanel(directory)
        gate, admin = await self._gate(panel, "ali")
        self.assertEqual(gate, "ok")
        self.assertEqual((admin or {}).get("id"), 777)
        self.assertEqual([call["offset"] for call in panel.directory_calls], [0, ADMIN_DIRECTORY_PAGE_SIZE])

    async def test_panel_ignoring_offset_is_unreachable_not_missing(self) -> None:
        panel = _FakePanel(lambda params: {"admins": _admins("ali", ADMIN_DIRECTORY_PAGE_SIZE)})
        gate, admin = await self._gate(panel, "ali")
        self.assertEqual(gate, "unreachable")
        self.assertIsNone(admin)
        self.assertEqual(len(panel.directory_calls), 2)

    async def test_panel_ignoring_limit_is_unreachable_when_absent(self) -> None:
        panel = _FakePanel(_admins("ali", ADMIN_DIRECTORY_PAGE_SIZE + 5))
        gate, _admin = await self._gate(panel, "ali")
        self.assertEqual(gate, "unreachable")

    async def test_panel_ignoring_limit_still_finds_exact_admin(self) -> None:
        listing = _admins("ali", ADMIN_DIRECTORY_PAGE_SIZE + 5) + [{"id": 555, "username": "ali"}]
        gate, admin = await self._gate(_FakePanel(listing), "ali")
        self.assertEqual(gate, "ok")
        self.assertEqual((admin or {}).get("id"), 555)

    async def test_scan_cut_off_at_page_limit_is_unreachable_not_missing(self) -> None:
        def directory(params: dict[str, Any]) -> dict[str, Any]:
            return {"admins": _admins(f"ali-{params['offset']}-", ADMIN_DIRECTORY_PAGE_SIZE)}

        panel = _FakePanel(directory)
        gate, _admin = await self._gate(panel, "ali")
        self.assertEqual(gate, "unreachable")
        self.assertEqual(len(panel.directory_calls), ADMIN_DIRECTORY_MAX_PAGES)

    async def test_rejected_filter_falls_back_to_plain_listing(self) -> None:
        panel = _FakePanel([{"id": 3, "username": "bob"}, {"id": 8, "username": "ali"}], reject_filter=True)
        gate, admin = await self._gate(panel, "ali")
        self.assertEqual(gate, "ok")
        self.assertEqual((admin or {}).get("id"), 8)
        self.assertNotIn("username", panel.directory_calls[-1])

    async def test_complete_listing_without_the_admin_is_missing(self) -> None:
        gate, admin = await self._gate(_FakePanel(_admins("other", 3)), "ali")
        self.assertEqual(gate, "missing")
        self.assertIsNone(admin)

    async def test_get_admin_returns_none_for_similar_name(self) -> None:
        panel = _FakePanel([{"id": 9, "username": "alireza"}])
        with patch.object(self.client, "request", new=AsyncMock(side_effect=panel.answer)):
            self.assertIsNone(await self.client.get_admin("ali"))


if __name__ == "__main__":
    unittest.main()
