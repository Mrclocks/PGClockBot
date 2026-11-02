"""The brand the web panel's header shows: a name, a logo and one accent colour, per shop.

The platform Owner sets the platform brand; a shop sets its own. A signed-in person always
sees the brand of the shop they work for, falling back to the platform's and then to the
product default, so one shop's brand never reaches another shop's staff. Settings are read
for one explicit scope at a time (never through the bot's shop context).

The colour is one of a fixed set of presets. The panel's stylesheet is generated from those
presets (``BRAND_PRESET_CSS``), so a saved value can only ever select a known preset and no
user text reaches CSS. The logo is re-encoded to a small PNG on upload and served from a file
name that carries its owner's scope, which is checked again on every read.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
import unicodedata
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from markupsafe import Markup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DATA_DIR
from app.db.models import ResellerSetting, Setting
from app.services.platform_identity import is_explicit_owner_staff
from app.services.shop_scope import ShopScopeError, resolve_shop_scope_id
from app.services.users import set_settings_bulk

logger = logging.getLogger(__name__)

NAME_KEY = "panel_brand_name"
LOGO_KEY = "panel_brand_logo"
COLOR_KEY = "panel_brand_color"
BRAND_KEYS = (NAME_KEY, LOGO_KEY, COLOR_KEY)

DEFAULT_NAME = "MrClockBot"
DEFAULT_LOGO_URL = "/static/logo-64.png"
NAME_MAX = 40
LOGO_MAX_BYTES = 2 * 1024 * 1024
LOGO_EDGE = 128
LOGO_MAX_PIXELS = 25_000_000
LOGO_FORMATS = frozenset({"PNG", "JPEG", "WEBP"})
MEDIA_PREFIX = "/media/"
UPLOADS_SUBDIR = "uploads"
_LOGO_PATH = re.compile(r"^uploads/brand_(p|r[0-9]{1,12})_[0-9a-f]{12}\.png$")
_CACHE_TTL_SECONDS = 30.0
_CACHE_MAX_ENTRIES = 1024
PLATFORM_SCOPE = 0


class BrandError(ValueError):
    """A brand input refused with a message that is safe to show to the person who sent it."""


@dataclass(frozen=True, slots=True)
class BrandPreset:
    """A selectable accent: the two shades each theme needs for fills and for text."""

    key: str
    label: str
    dark_brand: str
    dark_text: str
    light_brand: str
    light_text: str


# The orange default is the stylesheet's own and has no entry. Red and amber are left out: the
# panel already uses them for errors and warnings.
PRESETS: tuple[BrandPreset, ...] = (
    BrandPreset("blue", "آبی", "#3b82f6", "#60a5fa", "#2563eb", "#1d4ed8"),
    BrandPreset("green", "سبز", "#22c55e", "#4ade80", "#16a34a", "#15803d"),
    BrandPreset("purple", "بنفش", "#a855f7", "#c084fc", "#9333ea", "#7e22ce"),
    BrandPreset("pink", "صورتی", "#ec4899", "#f472b6", "#db2777", "#be185d"),
    BrandPreset("teal", "فیروزه‌ای", "#14b8a6", "#2dd4bf", "#0d9488", "#0f766e"),
)
DEFAULT_COLOR_LABEL = "پیش‌فرض (نارنجی)"
DEFAULT_COLOR_HEX = "#f97316"
PRESET_KEYS = frozenset(preset.key for preset in PRESETS)


def _theme_block(preset: BrandPreset, *, light: bool) -> str:
    """Variables a preset overrides for one theme.

    ``--brand-base`` (washes, logo tile) keeps the lighter hue in both themes and ``--brand-deep`` the
    darker one, as the orange default does. ``--accent*`` are re-declared because custom properties
    computed on <html> do not follow a later override on the same or a nested element.
    """
    brand = preset.light_brand if light else preset.dark_brand
    text = preset.light_text if light else preset.dark_text
    soft = "var(--brand) 10%" if light else "var(--brand-base) 12%"
    return (
        f"--brand:{brand};--brand-fg:{text};--brand-base:{preset.dark_brand};--brand-deep:{preset.light_brand};"
        f"--accent:var(--brand);--accent-fg:var(--brand-fg);"
        f"--accent-soft:color-mix(in srgb,{soft},transparent);"
    )


def _preset_rules(preset: BrandPreset) -> str:
    key = preset.key
    return (
        f'html[data-brand="{key}"],[data-brand="{key}"]{{{_theme_block(preset, light=False)}}}'
        f'html[data-theme="light"][data-brand="{key}"],html[data-theme="light"] [data-brand="{key}"]'
        f"{{{_theme_block(preset, light=True)}}}"
    )


def preset_css() -> str:
    """Generated stylesheet text for every preset; built only from the constants above."""
    return "".join(_preset_rules(preset) for preset in PRESETS)


# Constant built from the table above (hex colours and fixed keys), never from user input.
BRAND_PRESET_CSS = Markup(preset_css())


@dataclass(frozen=True, slots=True)
class PanelBrand:
    """What the header shows. ``color`` is a preset key or "" for the default orange."""

    name: str = DEFAULT_NAME
    logo_url: str = DEFAULT_LOGO_URL
    color: str = ""
    has_custom_logo: bool = False


DEFAULT_BRAND = PanelBrand()


def clean_brand_name(raw: object) -> str:
    """The name as stored: one line, no invisible or direction-changing characters, bounded."""
    text = " ".join(str(raw or "").split())
    text = "".join(ch for ch in text if unicodedata.category(ch) not in {"Cc", "Cf", "Zl", "Zp"})
    if len(text) > NAME_MAX:
        raise BrandError(f"نام برند حداکثر {NAME_MAX} نویسه باشد")
    return text


def normalize_color(raw: object) -> str:
    key = str(raw or "").strip().lower()
    if key and key not in PRESET_KEYS:
        raise BrandError("رنگ برند نامعتبر است")
    return key


def _stored_color(raw: object) -> str:
    """A saved colour that is not a preset reads as the default."""
    key = str(raw or "").strip().lower()
    return key if key in PRESET_KEYS else ""


def _scope_tag(scope: int) -> str:
    return "p" if scope == PLATFORM_SCOPE else f"r{int(scope)}"


def logo_belongs_to(rel_path: str, scope: int) -> bool:
    """A stored logo path is valid only in the shape this module writes and for its own scope."""
    match = _LOGO_PATH.match(rel_path or "")
    return bool(match) and match.group(1) == _scope_tag(scope)


class LogoError(BrandError):
    """A logo upload refused for a reason that is safe to show the person who uploaded it."""


def process_logo(data: bytes) -> bytes:
    """Re-encode an uploaded picture as a square 128 px PNG with no metadata, or raise LogoError.

    SVG and every other format are refused: a vector file can carry script, and a decoded raster
    re-saved from pixels cannot. The pixel count is checked from the header, before decoding.
    """
    from PIL import Image, ImageOps, UnidentifiedImageError

    if not data:
        raise LogoError("فایل لوگو خالی است")
    if len(data) > LOGO_MAX_BYTES:
        raise LogoError("حجم لوگو حداکثر ۲ مگابایت باشد")
    try:
        with Image.open(BytesIO(data)) as image:
            if image.format not in LOGO_FORMATS:
                raise LogoError("لوگو باید PNG، JPG یا WebP باشد")
            width, height = image.size
            if width < 1 or height < 1 or width * height > LOGO_MAX_PIXELS:
                raise LogoError("ابعاد لوگو بیش از حد بزرگ است")
            image.load()
            rgba = image.convert("RGBA")
    except LogoError:
        raise
    except (ValueError, UnidentifiedImageError, OSError, Image.DecompressionBombError, SyntaxError) as exc:
        raise LogoError("فایل لوگو معتبر نیست") from exc
    fitted = ImageOps.contain(rgba, (LOGO_EDGE, LOGO_EDGE), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (LOGO_EDGE, LOGO_EDGE), (0, 0, 0, 0))
    canvas.paste(fitted, ((LOGO_EDGE - fitted.width) // 2, (LOGO_EDGE - fitted.height) // 2), fitted)
    out = BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    return out.getvalue()


def _uploads_dir() -> Path:
    path = DATA_DIR / UPLOADS_SUBDIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def _logo_file(rel_path: str) -> Path | None:
    """The file behind a stored logo path, only if it sits directly inside the uploads folder."""
    if not _LOGO_PATH.match(rel_path or ""):
        return None
    uploads = _uploads_dir().resolve()
    candidate = (DATA_DIR / rel_path).resolve()
    return candidate if candidate.parent == uploads else None


def _write_logo(png: bytes, scope: int) -> str:
    rel = f"{UPLOADS_SUBDIR}/brand_{_scope_tag(scope)}_{secrets.token_hex(6)}.png"
    target = _logo_file(rel)
    if target is None:
        raise BrandError("مسیر لوگو نامعتبر است")
    target.write_bytes(png)
    return rel


def _remove_logo(rel_path: str, scope: int) -> None:
    if not logo_belongs_to(rel_path, scope):
        return
    target = _logo_file(rel_path)
    if target is not None:
        target.unlink(missing_ok=True)


_CACHE: dict[int, tuple[float, dict[str, str]]] = {}


def invalidate_brand_cache(scope: int | None = None) -> None:
    if scope is None:
        _CACHE.clear()
    else:
        _CACHE.pop(int(scope), None)


async def _stored_values(session: AsyncSession, scope: int, *, fresh: bool = False) -> dict[str, str]:
    """The three raw settings of one scope, read for that scope only (``fresh`` skips the cache)."""
    now = time.monotonic()
    hit = _CACHE.get(scope)
    if hit and not fresh and now - hit[0] < _CACHE_TTL_SECONDS:
        return hit[1]
    if scope == PLATFORM_SCOPE:
        rows = await session.execute(select(Setting.key, Setting.value).where(Setting.key.in_(BRAND_KEYS)))
    else:
        rows = await session.execute(
            select(ResellerSetting.key, ResellerSetting.value).where(
                ResellerSetting.reseller_user_id == scope, ResellerSetting.key.in_(BRAND_KEYS)
            )
        )
    values = {key: str(value or "") for key, value in rows.all()}
    if len(_CACHE) >= _CACHE_MAX_ENTRIES:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[scope] = (now, values)
    return values


def _brand_from(values: dict[str, str], scope: int) -> dict[str, str]:
    name = ""
    try:
        name = clean_brand_name(values.get(NAME_KEY))
    except ValueError:
        name = ""
    logo = values.get(LOGO_KEY, "")
    return {
        "name": name,
        "logo": logo if logo_belongs_to(logo, scope) else "",
        "color": _stored_color(values.get(COLOR_KEY)),
    }


async def load_brand(session: AsyncSession, shop_id: int | None) -> PanelBrand:
    """The brand for one shop (None = platform): its own values, else the platform's, else the default."""
    scope = PLATFORM_SCOPE if shop_id is None else int(shop_id)
    own = _brand_from(await _stored_values(session, scope), scope)
    platform = own if scope == PLATFORM_SCOPE else _brand_from(await _stored_values(session, PLATFORM_SCOPE), PLATFORM_SCOPE)
    # Name, logo and colour each fall back on their own: a shop that only set a colour keeps the platform's logo.
    name = own["name"] or platform["name"] or DEFAULT_NAME
    logo = own["logo"] or platform["logo"]
    color = own["color"] or platform["color"]
    return PanelBrand(
        name=name,
        logo_url=f"{MEDIA_PREFIX}{logo}" if logo else DEFAULT_LOGO_URL,
        color=color,
        has_custom_logo=bool(logo),
    )


async def load_own_values(session: AsyncSession, shop_id: int | None) -> dict[str, str]:
    """What this scope itself saved (no fallback), for the settings form."""
    scope = PLATFORM_SCOPE if shop_id is None else int(shop_id)
    return _brand_from(await _stored_values(session, scope), scope)


def staff_brand_scope(staff: dict) -> int | None:
    """The shop whose brand a signed-in person sees; the platform's for anyone without a shop."""
    try:
        return resolve_shop_scope_id(staff)
    except ShopScopeError:
        return None


async def brand_for_staff(session: AsyncSession, staff: dict) -> PanelBrand:
    """Header brand for a signed-in person; any failure shows the default instead of breaking the page."""
    try:
        return await load_brand(session, staff_brand_scope(staff))
    except Exception:
        logger.warning("panel brand load failed", exc_info=True)
        return DEFAULT_BRAND


def editable_scope(staff: dict, *, shop_settings_allowed: bool) -> tuple[bool, int | None]:
    """(may edit, scope): the explicit Owner edits the platform brand; shop staff with shop settings, their own."""
    if staff.get("role") == "admin":
        return (True, None) if is_explicit_owner_staff(staff) else (False, None)
    if not shop_settings_allowed:
        return False, None
    try:
        return True, resolve_shop_scope_id(staff)
    except ShopScopeError:
        return False, None


async def save_brand(
    session: AsyncSession,
    *,
    shop_id: int | None,
    name: object,
    color: object,
    logo_bytes: bytes | None = None,
    clear_logo: bool = False,
) -> None:
    """Validate and store one scope's brand; the new logo file exists before the settings point at it."""
    scope = PLATFORM_SCOPE if shop_id is None else int(shop_id)
    clean_name = clean_brand_name(name)
    clean_color = normalize_color(color)
    previous = _brand_from(await _stored_values(session, scope, fresh=True), scope)["logo"]
    new_logo: str | None = None
    if logo_bytes:
        png = await asyncio.to_thread(process_logo, logo_bytes)
        new_logo = await asyncio.to_thread(_write_logo, png, scope)
    payload = {NAME_KEY: clean_name, COLOR_KEY: clean_color}
    if new_logo is not None:
        payload[LOGO_KEY] = new_logo
    elif clear_logo:
        payload[LOGO_KEY] = ""
    try:
        await set_settings_bulk(session, payload, reseller_id=shop_id)
    except Exception:
        if new_logo is not None:
            await asyncio.to_thread(_remove_logo, new_logo, scope)
        raise
    invalidate_brand_cache(scope)
    if previous and (new_logo is not None or clear_logo):
        await asyncio.to_thread(_remove_logo, previous, scope)
    logger.info("panel brand saved scope=%s logo_changed=%s", scope, new_logo is not None or clear_logo)
