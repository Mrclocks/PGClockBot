"""Concurrent handling of Telegram updates: customers in parallel, each customer's updates in order.

Shop bots poll in one loop each and the main bot can receive updates by webhook. Handling an update
inline made every customer of a bot wait for the slowest update in front of them (a slow PasarGuard
call, a payment provider). Updates are therefore handed to a scheduler that runs different customers
at the same time but keeps one customer's updates in arrival order, so a double tap or a typed reply
never races the step before it (aiogram scopes FSM state the same way: bot, chat, user). The number of
handlers running at once and the backlog are both bounded.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Hashable
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any, Protocol

from aiogram.types.update import UpdateTypeLookupError

log = logging.getLogger(__name__)

# Handlers running at the same time across every bot in this process, and for one bot.
MAX_CONCURRENT_UPDATES = 32
MAX_CONCURRENT_PER_BOT = 8
# Updates accepted but not finished. A bot at its own limit waits for room (its polling or webhook pauses)
# without touching other bots; the process limit only bounds memory if many bots are stuck at once.
MAX_PENDING_UPDATES = 1024
MAX_PENDING_PER_BOT = 64
# Updates one customer may have waiting; more taps while the panel is slow are dropped, not queued.
MAX_PENDING_PER_CUSTOMER = 8
# How long a stopping shop bot lets its running updates finish before they are cancelled.
STOP_GRACE_SEC = 10.0

Job = Callable[[], Awaitable[None]]


class KeyedTaskRunner:
    """Runs jobs concurrently across keys and one at a time per key, at most ``limit`` at once."""

    def __init__(self, *, limit: int) -> None:
        self._slots = asyncio.Semaphore(limit)
        self._tails: dict[Hashable, asyncio.Task[None]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._room = asyncio.Event()
        self._room.set()

    @property
    def pending(self) -> int:
        return len(self._tasks)

    def submit(self, key: Hashable, job: Job, *, gate: asyncio.Semaphore | None = None) -> asyncio.Task[None]:
        """Start ``job`` after every earlier job of the same key has finished.

        ``gate`` is an extra limit taken before a shared slot, so a group that is at its own limit
        waits without holding a slot another group could use."""
        previous = self._tails.get(key)
        task = asyncio.create_task(self._run(previous, job, gate))
        self._tails[key] = task
        self._tasks.add(task)
        task.add_done_callback(lambda done, k=key: self._finished(k, done))
        return task

    async def wait_for_room(self, max_pending: int) -> None:
        """Return once fewer than ``max_pending`` jobs are waiting or running."""
        while len(self._tasks) >= max_pending:
            self._room.clear()
            await self._room.wait()

    async def _run(self, previous: asyncio.Task[None] | None, job: Job, gate: asyncio.Semaphore | None) -> None:
        if previous is not None:
            # Order only: the earlier job's own outcome is logged where it ran.
            await asyncio.wait({previous})
        async with gate or nullcontext(), self._slots:
            try:
                await job()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("update job failed")

    def _finished(self, key: Hashable, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if self._tails.get(key) is task:
            del self._tails[key]
        self._room.set()


class UpdateFeeder(Protocol):
    async def feed_update(self, bot: Any, update: Any) -> Any: ...


def update_order_key(bot_id: int, update: Any) -> Hashable:
    """The ordering key of an update: its bot, chat and sender, or the update itself when it has neither."""
    try:
        event = update.event
    except UpdateTypeLookupError:
        event = None
    message = getattr(event, "message", None)
    chat = getattr(event, "chat", None) or getattr(message, "chat", None)
    user = getattr(event, "from_user", None) or getattr(event, "user", None)
    chat_id = getattr(chat, "id", None)
    user_id = getattr(user, "id", None)
    if chat_id is None and user_id is None:
        return ("update", bot_id, getattr(update, "update_id", id(update)))
    return (bot_id, chat_id, user_id)


@dataclass(frozen=True, slots=True)
class SchedulerLimits:
    concurrent: int = MAX_CONCURRENT_UPDATES
    concurrent_per_bot: int = MAX_CONCURRENT_PER_BOT
    pending: int = MAX_PENDING_UPDATES
    pending_per_bot: int = MAX_PENDING_PER_BOT
    pending_per_customer: int = MAX_PENDING_PER_CUSTOMER


def _open_event() -> asyncio.Event:
    event = asyncio.Event()
    event.set()
    return event


@dataclass(slots=True)
class _BotLane:
    """One bot's share of the scheduler: its running limit, its queued updates and each sender's count."""

    slots: asyncio.Semaphore
    tasks: set[asyncio.Task[None]] = field(default_factory=set)
    per_key: dict[Hashable, int] = field(default_factory=dict)
    room: asyncio.Event = field(default_factory=_open_event)


class BotUpdateScheduler:
    """Feeds updates to the dispatcher through a :class:`KeyedTaskRunner`, with limits per bot and per sender.

    A slow panel or one customer tapping repeatedly fills only that bot's lane: other shops and the main
    bot keep their own room and their own running slots."""

    def __init__(self, limits: SchedulerLimits | None = None) -> None:
        self._limits = limits or SchedulerLimits()
        self._runner = KeyedTaskRunner(limit=self._limits.concurrent)
        self._lanes: dict[int, _BotLane] = {}

    @property
    def pending(self) -> int:
        return self._runner.pending

    async def submit(self, dispatcher: UpdateFeeder, bot: Any, update: Any) -> asyncio.Task[None] | None:
        """Queue ``update`` for ``bot``; waits while that bot's backlog is full.

        Returns ``None`` when the sender already has the maximum number of updates waiting."""
        bot_id = int(bot.id)
        lane = await self._lane_with_room(bot_id)
        await self._runner.wait_for_room(self._limits.pending)
        lane = self._lane(bot_id)
        key = update_order_key(bot_id, update)
        if lane.per_key.get(key, 0) >= self._limits.pending_per_customer:
            log.warning("bot %s: update %s dropped, its sender already has %s waiting",
                        bot_id, getattr(update, "update_id", "?"), self._limits.pending_per_customer)
            return None
        task = self._runner.submit(key, _handler(dispatcher, bot, update), gate=lane.slots)
        lane.tasks.add(task)
        lane.per_key[key] = lane.per_key.get(key, 0) + 1
        task.add_done_callback(lambda done: self._forget(bot_id, key, done))
        return task

    async def drain_bot(self, bot_id: int, *, grace_sec: float | None = None) -> None:
        """Let a stopping bot's updates finish for up to ``grace_sec`` (default STOP_GRACE_SEC), then cancel the rest."""
        lane = self._lanes.get(int(bot_id))
        await self._drain(set(lane.tasks) if lane else set(), grace_sec=grace_sec, label=f"bot {bot_id}")

    async def drain_all(self, *, grace_sec: float | None = None) -> None:
        """On shutdown: let every queued update finish for up to ``grace_sec``, then cancel the rest."""
        tasks = {t for lane in self._lanes.values() for t in lane.tasks}
        await self._drain(tasks, grace_sec=grace_sec, label="shutdown")

    def _lane(self, bot_id: int) -> _BotLane:
        lane = self._lanes.get(bot_id)
        if lane is None:
            lane = self._lanes[bot_id] = _BotLane(slots=asyncio.Semaphore(self._limits.concurrent_per_bot))
        return lane

    async def _lane_with_room(self, bot_id: int) -> _BotLane:
        lane = self._lane(bot_id)
        while len(lane.tasks) >= self._limits.pending_per_bot:
            lane.room.clear()
            await lane.room.wait()
            # An emptied lane is removed; take whichever lane the bot has now.
            lane = self._lane(bot_id)
        return lane

    @staticmethod
    async def _drain(tasks: set[asyncio.Task[None]], *, grace_sec: float | None, label: str) -> None:
        if not tasks:
            return
        grace_sec = STOP_GRACE_SEC if grace_sec is None else grace_sec
        _done, still_running = await asyncio.wait(tasks, timeout=grace_sec)
        for task in still_running:
            task.cancel()
        if still_running:
            log.warning("%s: %s update(s) cancelled after %ss", label, len(still_running), grace_sec)
            await asyncio.wait(still_running)

    def _forget(self, bot_id: int, key: Hashable, task: asyncio.Task[None]) -> None:
        lane = self._lanes.get(bot_id)
        if lane is None or task not in lane.tasks:
            return
        lane.tasks.discard(task)
        left = lane.per_key.get(key, 1) - 1
        if left > 0:
            lane.per_key[key] = left
        else:
            lane.per_key.pop(key, None)
        lane.room.set()
        if not lane.tasks:
            del self._lanes[bot_id]


def _handler(dispatcher: UpdateFeeder, bot: Any, update: Any) -> Job:
    bot_id = int(bot.id)

    async def handle() -> None:
        try:
            await dispatcher.feed_update(bot, update)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Last resort for one update: log it and keep the bot serving everyone else.
            log.exception("bot %s failed to handle update %s", bot_id, getattr(update, "update_id", "?"))

    return handle


_scheduler: BotUpdateScheduler | None = None


def get_update_scheduler() -> BotUpdateScheduler:
    """The process-wide scheduler shared by shop-bot polling and the main bot's webhook."""
    global _scheduler
    if _scheduler is None:
        _scheduler = BotUpdateScheduler()
    return _scheduler
