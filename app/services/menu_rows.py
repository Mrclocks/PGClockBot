"""Row layout and button order of a bot menu.

The menu editor saves, per menu and per shop, the menu's buttons as rows of
button keys («▦ چیدمان منو» sets the rows, ⬆️/⬇️ in the edit panel the order).
For most menus the saved rows also carry the order. The main menu and the club
menu keep their order in the web panel's own settings (``EXTERNAL_ORDER_SETTINGS``):
the bot writes those settings, and the saved rows there only regroup buttons in
that order. When such an order is changed in the web panel, the saved rows no
longer match and the menu falls back to its default rows instead of guessing.

Buttons that are missing at render time (a hidden feature, a used trial) are
skipped, so their row simply gets shorter. Buttons the layout does not know
(added by a later release) keep their default rows at their default position.
"""

from __future__ import annotations

import functools
import hashlib
import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.screen_editor import EditorActor
from app.services.users import set_setting, set_settings_bulk

# Persian text in more than four buttons per row is cut on most phones.
MAX_ROW_BUTTONS = 4
# Bounds the stored value and keeps the editor's callback data under Telegram's 64 bytes.
MAX_LAYOUT_BUTTONS = 24
MAX_KEY_LENGTH = 64

KeyRows = tuple[tuple[str, ...], ...]

# Menus whose button order is a web panel setting; the bot edits that same setting.
EXTERNAL_ORDER_SETTINGS = {"c.main": "menu_order", "a.main": "menu_order", "c.loyalty": "loyalty_submenu_order"}
FINGERPRINT_LENGTH = 6

TEXT_NO_NEXT_ROW = "ردیف بعدی وجود ندارد که دکمه‌ای از آن بیاید."
TEXT_ROW_FULL = f"هر ردیف حداکثر {MAX_ROW_BUTTONS} دکمه دارد."
TEXT_ROW_SINGLE = "این ردیف فقط یک دکمه دارد."
TEXT_LAYOUT_CHANGED = "دکمه‌های این منو تغییر کرده؛ چیدمان را دوباره باز کنید."
TEXT_NOT_EDITABLE = "چیدمان این منو قابل ویرایش نیست."
TEXT_ORDER_EDGE = "این دکمه از این بیشتر جابه‌جا نمی‌شود."


def setting_key(screen_id: str) -> str:
    return "menu_rows_" + screen_id.replace(".", "_")


@functools.lru_cache(maxsize=512)
def parse_layout(raw: str | None) -> KeyRows | None:
    """The saved rows, or None when nothing valid is saved (the menu then uses its default rows)."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, list) or not data:
        return None
    rows: list[tuple[str, ...]] = []
    seen: set[str] = set()
    for row in data:
        if not isinstance(row, list) or not 1 <= len(row) <= MAX_ROW_BUTTONS:
            return None
        for key in row:
            if not isinstance(key, str) or not key or len(key) > MAX_KEY_LENGTH or key in seen:
                return None
            seen.add(key)
        rows.append(tuple(row))
    if len(seen) > MAX_LAYOUT_BUTTONS:
        return None
    return tuple(rows)


def serialize_layout(rows: Sequence[Sequence[str]]) -> str:
    return json.dumps([list(row) for row in rows], ensure_ascii=False, separators=(",", ":"))


def follows_saved_order(screen_id: str) -> bool:
    """Whether the saved rows decide the order (False when a web setting owns it)."""
    return screen_id not in EXTERNAL_ORDER_SETTINGS


def _ordered_keys(order: list[str], layout: KeyRows, *, follow_saved_order: bool) -> list[str] | None:
    """The keys in the order to show, or None when the layout no longer fits the menu."""
    position = {key: index for index, key in enumerate(k for row in layout for k in row)}
    known = [key for key in order if key in position]
    if not follow_saved_order:
        return order if known == sorted(known, key=position.__getitem__) else None
    # A key the layout does not know stays right after the key it follows by default.
    following: dict[str | None, list[str]] = defaultdict(list)
    anchor: str | None = None
    for key in order:
        if key in position:
            anchor = key
        else:
            following[anchor].append(key)
    ordered = list(following[None])
    for key in sorted(known, key=position.__getitem__):
        ordered.append(key)
        ordered.extend(following[key])
    return ordered


def arrange(
    default_rows: Sequence[Sequence[str]], layout: KeyRows | None, *, follow_saved_order: bool = True
) -> list[list[str]]:
    """Group the keys present now into rows (and order them), following ``layout`` where it fits."""
    defaults = [list(row) for row in default_rows if row]
    order = [key for row in defaults for key in row]
    if layout is None or len(set(order)) != len(order):
        return defaults
    ordered = _ordered_keys(order, layout, follow_saved_order=follow_saved_order)
    if ordered is None:
        return defaults
    stored_row = {key: index for index, row in enumerate(layout) for key in row}
    default_row = {key: index for index, row in enumerate(defaults) for key in row}
    rows: list[list[str]] = []
    previous: tuple[str, int] | None = None
    for key in ordered:
        group = ("saved", stored_row[key]) if key in stored_row else ("default", default_row[key])
        if group == previous:
            rows[-1].append(key)
        else:
            rows.append([key])
        previous = group
    return rows


def layout_fits(default_rows: Sequence[Sequence[str]], layout: KeyRows | None, *, follow_saved_order: bool) -> bool:
    """False when a saved layout is ignored because the menu's order changed elsewhere."""
    order = [key for row in default_rows for key in row]
    return layout is None or _ordered_keys(order, layout, follow_saved_order=follow_saved_order) is not None


def fingerprint(items: Sequence[str]) -> str:
    """A short tag of a menu's state; a button from an older editor message carries a stale one."""
    return hashlib.sha256("\n".join(items).encode()).hexdigest()[:FINGERPRINT_LENGTH]


# ── Editor steps: a draft is the row sizes over the menu's buttons in order ──


def encode_sizes(sizes: Sequence[int]) -> str:
    return "".join(str(size) for size in sizes)


def decode_sizes(text: str, *, total: int) -> list[int]:
    """Row sizes from a callback, checked against the number of buttons the menu has now."""
    if not text or len(text) > MAX_LAYOUT_BUTTONS or not text.isascii() or not text.isdigit():
        raise ValueError(TEXT_LAYOUT_CHANGED)
    sizes = [int(char) for char in text]
    if any(not 1 <= size <= MAX_ROW_BUTTONS for size in sizes) or sum(sizes) != total:
        raise ValueError(TEXT_LAYOUT_CHANGED)
    return sizes


def widen_row(sizes: Sequence[int], index: int) -> list[int]:
    """➕: the first button of the next row moves up into this row."""
    if not 0 <= index < len(sizes) - 1:
        raise ValueError(TEXT_NO_NEXT_ROW)
    if sizes[index] >= MAX_ROW_BUTTONS:
        raise ValueError(TEXT_ROW_FULL)
    widened = list(sizes)
    widened[index] += 1
    widened[index + 1] -= 1
    return [size for size in widened if size]


def narrow_row(sizes: Sequence[int], index: int) -> list[int]:
    """➖: the last button of this row moves down to the start of the next row, or to a new row."""
    if not 0 <= index < len(sizes):
        raise ValueError(TEXT_LAYOUT_CHANGED)
    if sizes[index] <= 1:
        raise ValueError(TEXT_ROW_SINGLE)
    narrowed = list(sizes)
    narrowed[index] -= 1
    if index + 1 < len(narrowed) and narrowed[index + 1] < MAX_ROW_BUTTONS:
        narrowed[index + 1] += 1
    else:
        narrowed.insert(index + 1, 1)
    return narrowed


def split_rows(keys: Sequence[str], sizes: Sequence[int]) -> list[list[str]]:
    if sum(sizes) != len(keys):
        raise ValueError(TEXT_LAYOUT_CHANGED)
    rows: list[list[str]] = []
    start = 0
    for size in sizes:
        rows.append(list(keys[start : start + size]))
        start += size
    return rows


# ── Button order: ⬆️/⬇️ in the edit panel ──


@dataclass(frozen=True, slots=True)
class MenuOrder:
    """A menu as the order editor sees it.

    ``keys`` and ``sizes`` are the buttons and rows shown now; ``external_order`` is the web
    setting's list for menus whose order lives there (it may hold buttons hidden right now).
    """

    screen_id: str
    keys: tuple[str, ...]
    sizes: tuple[int, ...]
    default_rows: tuple[tuple[str, ...], ...]
    has_saved_rows: bool
    external_order: tuple[str, ...] | None = None

    @property
    def movable(self) -> tuple[str, ...]:
        """The buttons ⬆️/⬇️ can move; buttons a role adds (e.g. «پنل ادمین») stay where they are."""
        if self.external_order is None:
            return self.keys
        listed = set(self.external_order)
        return tuple(key for key in self.keys if key in listed)


def _replace_members(sequence: Sequence[str], members: Sequence[str]) -> list[str]:
    """``sequence`` with the slots held by ``members`` refilled in the order of ``members``."""
    wanted = set(members)
    refill = iter(members)
    return [next(refill) if item in wanted else item for item in sequence]


def moved_order(menu: MenuOrder, index: int, target: int) -> list[str]:
    """The movable buttons after moving the one at ``index`` to the next slot ``target``."""
    movable = list(menu.movable)
    if abs(index - target) != 1 or not 0 <= index < len(movable):
        raise ValueError(TEXT_LAYOUT_CHANGED)
    if not 0 <= target < len(movable):
        raise ValueError(TEXT_ORDER_EDGE)
    movable[index], movable[target] = movable[target], movable[index]
    return movable


def default_order(menu: MenuOrder, default_external: Sequence[str]) -> list[str]:
    """The movable buttons in the order the bot (or the web setting's default) ships with."""
    movable = set(menu.movable)
    if menu.external_order is None:
        return [key for row in menu.default_rows for key in row if key in movable]
    # Buttons the shipped list does not name (a trial placed later) keep their slot.
    shipped = [key for key in default_external if key in movable]
    restored = _replace_members(menu.external_order, shipped)
    return [key for key in restored if key in movable]


def order_values(menu: MenuOrder, new_movable: Sequence[str]) -> dict[str, str]:
    """The settings to write for the new order; row shapes stay as they are."""
    if sorted(new_movable) != sorted(menu.movable) or len(set(new_movable)) != len(new_movable):
        raise ValueError(TEXT_LAYOUT_CHANGED)
    keys = _replace_members(menu.keys, new_movable)
    rows = split_rows(keys, menu.sizes)
    rows_value = serialize_layout(rows)
    if parse_layout(rows_value) is None:
        raise ValueError(TEXT_NOT_EDITABLE)
    if menu.external_order is None:
        default = [list(row) for row in menu.default_rows]
        return {setting_key(menu.screen_id): "" if rows == default else rows_value}
    values = {EXTERNAL_ORDER_SETTINGS[menu.screen_id]: ",".join(_replace_members(menu.external_order, new_movable))}
    if menu.has_saved_rows:
        values[setting_key(menu.screen_id)] = rows_value
    return values


def rows_reset_value(menu: MenuOrder) -> str:
    """«↺ پیش‌فرض» of the row editor: default rows, but a saved order (kept in the rows) stays."""
    default = [list(row) for row in menu.default_rows]
    if menu.external_order is not None or [k for row in default for k in row] == list(menu.keys):
        return ""
    return serialize_layout(split_rows(menu.keys, [len(row) for row in default]))


# ── Saving ──


def _check_audience(actor: EditorActor, audience: str) -> None:
    if audience not in actor.audiences:
        raise ValueError(TEXT_NOT_EDITABLE)


async def save_menu_rows(
    session: AsyncSession,
    actor: EditorActor,
    *,
    screen_id: str,
    audience: str,
    rows: Sequence[Sequence[str]],
    default_rows: Sequence[Sequence[str]],
) -> None:
    """Persist the rows in the actor's own settings; rows equal to the default clear the setting."""
    _check_audience(actor, audience)
    normalized = [list(row) for row in rows]
    if normalized == [list(row) for row in default_rows if row]:
        value = ""
    else:
        value = serialize_layout(normalized)
        if parse_layout(value) is None:
            raise ValueError(TEXT_NOT_EDITABLE)
    await set_setting(session, setting_key(screen_id), value, reseller_id=actor.settings_scope)


async def reset_menu_rows(session: AsyncSession, actor: EditorActor, *, menu: MenuOrder, audience: str) -> None:
    _check_audience(actor, audience)
    await set_setting(session, setting_key(menu.screen_id), rows_reset_value(menu), reseller_id=actor.settings_scope)


async def save_menu_order(
    session: AsyncSession, actor: EditorActor, *, menu: MenuOrder, audience: str, new_movable: Sequence[str]
) -> None:
    """Write the new order (and the rows that carry it) in one commit, in the actor's own settings."""
    _check_audience(actor, audience)
    await set_settings_bulk(session, order_values(menu, new_movable), reseller_id=actor.settings_scope)
