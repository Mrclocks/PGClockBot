"""Row layout of a bot menu: which of its buttons share a row.

The menu editor's «▦ چیدمان منو» saves, per menu and per shop, the menu's
buttons as rows of button keys. A layout only regroups buttons; it never
reorders them, so the menu keeps the order its builder (or the web panel's
order settings) gives it. When that order changes, the saved rows no longer
match and the menu falls back to its default rows instead of guessing.

Buttons that are missing at render time (a hidden feature, a used trial) are
skipped, so their row simply gets shorter. Buttons the layout does not know
(added by a later release) keep their default rows at their default position.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.screen_editor import EditorActor
from app.services.users import set_setting

# Persian text in more than four buttons per row is cut on most phones.
MAX_ROW_BUTTONS = 4
# Bounds the stored value and keeps the editor's callback data under Telegram's 64 bytes.
MAX_LAYOUT_BUTTONS = 24
MAX_KEY_LENGTH = 64

KeyRows = tuple[tuple[str, ...], ...]

TEXT_NO_NEXT_ROW = "ردیف بعدی وجود ندارد که دکمه‌ای از آن بیاید."
TEXT_ROW_FULL = f"هر ردیف حداکثر {MAX_ROW_BUTTONS} دکمه دارد."
TEXT_ROW_SINGLE = "این ردیف فقط یک دکمه دارد."
TEXT_LAYOUT_CHANGED = "دکمه‌های این منو تغییر کرده؛ چیدمان را دوباره باز کنید."
TEXT_NOT_EDITABLE = "چیدمان این منو قابل ویرایش نیست."


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


def arrange(default_rows: Sequence[Sequence[str]], layout: KeyRows | None) -> list[list[str]]:
    """Group the keys present now into rows, following ``layout`` where it still fits."""
    defaults = [list(row) for row in default_rows if row]
    if layout is None:
        return defaults
    order = [key for row in defaults for key in row]
    stored_row = {key: index for index, row in enumerate(layout) for key in row}
    present = set(order)
    shared = [key for key in order if key in stored_row]
    if len(present) != len(order) or shared != [key for row in layout for key in row if key in present]:
        return defaults
    default_row = {key: index for index, row in enumerate(defaults) for key in row}
    rows: list[list[str]] = []
    previous: tuple[str, int] | None = None
    for key in order:
        group = ("saved", stored_row[key]) if key in stored_row else ("default", default_row[key])
        if group == previous:
            rows[-1].append(key)
        else:
            rows.append([key])
        previous = group
    return rows


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


async def reset_menu_rows(session: AsyncSession, actor: EditorActor, *, screen_id: str, audience: str) -> None:
    _check_audience(actor, audience)
    await set_setting(session, setting_key(screen_id), "", reseller_id=actor.settings_scope)
