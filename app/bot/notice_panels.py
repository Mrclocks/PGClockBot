"""Panels opened from a button under a notice, and how «بازگشت» leaves them.

A notice button (``nto:<screen>``) opens its screen in a new message under the notice. When that
first screen is built, the parents its back buttons point to are noted for the panel and those
buttons close the panel instead, so the person is back at the notice. Later screens of the panel
that lead back to one of those parents close it too (see ``app.bot.panel_back``).

The panels live in memory, bounded like the chat cleanup lists. After a restart a panel opened
earlier simply behaves like any other screen: its back buttons lead to their fixed parents.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

from app.bot.panel_back import leaving_panel

PANELS_TRACKED = 5000

PanelKey = tuple[int, int, int]


@dataclass(slots=True)
class NoticePanel:
    first_screen: str
    exit_parents: set[str] = field(default_factory=set)


class NoticePanels:
    """Open panels keyed by (bot, chat, message); the oldest is forgotten first."""

    def __init__(self, *, limit: int = PANELS_TRACKED) -> None:
        self._limit = limit
        self._panels: OrderedDict[PanelKey, NoticePanel] = OrderedDict()

    def open(self, key: PanelKey, screen: str) -> None:
        self._panels[key] = NoticePanel(first_screen=screen)
        self._panels.move_to_end(key)
        while len(self._panels) > self._limit:
            self._panels.popitem(last=False)

    def get(self, key: PanelKey) -> NoticePanel | None:
        return self._panels.get(key)

    def close(self, key: PanelKey) -> None:
        self._panels.pop(key, None)

    def clear(self) -> None:
        self._panels.clear()


notice_panels = NoticePanels()


def panel_key(bot: Bot, message: Message) -> PanelKey:
    return (int(bot.id), int(message.chat.id), int(message.message_id))


def tapped_panel(bot: Bot | None, callback: CallbackQuery | None) -> NoticePanel | None:
    if bot is None or callback is None or not isinstance(callback.message, Message):
        return None
    return notice_panels.get(panel_key(bot, callback.message))


class NoticePanelMiddleware(BaseMiddleware):
    """Builds the screens of a notice panel with «بازگشت» leaving the panel at its first level."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        callback = event.callback_query if isinstance(event, Update) else None
        panel = tapped_panel(data.get("bot"), callback)
        if panel is None:
            return await handler(event, data)
        with leaving_panel(panel.exit_parents, noting=callback.data == panel.first_screen):
            return await handler(event, data)
