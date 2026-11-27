"""Applies a menu's saved rows and order (``app.services.menu_rows``) while its keyboard is built.

``@editable_screen`` (and the main reply keyboard) publish which menu is being
built in a context variable; keyboard packers then pass the menu's own buttons
(never its «بازگشت» or queue-shortcut rows) through ``arrange_buttons``. Outside
a named menu, or when the menu has no saved layout, the default rows come back
unchanged. Inside ``recording_menu_bodies()`` every arranged body is noted so the
layout editor works on exactly the buttons the menu shows.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, TypeVar

from app.services.menu_rows import MAX_LAYOUT_BUTTONS, arrange, follows_saved_order, parse_layout, setting_key
from app.services.users import current_ui_snapshot

B = TypeVar("B")

# Ids inside callback data (a service, a page) change per viewer; the key keeps only the action.
_NUMBER_PART = re.compile(r"(?<![^:])\d+(?![^:])")

_active_screen: ContextVar[str | None] = ContextVar("row_layout_screen", default=None)
_recorded: ContextVar[list[MenuBody] | None] = ContextVar("row_layout_recorded", default=None)


@dataclass(frozen=True, slots=True)
class MenuBody:
    """One menu's own buttons as shown: keys and texts in order, and the default and shown rows.

    ``keys`` is empty when the buttons cannot be told apart (no key, or the same key twice);
    such a menu keeps its default rows and offers no layout editor. ``buttons`` maps each
    shown button object to its key, so the editor can tell which listed button it is.
    """

    screen_id: str
    keys: tuple[str, ...]
    texts: tuple[str, ...]
    default_rows: tuple[tuple[str, ...], ...]
    rows: tuple[tuple[str, ...], ...]
    buttons: dict[int, str] = field(default_factory=dict, compare=False, repr=False)

    @property
    def is_editable(self) -> bool:
        return bool(self.keys)


@contextmanager
def menu_screen(screen_id: str | None) -> Iterator[None]:
    token = _active_screen.set(screen_id)
    try:
        yield
    finally:
        _active_screen.reset(token)


@contextmanager
def recording_menu_bodies() -> Iterator[list[MenuBody]]:
    bodies: list[MenuBody] = []
    token = _recorded.set(bodies)
    try:
        yield bodies
    finally:
        _recorded.reset(token)


def button_key(button: Any) -> str | None:
    """A key that names the same button for every viewer, or None when there is none."""
    callback_data = getattr(button, "callback_data", None)
    if callback_data:
        return _NUMBER_PART.sub("#", callback_data)
    url = getattr(button, "url", None)
    if url:
        return f"url:{url}"
    web_app = getattr(button, "web_app", None)
    if web_app is not None and getattr(web_app, "url", None):
        return f"app:{web_app.url}"
    return None


def _distinct_keys(rows: Sequence[Sequence[B]], key: Callable[[B], str | None]) -> list[list[str]] | None:
    key_rows: list[list[str]] = []
    seen: set[str] = set()
    for row in rows:
        row_keys: list[str] = []
        for button in row:
            button_id = key(button)
            if button_id is None or button_id in seen:
                return None
            seen.add(button_id)
            row_keys.append(button_id)
        key_rows.append(row_keys)
    if not seen or len(seen) > MAX_LAYOUT_BUTTONS:
        return None
    return key_rows


def _record(screen_id: str, key_rows: list[list[str]] | None, shown: list[list[str]], by_key: dict[str, Any]) -> None:
    bodies = _recorded.get()
    if bodies is None:
        return
    if key_rows is None:
        bodies.append(MenuBody(screen_id, (), (), (), ()))
        return
    keys = tuple(k for row in shown for k in row)
    bodies.append(
        MenuBody(
            screen_id=screen_id,
            keys=keys,
            texts=tuple(str(getattr(by_key[k], "text", "")) for k in keys),
            default_rows=tuple(tuple(row) for row in key_rows),
            rows=tuple(tuple(row) for row in shown),
            buttons={id(by_key[k]): k for k in keys},
        )
    )


def arrange_buttons(
    default_rows: Sequence[Sequence[B]],
    ui: dict[str, str] | None,
    *,
    key: Callable[[B], str | None] = button_key,
) -> list[list[B]]:
    """The menu's own buttons in the rows and order its saved layout asks for (default otherwise)."""
    rows = [list(row) for row in default_rows if row]
    screen_id = _active_screen.get()
    if screen_id is None or not rows:
        return rows
    key_rows = _distinct_keys(rows, key)
    if key_rows is None:
        _record(screen_id, None, [], {})
        return rows
    settings = ui if ui is not None else current_ui_snapshot()
    layout = parse_layout(settings.get(setting_key(screen_id)))
    shown = arrange(key_rows, layout, follow_saved_order=follows_saved_order(screen_id))
    by_key = {
        k: button for row_keys, row in zip(key_rows, rows, strict=True) for k, button in zip(row_keys, row, strict=True)
    }
    _record(screen_id, key_rows, shown, by_key)
    return [[by_key[k] for k in row] for row in shown]
