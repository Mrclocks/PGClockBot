"""Search over the Telegram settings catalogue (labels and subsection titles)."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

SEARCH_QUERY_MAX_LEN = 60
SEARCH_RESULT_LIMIT = 8

FUZZY_MIN_WORD_LEN = 3
FUZZY_MIN_RATIO = 0.75

_ARABIC_TO_PERSIAN = str.maketrans({"ي": "ی", "ك": "ک", "ۀ": "ه", "ة": "ه"})
_DIGITS_TO_LATIN = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_STRIPPED_CHARS = "\u200c\u200f\u200e\u0640"


@dataclass(frozen=True, slots=True)
class SettingsSearchEntry:
    """One searchable place: a field (or a whole subsection) and where it lives."""

    label: str
    section_id: str
    section_title: str
    sub_id: str
    sub_title: str
    key: str = ""


def normalize_search_text(text: str) -> str:
    """Fold Arabic letters and Persian/Arabic digits, drop joiners («کارت‌به‌کارت» == «کارت به کارت»)."""
    folded = (text or "").translate(_ARABIC_TO_PERSIAN).translate(_DIGITS_TO_LATIN).casefold()
    for ch in _STRIPPED_CHARS:
        folded = folded.replace(ch, " " if ch == "\u200c" else "")
    return " ".join(folded.split())


def is_close_word(word: str, haystack: str) -> bool:
    """True when ``word`` is a near miss of a whole word in ``haystack`` (one typo)."""
    if len(word) < FUZZY_MIN_WORD_LEN:
        return False
    tokens = haystack.replace("_", " ").split()
    return any(
        abs(len(token) - len(word)) <= 1
        and SequenceMatcher(None, word, token).ratio() >= FUZZY_MIN_RATIO
        for token in tokens
    )


def _match_tier(words: list[str], label: str, where: str) -> int | None:
    """0 = in label, 1 = in location, 2 = typo-tolerant, None = no match."""
    if all(w in label for w in words):
        return 0
    if all(w in label or w in where for w in words):
        return 1
    both = f"{label} {where}"
    if all(w in both or is_close_word(w, both) for w in words):
        return 2
    return None


def search_settings(
    entries: list[SettingsSearchEntry],
    query: str,
    *,
    limit: int = SEARCH_RESULT_LIMIT,
) -> list[SettingsSearchEntry]:
    """Entries matching every word of the query; exact hits first, then near misses."""
    words = normalize_search_text(query[:SEARCH_QUERY_MAX_LEN]).split()
    if not words:
        return []
    hits: list[tuple[int, int, SettingsSearchEntry]] = []
    for order, entry in enumerate(entries):
        label = normalize_search_text(entry.label)
        where = normalize_search_text(
            f"{entry.section_title} {entry.sub_title} {entry.key}"
        )
        tier = _match_tier(words, label, where)
        if tier is not None:
            hits.append((tier, order, entry))
    hits.sort(key=lambda item: (item[0], item[1]))
    return [entry for _, _, entry in hits[:limit]]
