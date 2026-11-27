"""Multi-support contacts stored as JSON in settings.support_contacts."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.button_icons import ICON_KEY, keep_stored_icons, set_icon
from app.services.button_styles import serialize_item_button_style
from app.services.users import get_setting, set_setting

SETTING_KEY = "support_contacts"

_STYLE_UNSET = object()
_ICON_UNSET = object()

_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _sanitize_contact_title(raw: str) -> str:
    """Strip HTML/attribute breakout characters from staff-facing titles."""
    s = (raw or "").strip()
    if not s:
        return ""
    # Defense in depth for panel DOM editors (innerHTML / attributes).
    s = re.sub(r'[<>"\'`]', "", s)
    return s.strip()[:80]


def normalize_telegram_handle(raw: str) -> str:
    """Normalize to @username, numeric id, or keep absolute URL."""
    s = (raw or "").strip()
    if not s:
        return ""
    if s.startswith(("http://", "https://", "tg://")):
        return s
    lower = s.lower()
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if lower.startswith(prefix):
            rest = s.split("/", 2)[-1] if "://" in s else s[len("t.me/") :]
            rest = rest.split("?")[0].split("/")[0].lstrip("@")
            return f"@{rest}" if rest and not rest.isdigit() else rest
    s = s.lstrip("@").strip()
    if s.isdigit():
        return s
    return f"@{s}" if s else ""


def support_chat_url(raw: str) -> str | None:
    """Build a Telegram deep-link / t.me URL for url buttons."""
    s = normalize_telegram_handle(raw)
    if not s:
        return None
    if s.startswith(("http://", "https://", "tg://")):
        return s
    if s.isdigit():
        return f"tg://user?id={s}"
    uname = s.lstrip("@")
    if not uname:
        return None
    return f"https://t.me/{uname}"


def validate_telegram(raw: str) -> str | None:
    """Return error message or None if ok."""
    s = normalize_telegram_handle(raw)
    if not s:
        return "آیدی یا یوزرنیم تلگرام الزامی است"
    if s.startswith(("http://", "https://", "tg://")):
        return None
    if s.isdigit():
        if len(s) < 5:
            return "آیدی عددی نامعتبر است"
        return None
    uname = s.lstrip("@")
    if not _USERNAME_RE.match(uname):
        return "یوزرنیم نامعتبر است (مثال: @support_bot)"
    return None


def parse_support_contacts(raw: str | None) -> list[dict[str, Any]]:
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
        title = _sanitize_contact_title(str(item.get("title") or ""))
        telegram = normalize_telegram_handle(str(item.get("telegram") or ""))
        if not title or not telegram:
            continue
        cid = str(item.get("id") or _new_id())
        try:
            sort = int(item.get("sort", i))
        except (TypeError, ValueError):
            sort = i
        enabled = item.get("enabled", True)
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() in {"1", "true", "yes", "on"}
        entry = {
            "id": cid,
            "title": title,
            "telegram": telegram,
            "sort": sort,
            "enabled": bool(enabled),
            **serialize_item_button_style(item.get("button_style")),
        }
        out.append(set_icon(entry, item.get(ICON_KEY)))
    out.sort(key=lambda x: (x["sort"], x["title"]))
    return out


def dump_support_contacts(contacts: list[dict[str, Any]]) -> str:
    clean = []
    for i, c in enumerate(contacts):
        title = _sanitize_contact_title(str(c.get("title") or ""))
        telegram = normalize_telegram_handle(str(c.get("telegram") or ""))
        if not title or not telegram:
            continue
        try:
            sort_val = int(c.get("sort", i))
        except (TypeError, ValueError):
            sort_val = i
        entry = {
            "id": str(c.get("id") or _new_id()),
            "title": title,
            "telegram": telegram,
            "sort": sort_val,
            "enabled": bool(c.get("enabled", True)),
            **serialize_item_button_style(c.get("button_style")),
        }
        clean.append(set_icon(entry, c.get(ICON_KEY)))
    clean.sort(key=lambda x: (x["sort"], x["title"]))
    return json.dumps(clean, ensure_ascii=False)


def active_support_contacts(contacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in contacts if c.get("enabled", True) and support_chat_url(c.get("telegram") or "")]


async def get_support_contacts(
    session: AsyncSession,
    *,
    reseller_id: int | None = None,
) -> list[dict[str, Any]]:
    raw = await get_setting(session, SETTING_KEY, reseller_id=reseller_id)
    return parse_support_contacts(raw)


async def save_support_contacts(
    session: AsyncSession,
    contacts: list[dict[str, Any]],
    *,
    reseller_id: int | None = None,
) -> None:
    await set_setting(session, SETTING_KEY, dump_support_contacts(contacts), reseller_id=reseller_id)


async def save_submitted_support_contacts(
    session: AsyncSession,
    contacts: list[dict[str, Any]],
    *,
    reseller_id: int | None = None,
) -> None:
    """Save the web editor's list; each contact keeps the premium icon set for it in the bot."""
    stored = await get_support_contacts(session, reseller_id=reseller_id)
    await save_support_contacts(session, keep_stored_icons(contacts, stored), reseller_id=reseller_id)


def contacts_from_form_json(raw: str | None) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Parse bulk editor JSON. Empty rows skipped; partial rows return an error."""
    text = (raw or "").strip()
    if not text:
        return [], None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None, "داده پشتیبان‌ها نامعتبر است"
    if not isinstance(data, list):
        return None, "داده پشتیبان‌ها نامعتبر است"
    out: list[dict[str, Any]] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        title = _sanitize_contact_title(str(item.get("title") or ""))
        telegram_raw = str(item.get("telegram") or "").strip()
        if not title and not telegram_raw:
            continue
        if not title:
            return None, "عنوان پشتیبان الزامی است"
        err = validate_telegram(telegram_raw)
        if err:
            return None, err
        try:
            sort = int(item.get("sort", i))
        except (TypeError, ValueError):
            sort = i
        enabled = item.get("enabled", True)
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() in {"1", "true", "yes", "on"}
        out.append(
            {
                "id": str(item.get("id") or _new_id()),
                "title": title,
                "telegram": normalize_telegram_handle(telegram_raw),
                "sort": sort,
                "enabled": bool(enabled),
                **serialize_item_button_style(item.get("button_style")),
            }
        )
    out.sort(key=lambda x: (x["sort"], x["title"]))
    return out, None


async def upsert_support_contact(
    session: AsyncSession,
    *,
    contact_id: str | None,
    title: str,
    telegram: str,
    sort: int = 0,
    enabled: bool = True,
    button_style: object = _STYLE_UNSET,
    icon_custom_emoji_id: object = _ICON_UNSET,
    reseller_id: int | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Add or change one contact; the stored premium icon stays unless a new one is given."""
    err = validate_telegram(telegram)
    title = _sanitize_contact_title(title or "")
    if not title:
        return None, "عنوان الزامی است"
    if err:
        return None, err
    style_patch: dict[str, str] = {}
    if button_style is not _STYLE_UNSET:
        style_patch = serialize_item_button_style(button_style)
    contacts = await get_support_contacts(session, reseller_id=reseller_id)
    if contact_id:
        found = None
        for c in contacts:
            if c["id"] == contact_id:
                found = c
                break
        if not found:
            return None, "پشتیبان یافت نشد"
        found["title"] = title
        found["telegram"] = normalize_telegram_handle(telegram)
        found["sort"] = sort
        found["enabled"] = enabled
        if button_style is not _STYLE_UNSET:
            found.pop("button_style", None)
            found.update(style_patch)
        if icon_custom_emoji_id is not _ICON_UNSET:
            set_icon(found, icon_custom_emoji_id)
        await save_support_contacts(session, contacts, reseller_id=reseller_id)
        return found, None
    item = {
        "id": _new_id(),
        "title": title,
        "telegram": normalize_telegram_handle(telegram),
        "sort": sort if sort else (max((c["sort"] for c in contacts), default=-1) + 1),
        "enabled": enabled,
        **style_patch,
    }
    if icon_custom_emoji_id is not _ICON_UNSET:
        set_icon(item, icon_custom_emoji_id)
    contacts.append(item)
    await save_support_contacts(session, contacts, reseller_id=reseller_id)
    return item, None


async def delete_support_contact(
    session: AsyncSession,
    contact_id: str,
    *,
    reseller_id: int | None = None,
) -> bool:
    contacts = await get_support_contacts(session, reseller_id=reseller_id)
    new = [c for c in contacts if c["id"] != contact_id]
    if len(new) == len(contacts):
        return False
    await save_support_contacts(session, new, reseller_id=reseller_id)
    return True
