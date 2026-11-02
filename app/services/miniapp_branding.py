"""The brand a Mini App shows: its shop's name, caption, photo, accent colour and notice.

Name, caption and photo come from the bot's Telegram profile (settings → هویت ربات), so a
shop sets them once for both its bot and its Mini App. Accent colour and notice are the
Mini App's own settings; the colour is one of a fixed set of presets (each tested for
readable text on it), never a free colour. Everything is read from the settings of the Mini App's shop only;
a shop never sees the platform's values (``get_all_settings`` keeps shops isolated).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.users import get_all_settings, set_settings_bulk

ACCENT_SETTING = "miniapp_accent_color"
NOTICE_SETTING = "miniapp_notice"
NAME_SETTING = "bot_tg_name"
CAPTION_SETTING = "bot_tg_short_description"
PHOTO_SETTING = "bot_tg_photo"
PLATFORM_TITLE_SETTING = "shop_title"

NAME_MAX = 64
CAPTION_MAX = 120
NOTICE_MAX = 200
# (colour, title) choices besides the default orange, which is stored as "".
ACCENT_PRESETS: tuple[tuple[str, str], ...] = (
    ("#2563eb", "آبی"),
    ("#16a34a", "سبز"),
    ("#9333ea", "بنفش"),
    ("#db2777", "صورتی"),
    ("#0d9488", "فیروزه‌ای"),
    ("#dc2626", "قرمز"),
    ("#ca8a04", "طلایی"),
)
_PRESET_COLOURS = frozenset(colour for colour, _ in ACCENT_PRESETS)
# Profile photos are saved by bot_appearance as uploads/<prefix>_bot_tg_photo_<hex>.jpg.
PHOTO_PATTERN = re.compile(r"^uploads/[A-Za-z0-9_-]{1,96}\.jpg$")
PUBLIC_MEDIA_PREFIX = "/media/"


@dataclass(frozen=True, slots=True)
class MiniBrand:
    """``accent`` "" keeps the Mini App's default colour; ``logo_url`` "" shows the name's initial."""

    name: str
    tagline: str
    logo_url: str
    accent: str
    notice: str

    def payload(self) -> dict[str, str]:
        return asdict(self)


def normalize_accent(value: str | None) -> str:
    """One of ``ACCENT_PRESETS`` (lower-case), or "" for the default colour; anything else is refused."""
    text = (value or "").strip().lower()
    if text and text not in _PRESET_COLOURS:
        raise ValueError("رنگ برند را از رنگ‌های آماده انتخاب کنید")
    return text


def normalize_notice(value: str | None) -> str:
    """One short plain-text notice; line breaks become spaces so it stays one banner."""
    text = " ".join((value or "").split())
    if len(text) > NOTICE_MAX:
        raise ValueError(f"اطلاعیهٔ مینی‌اپ حداکثر {NOTICE_MAX} کاراکتر است")
    return text


def _clip(value: str | None, limit: int) -> str:
    return " ".join((value or "").split())[:limit]


def _logo_url(value: str | None) -> str:
    path = (value or "").strip()
    return PUBLIC_MEDIA_PREFIX + path if PHOTO_PATTERN.fullmatch(path) else ""


def _stored_accent(value: str | None) -> str:
    # A colour that is not a preset (a custom colour saved before presets only, or a hand edit)
    # falls back to the default instead of failing the launch.
    try:
        return normalize_accent(value)
    except ValueError:
        return ""


def brand_from_settings(ui: Mapping[str, str], *, name: str) -> MiniBrand:
    return MiniBrand(
        name=_clip(name, NAME_MAX),
        tagline=_clip(ui.get(CAPTION_SETTING), CAPTION_MAX),
        logo_url=_logo_url(ui.get(PHOTO_SETTING)),
        accent=_stored_accent(ui.get(ACCENT_SETTING)),
        notice=_clip(ui.get(NOTICE_SETTING), NOTICE_MAX),
    )


def platform_brand_name(ui: Mapping[str, str]) -> str:
    """The platform bot's Telegram name, else the shop title shown in the bot menu."""
    return (ui.get(NAME_SETTING) or "").strip() or (ui.get(PLATFORM_TITLE_SETTING) or "").strip()


async def platform_brand(session: AsyncSession) -> MiniBrand:
    ui = await get_all_settings(session)
    return brand_from_settings(ui, name=platform_brand_name(ui))


async def shop_brand(session: AsyncSession, *, shop_id: int, name: str) -> MiniBrand:
    """``name`` comes from ``shop_display_name``: the platform's shop title must never leak in."""
    ui = await get_all_settings(session, reseller_id=int(shop_id))
    return brand_from_settings(ui, name=name)


async def save_mini_brand(
    session: AsyncSession, *, shop_id: int | None, accent: str | None, notice: str | None
) -> None:
    """Validate and store the Mini App's own brand settings for one shop (None = platform)."""
    values = {ACCENT_SETTING: normalize_accent(accent), NOTICE_SETTING: normalize_notice(notice)}
    await set_settings_bulk(session, values, reseller_id=shop_id)
