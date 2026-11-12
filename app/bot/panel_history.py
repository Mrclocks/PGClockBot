"""The screens a panel message showed, so «بازگشت» leads to the one the person came from.

Each screen's back button has a fixed parent: the customer card always went back to the users list,
even when it was opened from a stuck order, a cancellation card or the second page of the list.
For every panel message the bot now keeps the screens it showed, in order, and an update that
opens a screen there builds its back buttons with the screen before it (``app.bot.panel_back``).

Only read-only screens are recorded (``VIEW_SCREENS``), because «بازگشت» sends the recorded
callback again: an approve, a delete or a toggle must never run a second time from a back button.
Screens of the same family (another page of a list, another customer's card) replace each other
instead of piling up, and opening a screen already in the history returns to it.

A panel opened from a notice starts with the close button as its exit, so its first screen goes
back to the notice. When a typed reply or an action shows a screen the history cannot place, the
history of that message is dropped and the fixed parents apply again. The histories live in
memory, bounded; after a restart every back button leads to its fixed parent, as before.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

from app.bot.panel_back import BackPlan, Level, planning_back
from app.bot.screen_replay import watching_replay
from app.services.notice_links import NOTICE_CLOSE_CALLBACK, NOTICE_SCREEN_PREFIXES

PANELS_TRACKED = 5000
HISTORY_DEPTH = 12

# Read-only screens: their handlers only read (a cache refresh or clearing an unfinished prompt at
# most), so sending their callback again from «بازگشت» is the same as tapping them again.
VIEW_SCREENS: frozenset[str] = frozenset({
    "nv:adm:home", "nv:adm:ops", "nv:adm:people", "nv:adm:product", "nv:adm:marketing",
    "nv:adm:system", "nv:adm:pg", "nv:adm:loy", "nv:adm:users", "nv:adm:resellers",
    "nv:adm:settings", "nv:adm:backup", "nv:adm:broadcast", "nv:adm:plans",
    "nv:res:home", "nv:w:home", "nv:s:home", "nv:loy:home",
    "res:plans", "res:st:hub", "shp:bc:hub", "svc:list",
    "adm:dash", "adm:orders", "adm:cancellations", "adm:resapp:list", "adm:tickets",
    "autorv:list", "payvoid:list",
    "res:dash", "res:orders", "res:tickets", "res:cancellations",
})
# Read-only screen families: the part before the id or page.
VIEW_FAMILIES: tuple[str, ...] = (
    "nv:adm:plans:aud:", "nv:res:grp:",
    "adm:users:list:", "adm:users:view:", "adm:users:tag:", "adm:users:svcs:", "adm:users:svc:",
    "adm:resellers:list:", "adm:resellers:view:", "adm:resellers:svcs:",
    "adm:order:", "adm:cancel:view:", "adm:resapp:view:",
    "res:users:", "res:user:", "res:cancel:view:",
    "cnrv:v:", "payvoid:v:", "autorv:v:", "stk:l:", "stk:v:", "tru:l:", "saud:l:",
)

PanelKey = tuple[int, int, int]


def view_family(data: str | None) -> str | None:
    """The family of a read-only screen's callback, or None when it is not one."""
    if not data:
        return None
    if data in VIEW_SCREENS:
        return data
    return next((family for family in VIEW_FAMILIES if data.startswith(family)), None)


@dataclass(slots=True)
class Shown:
    data: str
    family: str
    # The fixed parents of this screen's back buttons: an action showing the same level points there.
    parents: set[str] = field(default_factory=set)


@dataclass(slots=True)
class PanelHistory:
    shown: list[Shown] = field(default_factory=list)
    # Where the first screen goes back to: the close button of a notice panel, else its fixed parent.
    exit: str | None = None

    def before(self, data: str, family: str) -> list[Shown]:
        """The screens that stay before ``data`` when it is opened.

        Returning to a shown family drops what came after it. Opening the fixed parent of a shown
        screen is a move up, not forward: the parent takes that screen's place.
        """
        newest_first = range(len(self.shown) - 1, -1, -1)
        for index in newest_first:
            if self.shown[index].family == family:
                return self.shown[:index]
        for index in newest_first:
            if data in self.shown[index].parents:
                return self.shown[:index]
        return list(self.shown)

    def previous_of(self, screens: list[Shown]) -> str | None:
        return screens[-1].data if screens else self.exit


class PanelHistories:
    """Histories keyed by (bot, chat, message); the oldest panel is forgotten first."""

    def __init__(self, *, limit: int = PANELS_TRACKED, depth: int = HISTORY_DEPTH) -> None:
        self._limit = limit
        self._depth = depth
        self._panels: OrderedDict[PanelKey, PanelHistory] = OrderedDict()

    def get(self, key: PanelKey) -> PanelHistory | None:
        return self._panels.get(key)

    def open_from_notice(self, key: PanelKey) -> None:
        self._store(key, PanelHistory(exit=NOTICE_CLOSE_CALLBACK))

    def show(self, key: PanelKey, screens: list[Shown], screen: Shown, *, exit: str | None) -> None:
        shown = [*screens, screen]
        if len(shown) > self._depth:
            # The oldest screens are gone, so the first one left no longer leads out of the panel.
            shown, exit = shown[-self._depth:], None
        self._store(key, PanelHistory(shown=shown, exit=exit))

    def back_to(self, key: PanelKey, count: int) -> None:
        """Keep the first ``count`` screens of the history."""
        history = self._panels.get(key)
        if history is not None:
            del history.shown[count:]

    def forget(self, key: PanelKey) -> None:
        self._panels.pop(key, None)

    def clear(self) -> None:
        self._panels.clear()

    def _store(self, key: PanelKey, history: PanelHistory) -> None:
        self._panels[key] = history
        self._panels.move_to_end(key)
        while len(self._panels) > self._limit:
            self._panels.popitem(last=False)


panel_history = PanelHistories()


def panel_key(bot: Bot, message: Message) -> PanelKey:
    return (int(bot.id), int(message.chat.id), int(message.message_id))


def notice_screens_are_views() -> bool:
    """A notice panel's first screen must be recorded, or its back could not close the panel."""
    return all(view_family(prefix) == prefix for prefix in NOTICE_SCREEN_PREFIXES)


Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class PanelHistoryMiddleware(BaseMiddleware):
    """Plans the back buttons of each update and records the screens panels show."""

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        bot = data.get("bot")
        if bot is None or not isinstance(event, Update):
            return await handler(event, data)
        callback = event.callback_query
        if callback is not None and isinstance(callback.message, Message) and callback.data:
            return await _callback_turn(handler, event, data, bot, callback)
        if event.message is not None:
            return await _message_turn(handler, event, data, bot, event.message)
        return await handler(event, data)


async def _message_turn(handler: Handler, event: Update, data: dict[str, Any], bot: Bot, message: Message) -> Any:
    """A typed reply that shows a screen in a panel replaces whatever that panel showed before."""
    with watching_replay() as log:
        result = await handler(event, data)
    for message_id in log.edited_in(message.chat.id):
        panel_history.forget((int(bot.id), int(message.chat.id), message_id))
    return result


def _plan(history: PanelHistory | None, data: str, family: str | None) -> tuple[BackPlan | None, list[Shown]]:
    """The back plan of this update and, for a screen being opened, the screens before it."""
    if family is not None:
        screens = history.before(data, family) if history else []
        previous = history.previous_of(screens) if history else None
        return BackPlan(levels=[Level(parents=set(), previous=previous)], noting=True), screens
    if history is None or not history.shown:
        return None, []
    levels = [
        Level(parents=history.shown[index].parents, previous=history.previous_of(history.shown[:index]))
        for index in range(len(history.shown) - 1, -1, -1)
    ]
    return BackPlan(levels=levels, noting=False), []


async def _callback_turn(
    handler: Handler, event: Update, data: dict[str, Any], bot: Bot, callback: CallbackQuery
) -> Any:
    key = panel_key(bot, callback.message)
    history = panel_history.get(key)
    family = view_family(callback.data)
    plan, screens = _plan(history, callback.data, family)
    with watching_replay() as log:
        if plan is None:
            result = await handler(event, data)
        else:
            with planning_back(plan):
                result = await handler(event, data)
    if plan is None or not log.was_edited(key[1], key[2]):
        return result
    if family is not None:
        exit_ = history.exit if history else None
        panel_history.show(key, screens, Shown(callback.data, family, plan.levels[0].parents), exit=exit_)
    elif plan.unmatched:
        panel_history.forget(key)
    elif plan.matched:
        # The action showed a screen further back: the screens after it are left.
        panel_history.back_to(key, len(history.shown) - plan.matched)
    return result
