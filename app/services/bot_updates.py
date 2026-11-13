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
from typing import Any, Protocol

from aiogram.types.update import UpdateTypeLookupError

log = logging.getLogger(__name__)

# Handlers running at the same time across every bot in this process.
MAX_CONCURRENT_UPDATES = 32
# Updates accepted but not finished; past this, polling and the webhook wait for room.
MAX_PENDING_UPDATES = 512
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

    def submit(self, key: Hashable, job: Job) -> asyncio.Task[None]:
        """Start ``job`` after every earlier job of the same key has finished."""
        previous = self._tails.get(key)
        task = asyncio.create_task(self._run(previous, job))
        self._tails[key] = task
        self._tasks.add(task)
        task.add_done_callback(lambda done, k=key: self._finished(k, done))
        return task

    async def wait_for_room(self, max_pending: int) -> None:
        """Return once fewer than ``max_pending`` jobs are waiting or running."""
        while len(self._tasks) >= max_pending:
            self._room.clear()
            await self._room.wait()

    async def _run(self, previous: asyncio.Task[None] | None, job: Job) -> None:
        if previous is not None:
            # Order only: the earlier job's own outcome is logged where it ran.
            await asyncio.wait({previous})
        async with self._slots:
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


class BotUpdateScheduler:
    """Feeds updates to the dispatcher through a :class:`KeyedTaskRunner` and tracks them per bot."""

    def __init__(self, *, limit: int = MAX_CONCURRENT_UPDATES, max_pending: int = MAX_PENDING_UPDATES) -> None:
        self._runner = KeyedTaskRunner(limit=limit)
        self._max_pending = max_pending
        self._by_bot: dict[int, set[asyncio.Task[None]]] = {}

    @property
    def pending(self) -> int:
        return self._runner.pending

    async def submit(self, dispatcher: UpdateFeeder, bot: Any, update: Any) -> asyncio.Task[None]:
        """Queue ``update`` for ``bot``; waits only while the backlog is full."""
        await self._runner.wait_for_room(self._max_pending)
        bot_id = int(bot.id)

        async def handle() -> None:
            try:
                await dispatcher.feed_update(bot, update)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Last resort for one update: log it and keep the bot serving everyone else.
                log.exception("bot %s failed to handle update %s", bot_id, getattr(update, "update_id", "?"))

        task = self._runner.submit(update_order_key(bot_id, update), handle)
        tracked = self._by_bot.setdefault(bot_id, set())
        tracked.add(task)
        task.add_done_callback(lambda done: self._forget(bot_id, done))
        return task

    async def drain_bot(self, bot_id: int, *, grace_sec: float = STOP_GRACE_SEC) -> None:
        """Let a stopping bot's updates finish for up to ``grace_sec`` seconds, then cancel the rest."""
        await self._drain(set(self._by_bot.get(int(bot_id), ())), grace_sec=grace_sec, label=f"bot {bot_id}")

    async def drain_all(self, *, grace_sec: float = STOP_GRACE_SEC) -> None:
        """On shutdown: let every queued update finish for up to ``grace_sec`` seconds, then cancel the rest."""
        await self._drain({t for tasks in self._by_bot.values() for t in tasks}, grace_sec=grace_sec, label="shutdown")

    @staticmethod
    async def _drain(tasks: set[asyncio.Task[None]], *, grace_sec: float, label: str) -> None:
        if not tasks:
            return
        _done, still_running = await asyncio.wait(tasks, timeout=grace_sec)
        for task in still_running:
            task.cancel()
        if still_running:
            log.warning("%s: %s update(s) cancelled after %ss", label, len(still_running), grace_sec)
            await asyncio.wait(still_running)

    def _forget(self, bot_id: int, task: asyncio.Task[None]) -> None:
        tracked = self._by_bot.get(bot_id)
        if tracked is None:
            return
        tracked.discard(task)
        if not tracked:
            del self._by_bot[bot_id]


_scheduler: BotUpdateScheduler | None = None


def get_update_scheduler() -> BotUpdateScheduler:
    """The process-wide scheduler shared by shop-bot polling and the main bot's webhook."""
    global _scheduler
    if _scheduler is None:
        _scheduler = BotUpdateScheduler()
    return _scheduler
