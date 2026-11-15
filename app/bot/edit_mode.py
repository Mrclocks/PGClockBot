"""Adds «✏️ ویرایش این منو» under registered menus while edit mode is on.

``EditModeMiddleware`` looks up the person's edit-mode switch once per update
(a dictionary read; the settings read only happens when the switch is on) and
publishes the active editor in a context variable. Menu builders marked with
``@editable_screen("<screen id>")`` then append one extra row. Without an
active editor the builders return exactly what they did before.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, ParamSpec

from aiogram import BaseMiddleware
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, TelegramObject

from app.services.screen_editor import EditorActor, edit_mode_store, is_editor_enabled

P = ParamSpec("P")

EDIT_ROW_TEXT = "✏️ ویرایش این منو"
EDIT_CALLBACK_ROOT = "ed"
# The shop's own categories and plans, edited from the shop screens' edit panels.
CATALOG_ROW_ROOT = "edr"
CATALOG_ROW_SCREENS = frozenset({"c.shop", "c.shop.plans", "c.shop.wholesale"})

_active_editor: ContextVar[EditorActor | None] = ContextVar("active_editor", default=None)


def audience_code(actor: EditorActor) -> str:
    return "r" if actor.for_reseller else "a"


def active_editor() -> EditorActor | None:
    return _active_editor.get()


@contextmanager
def editing_as(actor: EditorActor | None) -> Iterator[None]:
    token = _active_editor.set(actor)
    try:
        yield
    finally:
        _active_editor.reset(token)


@contextmanager
def without_editor() -> Iterator[None]:
    """Build a menu as everyone sees it (the editor lists buttons this way)."""
    with editing_as(None):
        yield


def editable_screen(
    screen_id: str | Callable[..., str],
) -> Callable[[Callable[P, InlineKeyboardMarkup]], Callable[P, InlineKeyboardMarkup]]:
    """Mark a menu builder as screen *screen_id* (or a function of the builder's arguments)."""

    def decorate(build: Callable[P, InlineKeyboardMarkup]) -> Callable[P, InlineKeyboardMarkup]:
        @functools.wraps(build)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> InlineKeyboardMarkup:
            markup = build(*args, **kwargs)
            actor = _active_editor.get()
            if actor is None:
                return markup
            resolved = screen_id(*args, **kwargs) if callable(screen_id) else screen_id
            return with_edit_row(markup, actor, resolved)

        return wrapper

    return decorate


def with_edit_row(markup: InlineKeyboardMarkup, actor: EditorActor, screen_id: str) -> InlineKeyboardMarkup:
    # Local import: the registry imports nav_inline, whose builders use this module.
    from app.bot.screen_registry import SCREENS

    spec = SCREENS.get(screen_id)
    if spec is None or spec.audience.value not in actor.audiences:
        return markup
    button = InlineKeyboardButton(
        text=EDIT_ROW_TEXT,
        callback_data=f"{EDIT_CALLBACK_ROOT}:{audience_code(actor)}:o:{screen_id}",
    )
    return InlineKeyboardMarkup(inline_keyboard=[*markup.inline_keyboard, [button]])


def actor_for_update(data: dict[str, Any]) -> EditorActor | None:
    """Who would edit on this bot: the shop owner on a shop bot, otherwise the platform owner."""
    db_user = data.get("db_user")
    if db_user is None or getattr(db_user, "id", None) is None:
        return None
    shop_id = data.get("reseller_owner_id") if data.get("is_reseller_bot") else None
    return EditorActor(user_id=int(db_user.id), shop_id=int(shop_id) if shop_id else None)


class EditModeMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        actor = actor_for_update(data)
        if actor is None or not edit_mode_store.is_active(actor):
            return await handler(event, data)
        if not await _editor_allowed_now(data):
            return await handler(event, data)
        with editing_as(actor):
            return await handler(event, data)


async def _editor_allowed_now(data: dict[str, Any]) -> bool:
    # Local import: menu_nav pulls in the keyboard modules that import this one.
    from app.bot.menu_nav import is_user_preview_active

    session = data.get("session")
    if session is None or not await is_editor_enabled(session):
        return False
    # «پیش‌نمایش به‌عنوان مشتری» must look exactly like the customer's menu.
    return not await is_user_preview_active(data.get("state"))
