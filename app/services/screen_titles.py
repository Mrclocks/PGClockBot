"""Editable titles and captions at the top of the bot's main menus.

Each title is a one-line setting (``screen_title_<screen>``) and each caption, the
sentence under the title, a setting of its own (``screen_caption_<screen>``); their
defaults are the texts the menus always showed. A stored value is plain text, or text
packed with its Telegram entities when it carries premium (custom) emoji. Both are
rendered escaped, with premium emoji as ``<tg-emoji>``, so stored text can never
inject markup. Titles also never contain ``<``, ``>`` or ``&`` (validated on save).
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from aiogram.types import MessageEntity

from app.services.rich_text import (
    custom_emoji_html,
    first_custom_emoji_id,
    pack_rich_text,
    unpack_rich_text,
    utf16_len,
)

TITLE_MAX_LENGTH = 64
CAPTION_MAX_LENGTH = 600
CAPTION_MAX_LINES = 8
# Stored caption that hides the caption (an empty value falls back to the default).
CAPTION_HIDDEN = "-"
_FORBIDDEN_CHARS = frozenset("<>&")
# A leading run of emoji/symbols stays outside <b>, as the menus always rendered it.
_ICON_PREFIX_RE = re.compile(r"^([^\w\s]+(?:\s+[^\w\s]+)*)\s+(\S.*)$", re.S)

SCREEN_TITLE_DEFAULTS: dict[str, str] = {
    "c.wallet": "👛 کیف پول",
    "c.support": "🎧 پشتیبانی",
    "c.loyalty": "⭐ باشگاه مشتریان",
    "c.services": "📦 سرویس‌های شما",
    "a.home": "🛠 پنل ادمین",
    "a.ops": "🗓 عملیات روزانه",
    "a.people": "👤 افراد",
    "a.product": "📦 محصول و پاسارگارد",
    "a.marketing": "🎯 بازاریابی",
    "a.system": "🛠 سیستم",
    "a.pg": "🖥 عملیات پاسارگارد",
    "a.loyalty": "⭐ باشگاه مشتریان",
    "a.users": "👥 کاربران ربات",
    "a.resellers": "🤝 نمایندگان",
    "a.settings": "⚙️ تنظیمات سریع",
    "a.backup": "💾 بکاپ / ریستور",
    "a.broadcast": "📢 پیام گروهی",
    "a.plans": "💎 پلن‌ها",
    "a.plans.users": "👥 پلن‌های کاربران",
    "a.plans.resellers": "🤝 پلن‌های نمایندگان",
    "r.home": "🤝 پنل نماینده",
    "r.settings": "⚙️ تنظیمات سریع فروشگاه",
    "r.plans": "💎 پلن‌های فروش",
}

_PICK_SECTION = "یک بخش را از دکمه‌های زیر انتخاب کنید."
_PICK_OPTION = "یک گزینه را از دکمه‌های زیر انتخاب کنید."
_PLAN_LIST_CAPTION = "برای دیدن فهرست، «📋 مشاهده پلن‌ها» را بزنید. افزودن پلن و مدیریت کاتالوگ از دکمه‌های زیر است."
_WEB_ONLY_SETTINGS = "ظاهر، رنگ، گزارش روزانه → وب‌پنل."
_ADMIN_WEB_ONLY_SETTINGS = "ظاهر، رنگ دکمه‌ها و گزارش روزانه را از وب‌پنل تنظیم کنید."

# One caption per menu, wherever the menu is opened from. Menus whose caption differed between
# two entry points use the more complete wording.
SCREEN_CAPTION_DEFAULTS: dict[str, str] = {
    "c.wallet": _PICK_OPTION,
    "c.loyalty": "",
    "c.services": "یکی را انتخاب کنید:",
    "a.home": "یک بخش را انتخاب کنید.",
    "a.ops": "داشبورد، سفارش‌ها، رسیدها و تیکت‌ها.",
    "a.people": "مدیریت کاربران ربات و نمایندگان.",
    "a.product": "پلن‌های فروش و عملیات پاسارگارد.",
    "a.marketing": "باشگاه مشتریان، پیام گروهی، کد هدیه و کمپین‌ها.",
    "a.system": "تنظیمات، بکاپ و پیش‌نمایش منوی کاربر.",
    "a.pg": _PICK_SECTION,
    "a.loyalty": _PICK_SECTION,
    "a.users": _PICK_OPTION,
    "a.resellers": _PICK_SECTION,
    "a.settings": f"{_PICK_SECTION}\n{_ADMIN_WEB_ONLY_SETTINGS}",
    "a.backup": (
        "بکاپ کامل شامل دیتابیس همه فروشگاه‌ها، فایل‌های آپلودشده و حساب ورود وب‌پنل است.\n"
        "بکاپ پیش‌فرض بدون .env ساخته می‌شود (این فایل توکن‌ها و رمزها را دارد)؛ مثل بکاپ زمان‌بندی‌شده.\n"
        "بکاپ همراه .env فقط پس از تأیید امنیتی جداگانه ساخته می‌شود.\n"
        "پیش از هر ریستور، یک بکاپ ایمنی کامل (همراه .env) خودکار ساخته می‌شود."
    ),
    "a.broadcast": "مخاطبان پیام را انتخاب کنید:",
    "a.plans": "پلن‌های کاربران، پلن‌های نمایندگان یا یکی از ابزارهای کاتالوگ را انتخاب کنید.",
    "a.plans.users": _PLAN_LIST_CAPTION,
    "a.plans.resellers": _PLAN_LIST_CAPTION,
    "r.home": _PICK_SECTION,
    "r.settings": (
        "بخش را از دکمه‌های زیر انتخاب کنید.\n"
        f"فقط محدودهٔ همین فروشگاه — بدون تنظیمات پلتفرم.\n{_WEB_ONLY_SETTINGS}"
    ),
    "r.plans": "ساخت و کاتالوگ از دکمه‌های زیر.\nبرچسب دسته و بسته حجم/زمان از کیبورد — مثل وب‌پنل.",
}
# The support menu already shows an editable setting under its title.
CAPTION_SETTING_OVERRIDES: dict[str, str] = {"c.support": "support_text"}


def title_setting_key(screen_id: str) -> str:
    return "screen_title_" + screen_id.replace(".", "_")


def caption_setting_key(screen_id: str) -> str:
    return CAPTION_SETTING_OVERRIDES.get(screen_id) or "screen_caption_" + screen_id.replace(".", "_")


def screen_text_defaults() -> dict[str, str]:
    titles = {title_setting_key(sid): title for sid, title in SCREEN_TITLE_DEFAULTS.items()}
    captions = {caption_setting_key(sid): text for sid, text in SCREEN_CAPTION_DEFAULTS.items()}
    return {**titles, **captions}


def has_title(screen_id: str) -> bool:
    return screen_id in SCREEN_TITLE_DEFAULTS


def has_caption(screen_id: str) -> bool:
    return screen_id in SCREEN_CAPTION_DEFAULTS or screen_id in CAPTION_SETTING_OVERRIDES


def can_hide_caption(screen_id: str) -> bool:
    return screen_id in SCREEN_CAPTION_DEFAULTS


def screen_title_raw(ui: dict[str, str] | None, screen_id: str) -> str:
    """The stored title (plain or packed with premium emoji), or the default when blank."""
    stored = (ui or {}).get(title_setting_key(screen_id)) or ""
    return stored if unpack_rich_text(stored)[0].strip() else SCREEN_TITLE_DEFAULTS[screen_id]


def screen_title(ui: dict[str, str] | None, screen_id: str) -> str:
    """The title as plain text (premium emoji show as their regular emoji)."""
    return unpack_rich_text(screen_title_raw(ui, screen_id))[0].strip()


def screen_title_html(ui: dict[str, str] | None, screen_id: str) -> str:
    """The title as escaped HTML with premium emoji, for ``format_message`` cards."""
    text, entities = unpack_rich_text(screen_title_raw(ui, screen_id))
    return custom_emoji_html(text, entities).strip()


def screen_heading(ui: dict[str, str] | None, screen_id: str) -> str:
    """HTML heading in the menus' «🛠 <b>پنل ادمین</b>» shape, premium emoji included."""
    text, entities = unpack_rich_text(screen_title_raw(ui, screen_id))
    text = text.strip()
    match = _ICON_PREFIX_RE.match(text)
    if match is None:
        return f"<b>{custom_emoji_html(text, entities)}</b>"
    icon_end = utf16_len(text[: match.end(1)])
    words_start = utf16_len(text[: match.start(2)])
    icon = custom_emoji_html(text, entities, end=icon_end)
    words = custom_emoji_html(text, entities, start=words_start)
    return f"{icon} <b>{words}</b>"


def screen_caption_raw(ui: dict[str, str] | None, screen_id: str) -> str:
    """The stored caption, or the default when blank; ``CAPTION_HIDDEN`` when hidden."""
    stored = (ui or {}).get(caption_setting_key(screen_id)) or ""
    if unpack_rich_text(stored)[0].strip():
        return stored
    return SCREEN_CAPTION_DEFAULTS.get(screen_id, "")


def screen_caption(ui: dict[str, str] | None, screen_id: str) -> str:
    """The caption as escaped HTML with premium emoji; empty when hidden or unset."""
    text, entities = unpack_rich_text(screen_caption_raw(ui, screen_id))
    if text.strip() == CAPTION_HIDDEN:
        return ""
    return custom_emoji_html(text, entities).strip()


def screen_caption_plain(ui: dict[str, str] | None, screen_id: str) -> str:
    text = unpack_rich_text(screen_caption_raw(ui, screen_id))[0].strip()
    return "" if text == CAPTION_HIDDEN else text


def with_caption(*parts: str) -> str:
    """Join message lines, leaving out empty ones (a hidden caption leaves no blank line)."""
    return "\n".join(part for part in parts if part)


def validate_screen_title(text: str) -> str:
    """Return the cleaned title or raise ValueError with a message for the editor."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("تیتر نمی‌تواند خالی باشد.")
    if "\n" in raw:
        raise ValueError("تیتر باید یک خط باشد.")
    title = " ".join(raw.split())
    if len(title) > TITLE_MAX_LENGTH:
        raise ValueError(f"تیتر حداکثر {TITLE_MAX_LENGTH} کاراکتر است.")
    if _FORBIDDEN_CHARS & set(title):
        raise ValueError("تیتر نمی‌تواند < یا > یا & داشته باشد.")
    return title


def validate_screen_caption(text: str) -> str:
    """Return the caption to store or raise ValueError with a message for the editor."""
    caption = (text or "").strip()
    if not caption:
        raise ValueError("توضیح نمی‌تواند خالی باشد؛ برای پنهان کردن، دکمهٔ «نمایش نده» را بزنید.")
    if len(caption) > CAPTION_MAX_LENGTH:
        raise ValueError(f"توضیح حداکثر {CAPTION_MAX_LENGTH} کاراکتر است.")
    if caption.count("\n") >= CAPTION_MAX_LINES:
        raise ValueError(f"توضیح حداکثر {CAPTION_MAX_LINES} خط است.")
    return caption


def stored_text(cleaned: str, raw: str, entities: Sequence[MessageEntity] | None) -> str:
    """What to save for an edited title or caption: packed with its premium emoji when the
    person's text was kept as sent, so the emoji offsets still point at the right characters."""
    if first_custom_emoji_id(entities) and cleaned == (raw or "").strip():
        return pack_rich_text(raw, entities)
    return cleaned
