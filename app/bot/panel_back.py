"""Where «بازگشت» leads on the screen being built in this update.

Every back button is built with the ``btn_back`` label and the screen's fixed parent as its
callback. In a panel opened from a notice, the first screen's parent is not where the person came
from: the notice is. While that first screen is built, its parents are noted and «بازگشت» becomes
the close button. Any later screen of the panel whose back leads to one of those parents is at the
same level (the card re-rendered after an action, a sibling card), so it closes the panel too;
deeper screens keep their fixed parents.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from app.services.notice_links import NOTICE_CLOSE_CALLBACK

BACK_LABEL_KEY = "btn_back"


@dataclass(slots=True)
class PanelExit:
    """The parents that lead out of one panel; ``noting`` while its first screen is built."""

    parents: set[str]
    noting: bool


_panel_exit: ContextVar[PanelExit | None] = ContextVar("panel_exit", default=None)


@contextmanager
def leaving_panel(parents: set[str], *, noting: bool) -> Iterator[None]:
    token = _panel_exit.set(PanelExit(parents=parents, noting=noting))
    try:
        yield
    finally:
        _panel_exit.reset(token)


def back_callback(parent: str) -> str:
    """The callback of a back button whose fixed parent is ``parent``."""
    panel = _panel_exit.get()
    if panel is None:
        return parent
    if panel.noting:
        panel.parents.add(parent)
        return NOTICE_CLOSE_CALLBACK
    return NOTICE_CLOSE_CALLBACK if parent in panel.parents else parent
