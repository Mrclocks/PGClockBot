"""Where «بازگشت» leads on the screen being built in this update.

Every back button is built with the ``btn_back`` label and the screen's fixed parent as its
callback. The panel history (``app.bot.panel_history``) knows the screens the person actually came
through, and sets a ``BackPlan`` for the update:

- On a screen being opened, every back button leads to the screen before it in this panel (or the
  close button of a panel opened from a notice), and its fixed parents are noted for that level.
- After an action, the screen shown is one already in the history (the card after a tag was set,
  the card again after the tag picker). Its back buttons are matched against the noted parents,
  deepest level first; the first match tells which level is on screen and leads to the screen
  before it. A back button that matches no level means the action moved somewhere the history
  does not know (``unmatched``).

Without a plan, or with nothing before the level, the fixed parent is kept.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

BACK_LABEL_KEY = "btn_back"


@dataclass(slots=True)
class Level:
    """One screen of the history: its fixed back parents and the screen before it."""

    parents: set[str]
    previous: str | None


@dataclass(slots=True)
class BackPlan:
    # The opened screen's level when ``noting``; otherwise the shown levels, deepest first.
    levels: list[Level]
    noting: bool
    matched: int | None = None
    unmatched: bool = False


_plan: ContextVar[BackPlan | None] = ContextVar("back_plan", default=None)


@contextmanager
def planning_back(plan: BackPlan) -> Iterator[BackPlan]:
    token = _plan.set(plan)
    try:
        yield plan
    finally:
        _plan.reset(token)


def back_callback(parent: str) -> str:
    """The callback of a back button whose fixed parent is ``parent``."""
    plan = _plan.get()
    if plan is None or not plan.levels:
        return parent
    if plan.noting:
        level = plan.levels[0]
        level.parents.add(parent)
        return level.previous or parent
    for index, level in enumerate(plan.levels):
        if parent in level.parents:
            if plan.matched is None:
                plan.matched = index
            return level.previous or parent
    plan.unmatched = True
    return parent
