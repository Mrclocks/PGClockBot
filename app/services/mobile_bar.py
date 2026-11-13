"""The phone bottom bar's page order, chosen by the owner in System settings → «ظاهر و برند».

The setting holds up to ``DOCK_SLOTS`` page keys from the sidebar registry. Each viewer still sees only
pages their sidebar offers: a chosen page they may not open is skipped and the default order fills its
place (``panel_nav.build_dock``). «منو» always closes the bar.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.db_safe import rollback_quiet
from app.services.panel_nav import DOCK_SLOTS, DockChoice, dock_choices
from app.services.users import get_setting, set_setting

logger = logging.getLogger(__name__)

SETTING_KEY = "panel_mobile_bar"
AUTO = ""
# The bar is drawn on every panel page; a saved order is read from the database at most this often.
_CACHE_TTL_SEC = 30.0
_cache: tuple[float, tuple[str, ...]] | None = None

UNKNOWN_PAGE = "یکی از صفحه‌های انتخاب‌شده معتبر نیست."
DUPLICATE_PAGE = "هر صفحه را فقط یک بار انتخاب کنید."


@dataclass(frozen=True, slots=True)
class MobileBarForm:
    """What the settings box shows: the saved slots (``AUTO`` for empty ones) and the pages to choose from,
    grouped by sidebar section in sidebar order."""

    slots: tuple[str, ...]
    groups: tuple[tuple[str, tuple[DockChoice, ...]], ...]


def _grouped(choices: Iterable[DockChoice]) -> tuple[tuple[str, tuple[DockChoice, ...]], ...]:
    groups: dict[str, list[DockChoice]] = {}
    for choice in choices:
        groups.setdefault(choice.section, []).append(choice)
    return tuple((section, tuple(items)) for section, items in groups.items())


def _choice_keys() -> frozenset[str]:
    return frozenset(choice.key for choice in dock_choices())


def parse_bar_order(raw: str | None) -> tuple[str, ...]:
    """Saved keys in order; unknown, repeated or extra keys (an older release, a hand edit) are left out."""
    known = _choice_keys()
    order: list[str] = []
    for part in str(raw or "").split(","):
        key = part.strip()
        if key in known and key not in order:
            order.append(key)
    return tuple(order[:DOCK_SLOTS])


def clean_bar_slots(values: Iterable[object]) -> tuple[str, ...]:
    """The chosen pages in slot order, empty slots dropped; raises ``ValueError`` for an unknown or repeated page."""
    known = _choice_keys()
    chosen: list[str] = []
    for value in list(values)[:DOCK_SLOTS]:
        key = str(value or "").strip()
        if key == AUTO:
            continue
        if key not in known:
            raise ValueError(UNKNOWN_PAGE)
        if key in chosen:
            raise ValueError(DUPLICATE_PAGE)
        chosen.append(key)
    return tuple(chosen)


def invalidate_bar_cache() -> None:
    global _cache
    _cache = None


async def load_bar_order(session: AsyncSession) -> tuple[str, ...]:
    """The owner's page order for the bar, cached briefly; the default order on any failure."""
    global _cache
    now = time.monotonic()
    if _cache is not None and now - _cache[0] < _CACHE_TTL_SEC:
        return _cache[1]
    try:
        order = parse_bar_order(await get_setting(session, SETTING_KEY, ""))
    except Exception:
        # Display only: the page keeps working with the default order.
        logger.warning("mobile bar order load failed; using the default order", exc_info=True)
        await rollback_quiet(session)
        return ()
    _cache = (now, order)
    return order


async def load_bar_form(session: AsyncSession) -> MobileBarForm:
    order = parse_bar_order(await get_setting(session, SETTING_KEY, ""))
    slots = order + (AUTO,) * (DOCK_SLOTS - len(order))
    return MobileBarForm(slots=slots, groups=_grouped(dock_choices()))


async def save_bar_order(session: AsyncSession, values: Iterable[object]) -> tuple[str, ...]:
    """Validate and store the chosen slots (empty means the default order); returns what was stored."""
    order = clean_bar_slots(values)
    await set_setting(session, SETTING_KEY, ",".join(order))
    invalidate_bar_cache()
    return order
