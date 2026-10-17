"""Connection guides — admin-editable inline buttons for users / resellers.

Stored as JSON in settings.connection_guides (shop-scoped via reseller settings).
Each item: title, body text, audience, button style, optional deep-link flag.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.button_styles import parse_item_button_style_raw
from app.services.guide_media import (
    MAX_MEDIA_PER_GUIDE,
    GuideMediaError,
    delete_unused_files,
    keep_stored_media,
    new_media_id,
    parse_links,
    parse_media,
    stored_files,
)
from app.services.users import get_setting, set_setting

SETTING_KEY = "connection_guides"
AUDIENCES = frozenset({"user", "reseller"})

_STYLE_UNSET = object()


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _sanitize_title(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    s = re.sub(r'[<>"\'`]', "", s)
    return s.strip()[:64]


def _sanitize_body(raw: str) -> str:
    # Keep HTML-ish rich text from admin; cap length for Telegram safety.
    return (raw or "").strip()[:4000]


def parse_connection_guides(raw: str | None) -> list[dict[str, Any]]:
    if not (raw or "").strip():
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        title = _sanitize_title(str(item.get("title") or ""))
        body = _sanitize_body(str(item.get("body") or item.get("text") or ""))
        audience = str(item.get("audience") or "user").strip().lower()
        if audience not in AUDIENCES:
            audience = "user"
        enabled = item.get("enabled")
        if enabled is None:
            enabled = item.get("active", True)
        try:
            sort = int(item.get("sort", i))
        except (TypeError, ValueError):
            sort = i
        deep_link = bool(item.get("deep_link") or item.get("deep_link_enabled"))
        gid = str(item.get("id") or "").strip() or _new_id()
        style_raw = item.get("style", item.get("button_style", _STYLE_UNSET))
        # Accept legacy nested {"button_style": "..."} from earlier drafts.
        if isinstance(style_raw, dict):
            style_raw = style_raw.get("button_style", _STYLE_UNSET)
        style = (
            parse_item_button_style_raw(style_raw)
            if style_raw is not _STYLE_UNSET
            else None
        )
        entry: dict[str, Any] = {
            "id": gid[:32],
            "title": title,
            "body": body,
            "audience": audience,
            "enabled": bool(enabled),
            "sort": sort,
            "deep_link": deep_link,
        }
        if style is not None:
            # Store plain Telegram style token (""|primary|success|danger), not a nested dict.
            entry["style"] = style
        media = parse_media(item.get("media"))
        if media:
            entry["media"] = media
        links = parse_links(item.get("links"))
        if links:
            entry["links"] = links
        if title or body or media:
            out.append(entry)
    out.sort(key=lambda x: (int(x.get("sort") or 0), str(x.get("title") or "")))
    return out


def serialize_connection_guides(items: list[dict[str, Any]] | None) -> str:
    clean = parse_connection_guides(json.dumps(items or []))
    return json.dumps(clean, ensure_ascii=False)


def default_connection_guides() -> list[dict[str, Any]]:
    """Persian starter guides — admin can edit/remove."""
    return [
        {
            "id": "v2box",
            "title": "V2Box",
            "body": (
                "۱) اپ V2Box را نصب کنید.\n"
                "۲) دکمه افزودن اشتراک / + را بزنید.\n"
                "۳) لینک اشتراک را وارد کنید یا از دکمه «باز کردن لینک» استفاده کنید.\n"
                "۴) اتصال را فعال کنید."
            ),
            "audience": "user",
            "enabled": True,
            "sort": 0,
            "deep_link": True,
            "style": "primary",
        },
        {
            "id": "streisand",
            "title": "Streisand",
            "body": (
                "۱) اپ Streisand را نصب کنید.\n"
                "۲) اشتراک را از طریق لینک اضافه کنید.\n"
                "۳) پروفایل را انتخاب و وصل شوید."
            ),
            "audience": "user",
            "enabled": True,
            "sort": 1,
            "deep_link": True,
            "style": "success",
        },
        {
            "id": "reseller_panel",
            "title": "اتصال به پنل نماینده",
            "body": (
                "۱) لینک پنل نمایندگی را در مرورگر باز کنید.\n"
                "۲) با نام کاربری و رمزی که برایتان ارسال شده وارد شوید.\n"
                "۳) از بخش سرویس‌ها اشتراک بسازید یا وضعیت را ببینید."
            ),
            "audience": "reseller",
            "enabled": True,
            "sort": 0,
            "deep_link": False,
            "style": "primary",
        },
    ]


async def get_connection_guides(
    session: AsyncSession,
    *,
    reseller_id: int | None = None,
) -> list[dict[str, Any]]:
    raw = await get_setting(session, SETTING_KEY, "", reseller_id=reseller_id)
    items = parse_connection_guides(raw)
    if items:
        return items
    # Seed defaults into memory only — persist on first admin save.
    return default_connection_guides()


async def save_connection_guides(
    session: AsyncSession,
    items: list[dict[str, Any]],
    *,
    reseller_id: int | None = None,
) -> list[dict[str, Any]]:
    clean = parse_connection_guides(json.dumps(items or []))
    await set_setting(session, SETTING_KEY, serialize_connection_guides(clean), reseller_id=reseller_id)
    return clean


async def serialize_submitted_guides(session: AsyncSession, raw: str, *, reseller_id: int | None) -> str:
    """Guides posted by the settings form, keeping each guide's stored media as it is."""
    try:
        submitted = json.loads(raw) if (raw or "").strip() else []
    except json.JSONDecodeError:
        submitted = []
    if not isinstance(submitted, list):
        submitted = []
    stored = parse_connection_guides(await get_setting(session, SETTING_KEY, "", reseller_id=reseller_id))
    submitted = [item for item in submitted if isinstance(item, dict)]
    return serialize_connection_guides(keep_stored_media(submitted, stored))


def guides_for_audience(
    items: list[dict[str, Any]],
    audience: str,
    *,
    enabled_only: bool = True,
) -> list[dict[str, Any]]:
    aud = "reseller" if audience == "reseller" else "user"
    out = []
    for g in items:
        if str(g.get("audience") or "user") != aud:
            continue
        if enabled_only and not g.get("enabled", True):
            continue
        out.append(g)
    return out


def guide_import_url(sub_url: str | None) -> str | None:
    """Telegram url-button target — only http(s) subscription links are safe to open."""
    url = (sub_url or "").strip()
    if url.startswith(("http://", "https://")):
        return url
    return None


async def _load_guide(session: AsyncSession, guide_id: str, *, reseller_id: int | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    items = parse_connection_guides(await get_setting(session, SETTING_KEY, "", reseller_id=reseller_id))
    guide = next((g for g in items if str(g.get("id")) == str(guide_id)), None)
    if guide is None:
        raise GuideMediaError("اول آموزش را ذخیره کنید، بعد فایل اضافه کنید.")
    return items, guide


async def add_guide_media(
    session: AsyncSession,
    guide_id: str,
    *,
    kind: str,
    file_name: str | None = None,
    bot_file_id: tuple[int, str] | None = None,
    reseller_id: int | None,
) -> list[dict[str, Any]]:
    """Append one stored file (web upload) or one Telegram file (bot upload) to a guide."""
    items, guide = await _load_guide(session, guide_id, reseller_id=reseller_id)
    media = list(guide.get("media") or [])
    if len(media) >= MAX_MEDIA_PER_GUIDE:
        raise GuideMediaError(f"هر آموزش حداکثر {MAX_MEDIA_PER_GUIDE} فایل دارد.")
    entry: dict[str, Any] = {"id": new_media_id(), "kind": kind}
    if file_name:
        entry["file"] = file_name
    if bot_file_id:
        entry["tg"] = {str(bot_file_id[0]): bot_file_id[1]}
    guide["media"] = [*media, entry]
    saved = await save_connection_guides(session, items, reseller_id=reseller_id)
    return next(g for g in saved if str(g.get("id")) == str(guide_id)).get("media") or []


async def remove_guide_media(
    session: AsyncSession, guide_id: str, media_id: str, *, reseller_id: int | None
) -> tuple[str | None, list[dict[str, Any]]]:
    """Drop one media item; returns its stored file name (for the caller to delete) and the rest."""
    items, guide = await _load_guide(session, guide_id, reseller_id=reseller_id)
    media = list(guide.get("media") or [])
    removed = next((m for m in media if m.get("id") == media_id), None)
    if removed is None:
        raise GuideMediaError("این فایل پیدا نشد.")
    guide["media"] = [m for m in media if m is not removed]
    await save_connection_guides(session, items, reseller_id=reseller_id)
    return removed.get("file"), guide["media"]


async def move_guide_media(
    session: AsyncSession, guide_id: str, media_id: str, *, step: int, reseller_id: int | None
) -> list[dict[str, Any]]:
    """Move one media item ``step`` places (−1 earlier, +1 later) inside its album."""
    items, guide = await _load_guide(session, guide_id, reseller_id=reseller_id)
    media = list(guide.get("media") or [])
    index = next((i for i, m in enumerate(media) if m.get("id") == media_id), None)
    if index is None:
        raise GuideMediaError("این فایل پیدا نشد.")
    target = max(0, min(len(media) - 1, index + step))
    media.insert(target, media.pop(index))
    guide["media"] = media
    saved = await save_connection_guides(session, items, reseller_id=reseller_id)
    return next(g for g in saved if str(g.get("id")) == str(guide_id)).get("media") or []


async def guide_media_files(session: AsyncSession, *, reseller_id: int | None) -> set[str]:
    """File names this scope's guides use; a preview may serve only these."""
    items = parse_connection_guides(await get_setting(session, SETTING_KEY, "", reseller_id=reseller_id))
    return stored_files(items)


async def prune_guide_media(session: AsyncSession, *, reseller_id: int | None) -> int:
    """Delete this scope's stored files that no guide uses any more."""
    used = await guide_media_files(session, reseller_id=reseller_id)
    return await asyncio.to_thread(delete_unused_files, used, reseller_id=reseller_id)
