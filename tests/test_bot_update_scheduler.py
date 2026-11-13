"""Telegram updates: customers handled in parallel, one customer's updates in order, bounded, drained on stop."""

from __future__ import annotations

import asyncio
import os
import unittest
from datetime import datetime, timezone

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from aiogram.types import Update

from app.services.bot_updates import BotUpdateScheduler, KeyedTaskRunner, update_order_key
from app.services.reseller_bots import ResellerBotManager

BOT_ID = 7
NOW = int(datetime.now(timezone.utc).timestamp())


def _user(user_id: int) -> dict:
    return {"id": user_id, "is_bot": False, "first_name": "u"}


def _message(update_id: int, user_id: int, text: str = "hi") -> Update:
    return Update.model_validate({"update_id": update_id, "message": {
        "message_id": update_id, "date": NOW, "chat": {"id": user_id, "type": "private"},
        "from": _user(user_id), "text": text}})


def _callback(update_id: int, user_id: int, *, with_message: bool = True) -> Update:
    query = {"id": str(update_id), "from": _user(user_id), "chat_instance": "c", "data": "buy:1"}
    if with_message:
        query["message"] = {"message_id": 1, "date": NOW, "chat": {"id": user_id, "type": "private"},
                            "from": _user(1), "text": "menu"}
    else:
        query["inline_message_id"] = "inline-1"
    return Update.model_validate({"update_id": update_id, "callback_query": query})


async def _until(predicate, within: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.005)


class KeyedTaskRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_customers_are_handled_at_the_same_time(self) -> None:
        runner = KeyedTaskRunner(limit=4)
        slow_gate = asyncio.Event()
        done: list[str] = []

        async def slow() -> None:
            await slow_gate.wait()
            done.append("slow")

        async def fast() -> None:
            done.append("fast")

        slow_task = runner.submit("customer-a", slow)
        await runner.submit("customer-b", fast)
        self.assertEqual(done, ["fast"])
        self.assertFalse(slow_task.done())
        slow_gate.set()
        await slow_task
        self.assertEqual(done, ["fast", "slow"])

    async def test_one_customers_updates_run_in_arrival_order(self) -> None:
        runner = KeyedTaskRunner(limit=4)
        gate = asyncio.Event()
        order: list[int] = []

        async def first() -> None:
            await gate.wait()
            order.append(1)

        async def second() -> None:
            order.append(2)

        runner.submit("customer-a", first)
        second_task = runner.submit("customer-a", second)
        await asyncio.sleep(0.05)
        self.assertEqual(order, [], "the second update must wait for the first")
        gate.set()
        await second_task
        self.assertEqual(order, [1, 2])

    async def test_running_handlers_never_exceed_the_limit(self) -> None:
        runner = KeyedTaskRunner(limit=2)
        gate = asyncio.Event()
        running = 0
        peak = 0

        async def job() -> None:
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await gate.wait()
            running -= 1

        tasks = [runner.submit(f"customer-{i}", job) for i in range(5)]
        await _until(lambda: running == 2)
        await asyncio.sleep(0.05)
        self.assertEqual(peak, 2)
        gate.set()
        await asyncio.gather(*tasks)
        self.assertEqual(peak, 2)
        self.assertEqual(runner.pending, 0)

    async def test_a_failing_update_does_not_block_the_next_one_of_the_same_customer(self) -> None:
        runner = KeyedTaskRunner(limit=2)
        handled: list[str] = []

        async def broken() -> None:
            raise RuntimeError("panel timeout")

        async def next_one() -> None:
            handled.append("next")

        with self.assertLogs("app.services.bot_updates", level="ERROR"):
            runner.submit("customer-a", broken)
            await runner.submit("customer-a", next_one)
        self.assertEqual(handled, ["next"])

    async def test_wait_for_room_holds_while_the_backlog_is_full(self) -> None:
        runner = KeyedTaskRunner(limit=1)
        gate = asyncio.Event()

        async def job() -> None:
            await gate.wait()

        runner.submit("a", job)
        runner.submit("b", job)
        waiter = asyncio.create_task(runner.wait_for_room(2))
        await asyncio.sleep(0.05)
        self.assertFalse(waiter.done())
        gate.set()
        await asyncio.wait_for(waiter, 1)


class UpdateOrderKeyTests(unittest.TestCase):
    def test_a_customers_message_and_button_tap_share_one_key(self) -> None:
        self.assertEqual(update_order_key(BOT_ID, _message(1, 500)), (BOT_ID, 500, 500))
        self.assertEqual(update_order_key(BOT_ID, _callback(2, 500)), (BOT_ID, 500, 500))

    def test_customers_and_bots_get_different_keys(self) -> None:
        self.assertNotEqual(update_order_key(BOT_ID, _message(1, 500)), update_order_key(BOT_ID, _message(2, 501)))
        self.assertNotEqual(update_order_key(BOT_ID, _message(1, 500)), update_order_key(BOT_ID + 1, _message(1, 500)))

    def test_inline_button_without_a_chat_is_keyed_by_its_sender(self) -> None:
        self.assertEqual(update_order_key(BOT_ID, _callback(3, 500, with_message=False)), (BOT_ID, None, 500))

    def test_update_without_chat_or_sender_is_not_ordered_against_others(self) -> None:
        self.assertEqual(update_order_key(BOT_ID, Update(update_id=9)), ("update", BOT_ID, 9))


class FakeDispatcher:
    def __init__(self) -> None:
        self.handled: list[int] = []
        self.gates: dict[int, asyncio.Event] = {}

    async def feed_update(self, bot, update) -> None:
        gate = self.gates.get(update.update_id)
        if gate is not None:
            await gate.wait()
        self.handled.append(update.update_id)


class FakeSession:
    def __init__(self) -> None:
        self.closed_after: list[int] | None = None
        self.handled_ref: list[int] = []

    async def close(self) -> None:
        if self.closed_after is None:
            self.closed_after = list(self.handled_ref)


class FakeBot:
    id = BOT_ID

    def __init__(self, batches: list[list[Update]]) -> None:
        self._batches = batches
        self.session = FakeSession()

    async def delete_webhook(self, **_kwargs) -> None:
        return None

    async def get_updates(self, **_kwargs) -> list[Update]:
        if self._batches:
            return self._batches.pop(0)
        await asyncio.Event().wait()
        return []


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_submit_returns_before_the_update_is_handled(self) -> None:
        scheduler = BotUpdateScheduler(limit=2, max_pending=10)
        dispatcher = FakeDispatcher()
        dispatcher.gates[1] = asyncio.Event()
        task = await scheduler.submit(dispatcher, FakeBot([]), _message(1, 500))
        self.assertFalse(task.done())
        self.assertEqual(scheduler.pending, 1)
        dispatcher.gates[1].set()
        await task
        self.assertEqual(dispatcher.handled, [1])

    async def test_drain_cancels_updates_that_outlive_the_grace_period(self) -> None:
        scheduler = BotUpdateScheduler(limit=2, max_pending=10)
        dispatcher = FakeDispatcher()
        dispatcher.gates[1] = asyncio.Event()
        task = await scheduler.submit(dispatcher, FakeBot([]), _message(1, 500))
        with self.assertLogs("app.services.bot_updates", level="WARNING"):
            await scheduler.drain_bot(BOT_ID, grace_sec=0.05)
        self.assertTrue(task.cancelled())

    async def test_shutdown_drain_waits_for_every_bots_updates(self) -> None:
        scheduler = BotUpdateScheduler(limit=2, max_pending=10)
        dispatcher = FakeDispatcher()
        dispatcher.gates[1] = asyncio.Event()
        task = await scheduler.submit(dispatcher, FakeBot([]), _message(1, 500))
        asyncio.get_running_loop().call_later(0.05, dispatcher.gates[1].set)
        await scheduler.drain_all(grace_sec=2)
        self.assertTrue(task.done() and not task.cancelled())
        self.assertEqual(dispatcher.handled, [1])


class ShopBotPollingTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_slow_customer_does_not_hold_up_another_customer_of_the_shop(self) -> None:
        dispatcher = FakeDispatcher()
        dispatcher.gates[1] = asyncio.Event()
        bot = FakeBot([[_message(1, 500), _message(2, 501)]])
        manager = ResellerBotManager(dispatcher, scheduler=BotUpdateScheduler(limit=4, max_pending=50))
        poll = asyncio.create_task(manager._poll_loop(1, bot))
        await _until(lambda: dispatcher.handled == [2])
        self.assertEqual(dispatcher.handled, [2], "customer 501 is served while 500 waits on the panel")
        dispatcher.gates[1].set()
        await _until(lambda: dispatcher.handled == [2, 1])
        poll.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await poll

    async def test_stopping_a_shop_bot_lets_running_updates_finish_before_closing_its_session(self) -> None:
        dispatcher = FakeDispatcher()
        dispatcher.gates[1] = asyncio.Event()
        bot = FakeBot([[_message(1, 500)]])
        bot.session.handled_ref = dispatcher.handled
        manager = ResellerBotManager(dispatcher, scheduler=BotUpdateScheduler(limit=4, max_pending=50))
        poll = asyncio.create_task(manager._poll_loop(1, bot))
        await _until(lambda: 1 in dispatcher.gates and manager._scheduler.pending == 1)
        poll.cancel()
        asyncio.get_running_loop().call_later(0.05, dispatcher.gates[1].set)
        with self.assertRaises(asyncio.CancelledError):
            await poll
        self.assertEqual(bot.session.closed_after, [1], "the in-flight update finished before the session closed")


if __name__ == "__main__":
    unittest.main()
