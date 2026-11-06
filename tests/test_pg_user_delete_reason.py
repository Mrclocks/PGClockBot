"""Deleting a panel user needs a written reason, like deleting a bot user, reseller or admin."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import httpx

from tests.test_update_owner_only import OWNER_SESSION, RoutesHarness


class PgUserDeleteReasonTests(RoutesHarness):
    async def _delete(self, **form: str) -> httpx.Response:
        cookie = f"session={self.signer.dumps(OWNER_SESSION)}; csrf=token"
        headers = {"cookie": cookie, "x-csrf-token": "token", "origin": "http://panel.test"}
        removal = AsyncMock()
        self.removal = removal
        with patch("app.services.authz.can_pg_page", return_value=True), patch(
            "app.api.pg_pages.staff_user_actions", return_value={"delete": True}
        ), patch(
            "app.api.pg_pages._assert_owned_user", AsyncMock(return_value=object())), patch(
            "app.api.pg_pages.assert_can_mutate_owned_users", AsyncMock()
        ), patch("app.api.pg_pages._staff_pg", AsyncMock(return_value=(MagicMock(), None))), patch(
            "app.services.bot_user_admin.ensure_pg_user_removed_or_absent", removal
        ), patch("app.services.bot_user_admin.detach_local_services_for_pg_user", AsyncMock()):
            response = await self.client.post("/pg/users/77/delete", headers=headers, data=form)
        return response

    def _query(self, response: httpx.Response) -> dict[str, list[str]]:
        return parse_qs(urlsplit(response.headers["location"]).query)

    async def test_deleting_without_a_reason_is_refused_and_removes_nothing(self):
        for form in ({}, {"reason": ""}, {"reason": "  ab "}):
            response = await self._delete(**form)
            self.assertIn("علت حذف", self._query(response).get("err", [""])[0], form)
            self.removal.assert_not_awaited()

    async def test_deleting_with_a_reason_goes_through(self):
        response = await self._delete(reason="customer asked for removal")
        self.assertIn("ok", self._query(response), response.headers["location"])
        self.removal.assert_awaited_once()
