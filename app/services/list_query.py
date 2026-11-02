"""Shared list helpers: case-insensitive substring search + sort keys."""

from __future__ import annotations

from typing import Any, Callable, Iterable

# Default page size for heavy remote lists (e.g. /pg/users).
DEFAULT_LIST_PAGE_SIZE = 50


def normalize_search_q(raw: str | None) -> str:
    return str(raw or "").strip()


def parse_list_page(raw: str | None, *, default: int = 1) -> int:
    """1-based page number; invalid / empty → default (at least 1)."""
    try:
        page = int(str(raw if raw is not None else default).strip())
    except (TypeError, ValueError):
        page = default
    return max(1, page)


def list_offset(page: int, page_size: int) -> int:
    size = max(1, int(page_size))
    return (max(1, int(page)) - 1) * size


def extract_list_total(data: Any) -> int | None:
    """Pull total/count from a PasarGuard-style list payload when present."""
    if not isinstance(data, dict):
        return None
    for key in ("total", "count", "total_count"):
        if data.get(key) is None:
            continue
        try:
            return max(0, int(data[key]))
        except (TypeError, ValueError):
            continue
    return None


def build_list_pager(
    *,
    page: int,
    page_size: int,
    fetched: int,
    total: int | None,
    scoped_len: int | None = None,
) -> dict[str, Any]:
    """Pager flags for templates. ``fetched`` = raw page length before local filters.

    When ownership/search shrinks the page vs the API window, ``total`` is cleared
    so we never claim a false grand total (same idea as bot scoped lists).
    """
    size = max(1, int(page_size))
    page = max(1, int(page))
    fetched = max(0, int(fetched))
    if scoped_len is not None and total is not None and int(scoped_len) != fetched:
        total = None
    pages = None
    if total is not None:
        pages = max(1, (int(total) + size - 1) // size)
        if page > pages:
            page = pages
    has_prev = page > 1
    if pages is not None:
        has_next = page < pages
    else:
        has_next = fetched >= size
    return {
        "page": page,
        "page_size": size,
        "total": total,
        "pages": pages,
        "has_prev": has_prev,
        "has_next": has_next,
        "prev_page": page - 1 if has_prev else None,
        "next_page": page + 1 if has_next else None,
    }


# Letters typed on an Arabic keyboard, and the zero-width joiner, mean the same as their Persian forms.
SEARCH_LETTER_FOLDS = {"ي": "ی", "ك": "ک", "\u200c": " "}
_DIGIT_FOLDS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def fold_search_text(raw: str | None) -> str:
    """Lower-cased text where Arabic ي/ك, half-spaces, Persian digits and repeated spaces cannot hide a match."""
    text = str(raw or "").casefold().translate(_DIGIT_FOLDS)
    for arabic, persian in SEARCH_LETTER_FOLDS.items():
        text = text.replace(arabic, persian)
    return " ".join(text.split())


def text_matches(*parts: Any, needle: str) -> bool:
    """True when needle is empty or appears in any part (case-insensitive, substring, Persian-folded)."""
    needle_cf = fold_search_text(normalize_search_q(needle))
    if not needle_cf:
        return True
    for part in parts:
        if part is None:
            continue
        if needle_cf in fold_search_text(str(part)):
            return True
    return False


def filter_by_search(
    rows: Iterable[Any],
    needle: str,
    extractor: Callable[[Any], Iterable[Any]],
) -> list[Any]:
    q = normalize_search_q(needle)
    if not q:
        return list(rows)
    out: list[Any] = []
    for row in rows:
        if text_matches(*extractor(row), needle=q):
            out.append(row)
    return out


def ilike_pattern(q: str) -> str:
    """SQL ILIKE / LIKE pattern for substring match (caller must use bind param)."""
    # Escape LIKE wildcards in user input
    cleaned = normalize_search_q(q).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{cleaned}%"
