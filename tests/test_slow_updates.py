"""Slow updates are logged with the button's shape, never with what a person typed."""

from __future__ import annotations

import asyncio
import logging
import unittest
from types import SimpleNamespace

from app.bot.slow_updates import SlowUpdateMiddleware, update_label


class LabelTests(unittest.TestCase):
    def test_taps_show_the_callback_shape_without_ids(self) -> None:
        event = SimpleNamespace(callback_query=SimpleNamespace(data="adm:users:svc:6495996073:2"))
        self.assertEqual(update_label(event), "callback:adm:users:svc:#:#")

    def test_commands_are_named_and_typed_text_is_never_logged(self) -> None:
        command = SimpleNamespace(callback_query=None, message=SimpleNamespace(text="/start ref_secret"))
        typed = SimpleNamespace(callback_query=None, message=SimpleNamespace(text="my card 6037 9911"))
        self.assertEqual(update_label(command), "command:/start")
        self.assertEqual(update_label(typed), "message")


class MiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def run_handler(self, delay: float, *, fail: bool = False) -> list[str]:
        event = SimpleNamespace(callback_query=SimpleNamespace(data="nv:adm:users"))

        async def handler(_event, _data):
            await asyncio.sleep(delay)
            if fail:
                raise RuntimeError("boom")
            return "ok"

        # Alembic's fileConfig (run by earlier migration tests) disables loggers that already exist.
        logging.getLogger("app.bot.slow_updates").disabled = False
        with self.assertLogs("app.bot.slow_updates", "WARNING") as logs:
            call = SlowUpdateMiddleware(threshold=0.05)(handler, event, {})
            if fail:
                with self.assertRaises(RuntimeError):
                    await call
            else:
                self.assertEqual(await call, "ok")
            # assertLogs needs at least one record; this marker is filtered out below.
            logging.getLogger("app.bot.slow_updates").warning("marker")
        return [line for line in logs.output if "slow update" in line]

    async def test_a_slow_update_is_logged_with_its_time(self) -> None:
        lines = await self.run_handler(0.1)
        self.assertEqual(len(lines), 1)
        self.assertIn("callback:nv:adm:users", lines[0])

    async def test_a_fast_update_is_not_logged(self) -> None:
        self.assertEqual(await self.run_handler(0), [])

    async def test_a_slow_update_that_fails_is_still_logged_and_the_error_propagates(self) -> None:
        self.assertEqual(len(await self.run_handler(0.1, fail=True)), 1)


if __name__ == "__main__":
    unittest.main()
