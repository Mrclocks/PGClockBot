"""Log updates that take long to handle, so slow buttons are found from data, not guesses.

Only the kind of update and the button's callback data are logged, never what a person typed.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

logger = logging.getLogger(__name__)

# A tap answered later than this feels stuck; Telegram shows the spinner until then.
SLOW_UPDATE_SECONDS = 1.5
# Callback data can carry ids; a short shape is enough to find the handler.
LABEL_LIMIT = 40
_DIGITS = re.compile(r"\d+")


def update_label(event: TelegramObject) -> str:
    """``callback:adm:users:svc:#:#`` for a tap, ``command:/start`` or ``message`` otherwise."""
    callback = getattr(event, "callback_query", None)
    if callback is not None:
        return "callback:" + _DIGITS.sub("#", str(getattr(callback, "data", "") or ""))[:LABEL_LIMIT]
    message = getattr(event, "message", None)
    if message is None:
        return str(getattr(event, "event_type", None) or "update")
    text = str(getattr(message, "text", "") or "")
    if text.startswith("/"):
        return "command:" + text.split(maxsplit=1)[0].split("@", 1)[0][:LABEL_LIMIT]
    return "message"


class SlowUpdateMiddleware(BaseMiddleware):
    """Outermost middleware: times the whole update, including the other middlewares."""

    def __init__(self, *, threshold: float = SLOW_UPDATE_SECONDS) -> None:
        self.threshold = threshold

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        started = time.monotonic()
        try:
            return await handler(event, data)
        finally:
            elapsed = time.monotonic() - started
            if elapsed >= self.threshold:
                logger.warning("slow update %s took %.2fs", update_label(event), elapsed)
