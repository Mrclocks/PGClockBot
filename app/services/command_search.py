"""Search over the command catalogue: every menu button and setting a staff member can open.

The catalogue is built per viewer by the bot layer (only what that viewer may open), so this
module only ranks entries. Matching reuses the settings search rules (Persian letter and digit
folding, joiners ignored, one typo tolerated) and adds synonyms, so «trial» finds «تست».
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app.py_compat import StrEnum
from app.services.settings_search import SEARCH_QUERY_MAX_LEN, is_close_word, normalize_search_text

# Shorter queries are refused: a single letter matches half the bot.
MIN_QUERY_CHARS = 2


class CommandKind(StrEnum):
    MENU = "menu"
    SETTING = "setting"
    # The text of a button, edited in settings; listed after the menus and settings themselves.
    LABEL = "label"


_KIND_ORDER = {CommandKind.MENU: 0, CommandKind.SETTING: 1, CommandKind.LABEL: 2}


@dataclass(frozen=True, slots=True)
class CommandEntry:
    """One place the viewer can open: its label, where it lives, and the callback that opens it."""

    label: str
    path: str
    target: str
    kind: CommandKind
    # Extra searchable text that is never shown, such as the setting key.
    terms: str = ""


# Each group lists words people use for the same thing; a query word in a group also matches the others.
SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
    ("کارت", "card", "کارت به کارت"),
    ("تست", "trial", "آزمایشی", "رایگان"),
    ("کاربر", "مشتری", "user", "customer"),
    ("نماینده", "reseller", "همکار"),
    ("پرداخت", "payment", "تراکنش"),
    ("درگاه", "gateway"),
    ("رسید", "فیش", "receipt"),
    ("تیکت", "ticket"),
    ("کیف پول", "wallet", "موجودی"),
    ("تخفیف", "discount", "کوپن"),
    ("پلن", "plan", "تعرفه", "بسته"),
    ("سرویس", "اشتراک", "کانفیگ", "service"),
    ("گزارش", "آمار", "report"),
    ("دکمه", "button", "کلید"),
    ("بکاپ", "backup"),
    ("همگانی", "گروهی", "broadcast"),
    ("رمزارز", "کریپتو", "crypto", "ارز دیجیتال"),
    ("استارز", "stars"),
    ("دعوت", "رفرال", "referral", "معرف"),
    ("کش بک", "cashback"),
    ("هدیه", "gift"),
    ("پاسارگارد", "pasarguard"),
    ("قوانین", "terms"),
    ("کانال", "channel", "عضویت"),
    ("حجم", "گیگ", "ترافیک"),
    ("تمدید", "renew"),
)
_SYNONYMS: dict[str, tuple[str, ...]] = {}
for _group in SYNONYM_GROUPS:
    _folded = tuple(normalize_search_text(term) for term in _group)
    for _term in _folded:
        _SYNONYMS[_term] = _folded


@dataclass(frozen=True, slots=True)
class _Prepared:
    entry: CommandEntry
    label: str
    # Label and hidden terms: synonyms match here only, since every path shares words like «پنل».
    own_text: str
    haystack: str


def query_words(query: str) -> list[str]:
    """The folded words of a query; empty when it is too short to search."""
    folded = normalize_search_text((query or "")[:SEARCH_QUERY_MAX_LEN])
    if len(folded.replace(" ", "")) < MIN_QUERY_CHARS:
        return []
    return folded.split()


def _has_synonym(word: str, haystack: str) -> bool:
    return any(alt in haystack for alt in _SYNONYMS.get(word, ()))


def _rank(words: list[str], phrase: str, item: _Prepared) -> int | None:
    """0 = label starts with the query, 1 = in label, 2 = in its place, 3 = synonym, 4 = typo."""
    if item.label.startswith(phrase):
        return 0
    if all(w in item.label for w in words):
        return 1
    if all(w in item.haystack for w in words):
        return 2
    if all(w in item.haystack or _has_synonym(w, item.own_text) for w in words):
        return 3
    if all(w in item.haystack or _has_synonym(w, item.own_text) or is_close_word(w, item.haystack) for w in words):
        return 4
    return None


def _without_leading_symbols(text: str) -> str:
    """«💾 بکاپ» → «بکاپ», so a menu button's icon does not hide that its label starts with the query."""
    for index, char in enumerate(text):
        if char.isalnum():
            return text[index:]
    return text


def _prepare(entry: CommandEntry) -> _Prepared:
    label = _without_leading_symbols(normalize_search_text(entry.label))
    own_text = f"{label} {normalize_search_text(entry.terms)}"
    haystack = f"{own_text} {normalize_search_text(entry.path)}"
    return _Prepared(entry=entry, label=label, own_text=own_text, haystack=haystack)


def search_commands(entries: Iterable[CommandEntry], query: str) -> list[CommandEntry]:
    """Entries matching every word of the query, best first; a repeated place is listed once."""
    words = query_words(query)
    if not words:
        return []
    phrase = " ".join(words)
    hits: list[tuple[int, int, int, CommandEntry]] = []
    seen: set[tuple[str, str]] = set()
    for order, entry in enumerate(entries):
        identity = (entry.target, normalize_search_text(entry.label))
        if identity in seen:
            continue
        rank = _rank(words, phrase, _prepare(entry))
        if rank is None:
            continue
        seen.add(identity)
        hits.append((rank, _KIND_ORDER[entry.kind], order, entry))
    hits.sort(key=lambda hit: hit[:3])
    return [hit[3] for hit in hits]


def filter_kind(entries: list[CommandEntry], kind: CommandKind | None) -> list[CommandEntry]:
    return entries if kind is None else [entry for entry in entries if entry.kind == kind]
