"""A short PasarGuard outage does not log out an account PasarGuard just confirmed.

The grace never applies to an admin PasarGuard reported as missing or denied,
nor to an account that was never confirmed, and it ends after a few minutes.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.services import pg_staff_access as gate


class _Clock:
    def __init__(self) -> None:
        self.now = 10_000.0

    def monotonic(self) -> float:
        return self.now


class OutageGraceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.clock = _Clock()
        self.answers: list[tuple[str, dict | None]] = []
        self.fetch = AsyncMock(side_effect=lambda _name: self.answers.pop(0))
        for patcher in (
            patch.object(gate, "time", SimpleNamespace(monotonic=self.clock.monotonic)),
            patch.object(gate, "_PG_GATE_CACHE", {}),
            patch.object(gate, "_PG_GATE_LAST_OK", {}),
            patch.object(gate, "fetch_pg_admin_gate", self.fetch),
            patch.object(gate, "revoke_web_access", new=AsyncMock()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def check(self, *answers: tuple[str, dict | None]) -> tuple[bool, str | None]:
        self.answers.extend(answers)
        return await gate.enforce_pg_admin_web_gate(AsyncMock(), "shop_a")

    async def confirm_then_wait(self, seconds: float) -> None:
        self.assertEqual(await self.check(("ok", {"username": "shop_a"})), (True, None))
        self.clock.now += seconds

    async def test_recently_confirmed_account_survives_a_short_outage(self) -> None:
        await self.confirm_then_wait(gate._PG_GATE_TTL_SEC + 10)
        self.assertEqual(await self.check(("unreachable", None)), (True, None))
        gate.revoke_web_access.assert_not_awaited()

    async def test_grace_ends_after_its_window(self) -> None:
        await self.confirm_then_wait(gate.GATE_OUTAGE_GRACE_SEC + 1)
        self.assertEqual(await self.check(("unreachable", None)), (False, gate.PG_UNAVAILABLE_MSG))

    async def test_never_confirmed_account_is_refused_during_an_outage(self) -> None:
        self.assertEqual(await self.check(("unreachable", None)), (False, gate.PG_UNAVAILABLE_MSG))

    async def test_deleted_admin_gets_no_grace_afterwards(self) -> None:
        await self.confirm_then_wait(gate._PG_GATE_TTL_SEC + 1)
        allowed, _message = await self.check(("missing", None))
        self.assertFalse(allowed)
        self.clock.now += gate._PG_GATE_TTL_SEC + 1
        self.assertEqual(await self.check(("unreachable", None)), (False, gate.PG_UNAVAILABLE_MSG))

    async def test_access_kept_by_grace_is_rechecked_soon(self) -> None:
        await self.confirm_then_wait(gate._PG_GATE_TTL_SEC + 1)
        await self.check(("unreachable", None))
        self.clock.now += gate._PG_GATE_UNAVAILABLE_TTL_SEC + 0.1
        allowed, _message = await self.check(("missing", None))
        self.assertFalse(allowed)
        self.assertEqual(self.fetch.await_count, 3)

    async def test_grace_does_not_outlive_its_window_through_the_cache(self) -> None:
        await self.confirm_then_wait(gate.GATE_OUTAGE_GRACE_SEC - 1)
        self.assertTrue((await self.check(("unreachable", None)))[0])
        self.clock.now += gate._PG_GATE_UNAVAILABLE_TTL_SEC + 0.1
        self.assertEqual(await self.check(("unreachable", None)), (False, gate.PG_UNAVAILABLE_MSG))

    def test_correct_password_during_an_outage_does_not_count_toward_lockout(self) -> None:
        self.assertEqual(gate.login_denial(gate.PG_UNAVAILABLE_MSG), (503, False))
        self.assertEqual(gate.login_denial(gate.PG_ACCESS_DENIED_MSG), (403, True))
        self.assertEqual(gate.login_denial(None), (403, True))

    def test_outage_denials_are_told_apart_from_real_denials(self) -> None:
        self.assertTrue(gate.is_outage_denial(gate.PG_UNAVAILABLE_MSG))
        self.assertFalse(gate.is_outage_denial(gate.PG_ACCESS_DENIED_MSG))
        self.assertFalse(gate.is_outage_denial(None))


if __name__ == "__main__":
    unittest.main()
