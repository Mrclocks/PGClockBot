"""Telegram updates: customers handled in parallel, one customer's updates in order, bounded, drained on stop."""

from __future__ import annotations

import asyncio
import os
import unittest
from datetime import datetime, timezone

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from aiogram.types import Update

from unittest import mock

from app.services import bot_updates
from app.services.bot_updates import BotUpdateScheduler, KeyedTaskRunner, SchedulerLimits, update_order_key
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

    def __init__(self, batches: list[list[Update]], bot_id: int = BOT_ID) -> None:
        self.id = bot_id
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
        scheduler = BotUpdateScheduler(SchedulerLimits(concurrent=2, pending=10))
        dispatcher = FakeDispatcher()
        dispatcher.gates[1] = asyncio.Event()
        task = await scheduler.submit(dispatcher, FakeBot([]), _message(1, 500))
        self.assertFalse(task.done())
        self.assertEqual(scheduler.pending, 1)
        dispatcher.gates[1].set()
        await task
        self.assertEqual(dispatcher.handled, [1])

    async def test_drain_cancels_updates_that_outlive_the_grace_period(self) -> None:
        scheduler = BotUpdateScheduler(SchedulerLimits(concurrent=2, pending=10))
        dispatcher = FakeDispatcher()
        dispatcher.gates[1] = asyncio.Event()
        task = await scheduler.submit(dispatcher, FakeBot([]), _message(1, 500))
        with self.assertLogs("app.services.bot_updates", level="WARNING"):
            await scheduler.drain_bot(BOT_ID, grace_sec=0.05)
        self.assertTrue(task.cancelled())

    async def test_shutdown_drain_waits_for_every_bots_updates(self) -> None:
        scheduler = BotUpdateScheduler(SchedulerLimits(concurrent=2, pending=10))
        dispatcher = FakeDispatcher()
        dispatcher.gates[1] = asyncio.Event()
        task = await scheduler.submit(dispatcher, FakeBot([]), _message(1, 500))
        asyncio.get_running_loop().call_later(0.05, dispatcher.gates[1].set)
        await scheduler.drain_all(grace_sec=2)
        self.assertTrue(task.done() and not task.cancelled())
        self.assertEqual(dispatcher.handled, [1])


class FairnessTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_bot_with_a_full_backlog_does_not_hold_up_another_bot(self) -> None:
        scheduler = BotUpdateScheduler(SchedulerLimits(concurrent=8, concurrent_per_bot=2, pending=100,
                                                       pending_per_bot=3, pending_per_customer=8))
        dispatcher = FakeDispatcher()
        stuck = FakeBot([], bot_id=1)
        for update_id in (1, 2, 3):
            dispatcher.gates[update_id] = asyncio.Event()
            await scheduler.submit(dispatcher, stuck, _message(update_id, 500 + update_id))
        waiting = asyncio.create_task(scheduler.submit(dispatcher, stuck, _message(4, 600)))
        await asyncio.sleep(0.05)
        self.assertFalse(waiting.done(), "the stuck bot waits for room in its own backlog")

        other = await asyncio.wait_for(scheduler.submit(dispatcher, FakeBot([], bot_id=2), _message(10, 700)), 1)
        await asyncio.wait_for(other, 1)
        self.assertEqual(dispatcher.handled, [10], "another shop is served while the first is stuck")

        dispatcher.gates[1].set()
        await asyncio.wait_for(waiting, 1)
        for gate in dispatcher.gates.values():
            gate.set()
        await scheduler.drain_all(grace_sec=1)

    async def test_a_stuck_bot_uses_only_its_own_running_slots(self) -> None:
        scheduler = BotUpdateScheduler(SchedulerLimits(concurrent=3, concurrent_per_bot=2, pending=100,
                                                       pending_per_bot=50, pending_per_customer=8))
        dispatcher = FakeDispatcher()
        stuck = FakeBot([], bot_id=1)
        for update_id in range(1, 6):
            dispatcher.gates[update_id] = asyncio.Event()
            await scheduler.submit(dispatcher, stuck, _message(update_id, 500 + update_id))
        other = await scheduler.submit(dispatcher, FakeBot([], bot_id=2), _message(10, 700))
        await asyncio.wait_for(other, 1)
        self.assertEqual(dispatcher.handled, [10])
        for gate in dispatcher.gates.values():
            gate.set()
        await scheduler.drain_all(grace_sec=1)
        self.assertEqual(sorted(dispatcher.handled), [1, 2, 3, 4, 5, 10])

    async def test_repeated_taps_past_the_customer_limit_are_dropped(self) -> None:
        scheduler = BotUpdateScheduler(SchedulerLimits(pending_per_customer=2))
        dispatcher = FakeDispatcher()
        bot = FakeBot([])
        dispatcher.gates[1] = asyncio.Event()
        first = await scheduler.submit(dispatcher, bot, _callback(1, 500))
        second = await scheduler.submit(dispatcher, bot, _callback(2, 500))
        with self.assertLogs("app.services.bot_updates", level="WARNING"):
            third = await scheduler.submit(dispatcher, bot, _callback(3, 500))
        self.assertIsNone(third)
        other_customer = await scheduler.submit(dispatcher, bot, _message(4, 501))
        await asyncio.wait_for(other_customer, 1)
        dispatcher.gates[1].set()
        await asyncio.gather(first, second)
        self.assertEqual(dispatcher.handled, [4, 1, 2])
        again = await scheduler.submit(dispatcher, bot, _callback(5, 500))
        await asyncio.wait_for(again, 1)
        self.assertEqual(dispatcher.handled[-1], 5, "the customer can tap again once their updates are done")


class ShopBotPollingTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_slow_customer_does_not_hold_up_another_customer_of_the_shop(self) -> None:
        dispatcher = FakeDispatcher()
        dispatcher.gates[1] = asyncio.Event()
        bot = FakeBot([[_message(1, 500), _message(2, 501)]])
        manager = ResellerBotManager(dispatcher, scheduler=BotUpdateScheduler(SchedulerLimits(concurrent=4, pending=50)))
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
        manager = ResellerBotManager(dispatcher, scheduler=BotUpdateScheduler(SchedulerLimits(concurrent=4, pending=50)))
        poll = asyncio.create_task(manager._poll_loop(1, bot))
        await _until(lambda: 1 in dispatcher.gates and manager._scheduler.pending == 1)
        poll.cancel()
        asyncio.get_running_loop().call_later(0.05, dispatcher.gates[1].set)
        with self.assertRaises(asyncio.CancelledError):
            await poll
        self.assertEqual(bot.session.closed_after, [1], "the in-flight update finished before the session closed")


class StopTests(unittest.IsolatedAsyncioTestCase):
    async def _running_shop(self, manager: ResellerBotManager, dispatcher: FakeDispatcher, rid: int) -> FakeBot:
        update_id = 100 + rid
        dispatcher.gates[update_id] = asyncio.Event()
        bot = FakeBot([[_message(update_id, 900 + rid)]], bot_id=rid)
        bot.session.handled_ref = dispatcher.handled
        manager._bots[rid] = bot
        manager._tasks[rid] = asyncio.create_task(manager._poll_loop(rid, bot))
        await _until(lambda: manager._scheduler.pending >= 1 and update_id in dispatcher.gates)
        return bot

    async def test_stopping_one_shop_returns_without_waiting_for_its_updates(self) -> None:
        dispatcher = FakeDispatcher()
        manager = ResellerBotManager(dispatcher, scheduler=BotUpdateScheduler())
        bot = await self._running_shop(manager, dispatcher, 1)
        await asyncio.wait_for(manager.stop_reseller(1), 0.5)
        self.assertIsNone(bot.session.closed_after, "the in-flight update is still finishing")
        dispatcher.gates[101].set()
        await manager.stop_all()
        self.assertEqual(bot.session.closed_after, [101])

    async def test_stop_all_drains_every_shop_at_the_same_time(self) -> None:
        dispatcher = FakeDispatcher()
        manager = ResellerBotManager(dispatcher, scheduler=BotUpdateScheduler())
        for rid in (1, 2, 3):
            await self._running_shop(manager, dispatcher, rid)
        loop = asyncio.get_running_loop()
        with mock.patch.object(bot_updates, "STOP_GRACE_SEC", 0.3), \
                self.assertLogs("app.services.bot_updates", level="WARNING"):
            started = loop.time()
            await manager.stop_all()
            elapsed = loop.time() - started
        self.assertLess(elapsed, 0.6, "three stuck shops drain in parallel, not one after another")
        self.assertEqual(manager._scheduler.pending, 0)


if __name__ == "__main__":
    unittest.main()
