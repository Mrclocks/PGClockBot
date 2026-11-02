"""Messages the person must keep: deliveries, configs, credentials, payment results, backups.

In a private chat the bot's messages are temporary by default and the next screen replaces
them (``app.bot.chat_screen``). Code that sends something worth keeping runs it inside
``keep_sent()`` (or decorates the sending function with ``@keeps_sent``), and every message sent
meanwhile stays in the chat.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import ParamSpec, TypeVar

_keeping: ContextVar[bool] = ContextVar("chat_records_keeping", default=False)

P = ParamSpec("P")
R = TypeVar("R")


def is_keeping() -> bool:
    """True while the messages being sent are records the chat must keep."""
    return _keeping.get()


@contextmanager
def keep_sent() -> Iterator[None]:
    """Every message sent inside this block stays in the chat."""
    token = _keeping.set(True)
    try:
        yield
    finally:
        _keeping.reset(token)


def keeps_sent(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Decorate an async function whose messages are records (a delivery, a config file …)."""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        with keep_sent():
            return await fn(*args, **kwargs)

    return wrapper
