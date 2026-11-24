"""Hardening of adding an existing service: no token in logs, atomic attempt limit, ACL, deep link cleanup."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch


from sqlalchemy import select

from app.db.models import BotUser, ServiceImportAttempt, UserService
from app.services import service_import as si
from app.services.bot_user_admin import admin_delete_service
from app.services.pasarguard import PasarGuardError
from app.services.service_import import RATE_LIMITED, ServiceImportError
from tests.test_service_import import ImportCase

SECRET = "tok-SECRETSECRET1"


class TokenNeverReachesTheLogTests(ImportCase):
    async def test_a_panel_outage_does_not_log_the_request_url_or_traceback(self):
        self.platform_panel.fail_with = PasarGuardError(f"GET https://p.example/sub/{SECRET}/info failed (503)", status_code=503)
        # The logger is replaced instead of captured: other tests may switch logging off for the process.
        with patch.object(si, "logger") as log:
            self.assertEqual(await self.refused(1, SECRET), si.PANEL_UNAVAILABLE)
        self.assertGreaterEqual(log.warning.call_count, 1)
        rendered = []
        for call in log.warning.call_args_list:
            self.assertNotIn("exc_info", call.kwargs)
            rendered.append(str(call.args[0]) % tuple(call.args[1:]))
        self.assertTrue(any("503" in line for line in rendered))
        for line in rendered:
            self.assertNotIn(SECRET, line)
        self.assertFalse(log.exception.called)


class FailedAttemptLimitHoldsUnderBurstTests(ImportCase):
    async def test_simultaneous_guesses_cannot_all_reach_the_panel(self):
        calls = 0
        original = self.platform_panel.subscription_info

        async def counting(token: str, **kwargs) -> dict:
            nonlocal calls
            calls += 1
            return await original(token, **kwargs)

        self.platform_panel.subscription_info = counting
        results = await asyncio.gather(
            *(self.run_import(1, f"guess-{i:08d}") for i in range(12)), return_exceptions=True
        )
        self.assertLessEqual(calls, si.MAX_FAILED_ATTEMPTS)
        self.assertTrue(all(isinstance(r, ServiceImportError) for r in results))
        self.assertGreaterEqual(sum(1 for r in results if str(r) == RATE_LIMITED), 12 - si.MAX_FAILED_ATTEMPTS)

    async def test_a_try_that_was_rate_limited_is_not_kept_as_another_failure(self):
        results = await asyncio.gather(
            *(self.run_import(1, f"guess-{i:08d}") for i in range(12)), return_exceptions=True
        )
        # How many simultaneous guesses get through depends on how they interleave; whatever
        # the split, only the tries that were not rate limited are stored, and never more than the limit.
        checked = sum(1 for r in results if str(r) != RATE_LIMITED)
        async with self.Session() as session:
            rows = (await session.scalars(select(ServiceImportAttempt))).all()
        self.assertEqual(len(rows), checked)
        self.assertLessEqual(len(rows), si.MAX_FAILED_ATTEMPTS)
        self.assertTrue(all(r.outcome == "refused" for r in rows), [r.outcome for r in rows])

    async def test_a_try_that_never_finished_counts_as_failed_until_the_window_passes(self):
        async with self.Session() as session:
            session.add_all(
                ServiceImportAttempt(bot_user_id=1, token_hash=f"h{i}", outcome=si.PENDING)
                for i in range(si.MAX_FAILED_ATTEMPTS)
            )
            await session.commit()
        self.assertEqual(await self.refused(1, "tok-plat-0000001"), RATE_LIMITED)

    async def test_successful_tries_leave_no_pending_rows(self):
        await self.run_import(1, "tok-plat-0000001")
        async with self.Session() as session:
            outcomes = [r.outcome for r in (await session.scalars(select(ServiceImportAttempt))).all()]
        self.assertEqual(outcomes, ["added"])


class DetachNeedsTheSameRightAsDeleteTests(ImportCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        async with self.Session() as session:
            session.add(UserService(id=903, bot_user_id=1, pg_user_id=903, pg_username="new", remark="imported"))
            await session.commit()

    async def _detach(self, staff):
        async with self.Session() as session:
            service = await session.get(UserService, 903)
            return await admin_delete_service(session, service, delete_pg=False, staff=staff)

    async def test_staff_without_the_panel_delete_right_cannot_detach(self):
        with patch("app.services.pg_access.staff_user_actions", return_value={"delete": False}):
            with self.assertRaises(ValueError):
                await self._detach({"role": "admin"})
        async with self.Session() as session:
            self.assertIsNotNone(await session.get(UserService, 903))

    async def test_staff_with_the_right_can_detach(self):
        with patch("app.services.pg_access.staff_user_actions", return_value={"delete": True}):
            info = await self._detach({"role": "admin"})
        self.assertFalse(info["pg_deleted"])
        async with self.Session() as session:
            self.assertIsNone(await session.get(UserService, 903))

    async def test_the_bot_path_without_staff_is_unchanged(self):
        info = await self._detach(None)
        self.assertEqual(info["service_id"], 903)


class DeepLinkMessageIsRemovedTests(ImportCase):
    async def _link(self, token: str):
        from app.bot.handlers import start

        message = AsyncMock()
        async with self.Session() as session:
            user = await session.get(BotUser, 1)
            with (
                patch.object(start, "get_all_settings", AsyncMock(return_value={"service_import_enabled": "1"})),
                patch.object(start, "render_home", AsyncMock()),
                patch.object(start, "delete_user_message", AsyncMock()) as delete,
            ):
                await start._link_subscription(message, session, user, token)
        return delete, message

    async def test_the_start_message_carrying_the_link_is_deleted_on_success_and_on_refusal(self):
        for token in ("tok-plat-0000001", "tok-unknown-000001"):
            delete, message = await self._link(token)
            delete.assert_awaited_once_with(message)


if __name__ == "__main__":
    unittest.main()
