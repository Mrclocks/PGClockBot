"""Build the real bot dispatcher in a test without leaking router attachment.

The bot routers are module-level objects and aiogram lets a router belong to
one dispatcher only, so a second ``create_dispatcher()`` in the same process
fails with "Router is already attached". Detaching before and after keeps
tests independent of the order xdist runs them in.
"""

from __future__ import annotations

import unittest

from aiogram import Dispatcher

from app.bot import bot_routers, create_dispatcher


def detach_bot_routers() -> None:
    for router in bot_routers():
        router._parent_router = None


def create_isolated_dispatcher(test_case: unittest.TestCase) -> Dispatcher:
    """Return a fresh dispatcher; its routers are released when the test ends."""
    detach_bot_routers()
    test_case.addCleanup(detach_bot_routers)
    return create_dispatcher()
