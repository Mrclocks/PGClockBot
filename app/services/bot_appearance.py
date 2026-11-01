"""Telegram Bot API appearance (name, about, short description, photo)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.types import BufferedInputFile, InputProfilePhotoStatic

log = logging.getLogger(__name__)

# Shared with bot_profile_photo, which copies a photo set in @BotFather for the Mini App logo.
PHOTO_SOURCE_SETTING = "bot_tg_photo_source"
PANEL_PHOTO_SOURCE = "panel"

NAME_MAX = 64
DESCRIPTION_MAX = 512
SHORT_DESCRIPTION_MAX = 120


@dataclass
class BotAppearance:
    name: str = ""
    username: str = ""
    description: str = ""
    short_description: str = ""
    photo_url: str = ""
    ok: bool = False
    error: str = ""
    extras: dict[str, Any] = field(default_factory=dict)


def clip(text: str | None, limit: int) -> str:
    return (text or "").strip()[:limit]


def validate_appearance_form(
    *,
    name: str,
    description: str,
    short_description: str,
) -> str | None:
    if len(name) > NAME_MAX:
        return f"نام ربات حداکثر {NAME_MAX} کاراکتر است"
    # description = empty-chat text (متن وسط); short_description = profile caption (کپشن)
    if len(description) > DESCRIPTION_MAX:
        return f"متن وسط صفحه حداکثر {DESCRIPTION_MAX} کاراکتر است"
    if len(short_description) > SHORT_DESCRIPTION_MAX:
        return f"کپشن / درباره حداکثر {SHORT_DESCRIPTION_MAX} کاراکتر است"
    return None


async def fetch_appearance(token: str, *, local_photo: str = "") -> BotAppearance:
    token = (token or "").strip()
    if not token:
        return BotAppearance(error="توکن ربات تنظیم نشده", photo_url=local_photo)

    bot = Bot(token=token)
    try:
        me = await bot.get_me()
        name_obj = await bot.get_my_name()
        desc_obj = await bot.get_my_description()
        short_obj = await bot.get_my_short_description()
        name = (getattr(name_obj, "name", None) or me.first_name or "").strip()
        return BotAppearance(
            name=name,
            username=(me.username or "").strip(),
            description=(getattr(desc_obj, "description", None) or "").strip(),
            short_description=(getattr(short_obj, "short_description", None) or "").strip(),
            photo_url=local_photo,
            ok=True,
        )
    except Exception as exc:
        log.warning("fetch_appearance failed: %s", exc)
        return BotAppearance(error=f"خواندن از تلگرام ناموفق: {exc}", photo_url=local_photo)
    finally:
        await bot.session.close()


async def _sync_platform_menu_button(bot: Bot) -> None:
    """Mini App «Open» on the platform bot when HTTPS is ready, else cleared; best effort."""
    # app.bot imports services; importing at call time keeps this module free of the cycle.
    from app.bot.chat_menu import sync_telegram_menu_button

    try:
        await sync_telegram_menu_button(bot, allow_miniapp=True)
    except Exception:
        log.warning("sync menu button after appearance failed", exc_info=True)


async def apply_appearance(
    token: str,
    *,
    name: str,
    description: str,
    short_description: str,
    photo_bytes: bytes | None = None,
    photo_filename: str = "photo.jpg",
    remove_photo: bool = False,
    is_platform_bot: bool,
) -> BotAppearance:
    """Push appearance fields to Telegram Bot API.

    Only the platform bot's «Open» button is pointed at the platform Mini App here. A shop
    bot's button follows its own Mini App switches (``reseller_bots.resync_shop_menu_buttons``):
    the platform address on a shop bot fails every request, because the shop bot signs
    initData with its own token.
    """
    token = (token or "").strip()
    if not token:
        return BotAppearance(error="توکن ربات تنظیم نشده")

    name = clip(name, NAME_MAX)
    description = clip(description, DESCRIPTION_MAX)
    short_description = clip(short_description, SHORT_DESCRIPTION_MAX)

    err = validate_appearance_form(
        name=name,
        description=description,
        short_description=short_description,
    )
    if err:
        return BotAppearance(error=err)

    bot = Bot(token=token)
    try:
        me = await bot.get_me()
        await bot.set_my_name(name=name)
        await bot.set_my_description(description=description)
        await bot.set_my_short_description(short_description=short_description)
        await bot.delete_my_commands()
        if is_platform_bot:
            await _sync_platform_menu_button(bot)
        if remove_photo and not photo_bytes:
            try:
                await bot.remove_my_profile_photo()
            except Exception as exc:
                log.warning("remove_my_profile_photo: %s", exc)
                return BotAppearance(error=f"حذف عکس پروفایل ناموفق: {exc}")
        if photo_bytes:
            fname = (photo_filename or "photo.jpg").lower()
            if not fname.endswith((".jpg", ".jpeg")):
                fname = Path(fname).stem + ".jpg"
            photo = InputProfilePhotoStatic(
                photo=BufferedInputFile(photo_bytes, filename=fname)
            )
            try:
                await bot.set_my_profile_photo(photo=photo)
            except Exception as exc:
                log.warning("set_my_profile_photo: %s", exc)
                return BotAppearance(
                    error=f"آپلود عکس پروفایل ناموفق (فقط JPG): {exc}",
                    name=name,
                    username=(me.username or "").strip(),
                    description=description,
                    short_description=short_description,
                )

        return BotAppearance(
            name=name,
            username=(me.username or "").strip(),
            description=description,
            short_description=short_description,
            ok=True,
        )
    except Exception as exc:
        log.warning("apply_appearance failed: %s", exc)
        return BotAppearance(error=f"اعمال در تلگرام ناموفق: {exc}")
    finally:
        await bot.session.close()


def appearance_to_form_dict(app: BotAppearance, *, local_photo: str = "") -> dict[str, str]:
    return {
        "bot_tg_name": app.name or "",
        "bot_tg_description": app.description or "",
        "bot_tg_short_description": app.short_description or "",
        "bot_tg_photo": local_photo or app.photo_url or "",
        "bot_username": app.username or "",
    }


async def load_appearance_context(
    session,
    *,
    token: str,
    reseller_id: int | None = None,
    fallback_username: str = "",
) -> dict[str, Any]:
    """Build template context for appearance tab (Telegram + local cache)."""
    from app.services.users import get_all_settings

    values = await get_all_settings(session, reseller_id=reseller_id)
    local_photo = (values.get("bot_tg_photo") or "").strip()
    fetched = await fetch_appearance(token, local_photo=local_photo)
    if fetched.ok:
        form = appearance_to_form_dict(fetched, local_photo=local_photo)
        # Prefer Telegram values; keep local photo for panel preview
        return {
            "appearance": form,
            "appearance_sync_ok": True,
            "appearance_sync_err": "",
        }
    # Fallback to cached settings when Telegram unreachable
    form = {
        "bot_tg_name": values.get("bot_tg_name") or "",
        "bot_tg_description": values.get("bot_tg_description") or "",
        "bot_tg_short_description": values.get("bot_tg_short_description") or "",
        "bot_tg_photo": local_photo,
        "bot_username": fallback_username or "",
    }
    return {
        "appearance": form,
        "appearance_sync_ok": False,
        "appearance_sync_err": fetched.error or "خواندن از تلگرام ناموفق",
    }


async def save_appearance_from_form(
    session,
    form,
    *,
    token: str,
    reseller_id: int | None = None,
    upload_prefix: str = "",
) -> tuple[bool, str]:
    """Validate form, push to Telegram, persist local cache. Returns (ok, message)."""
    import uuid

    from starlette.datastructures import UploadFile

    from app.config import DATA_DIR

    name = clip(str(form.get("bot_tg_name") or ""), NAME_MAX)
    description = clip(str(form.get("bot_tg_description") or ""), DESCRIPTION_MAX)
    short_description = clip(str(form.get("bot_tg_short_description") or ""), SHORT_DESCRIPTION_MAX)
    remove_photo = str(form.get("bot_tg_photo_clear") or "") in {"1", "on", "true", "yes"}

    err = validate_appearance_form(
        name=name,
        description=description,
        short_description=short_description,
    )
    if err:
        return False, err

    photo_bytes: bytes | None = None
    photo_filename = "photo.jpg"
    upload = form.get("bot_tg_photo")
    if isinstance(upload, UploadFile) and upload.filename:
        fname = upload.filename.lower()
        ext = Path(fname).suffix
        if ext not in {".jpg", ".jpeg"}:
            return False, "عکس پروفایل باید JPG باشد"
        content = await upload.read(5 * 1024 * 1024 + 1)
        if len(content) > 5 * 1024 * 1024:
            return False, "حجم عکس پروفایل بیش از ۵ مگابایت است"
        if content:
            photo_bytes = content
            photo_filename = Path(fname).name
            remove_photo = False

    result = await apply_appearance(
        token,
        name=name,
        description=description,
        short_description=short_description,
        photo_bytes=photo_bytes,
        photo_filename=photo_filename,
        remove_photo=remove_photo,
        is_platform_bot=reseller_id is None,
    )
    if not result.ok:
        return False, result.error or "اعمال در تلگرام ناموفق"

    from app.services.users import set_settings_bulk

    payload: dict[str, str] = {
        "bot_tg_name": name,
        "bot_tg_description": description,
        "bot_tg_short_description": short_description,
        "bot_cmd_start": "",
        "bot_cmd_help": "",
        "welcome_image": "",
    }

    if remove_photo and not photo_bytes:
        payload["bot_tg_photo"] = ""
        payload[PHOTO_SOURCE_SETTING] = ""
    elif photo_bytes:
        uploads = DATA_DIR / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        prefix = upload_prefix or ("r" + str(reseller_id) if reseller_id else "main")
        dest_name = f"{prefix}_bot_tg_photo_{uuid.uuid4().hex[:10]}.jpg"
        dest = uploads / dest_name
        dest.write_bytes(photo_bytes)
        payload["bot_tg_photo"] = f"uploads/{dest_name}"
        # A panel upload wins over the copy the Mini App logo job takes from Telegram.
        payload[PHOTO_SOURCE_SETTING] = PANEL_PHOTO_SOURCE

    await set_settings_bulk(session, payload, reseller_id=reseller_id)

    return True, "هویت ربات در تلگرام اعمال شد"
