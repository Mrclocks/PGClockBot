"""PasarGuard inbounds and panel admins in the bot (read-only; changes stay in the web panel)."""
from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.pg_entries import PG_ADMINS_CALLBACK, PG_INBOUNDS_CALLBACK
from app.bot.tg_utils import safe_edit_text
from app.db.models import BotUser
from app.services.bot_pg_page_authz import authorize_bot_pg_page
from app.services.formatting import format_message
from app.services.redact import user_safe_error

logger = logging.getLogger(__name__)
router = Router(name="admin_pg_inbounds")

BACK_CALLBACK = "adm:pg"
LIST_LIMIT = 40


def inbound_tags(raw: object) -> list[str]:
    """Tag of every inbound, whether the panel lists names or objects."""
    tags: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            tags.append(item)
        elif isinstance(item, dict):
            tags.append(str(item.get("tag") or item.get("name") or item.get("id") or ""))
    return [t for t in tags if t]


def admin_line(admin: dict) -> str:
    name = html.escape(str(admin.get("username") or "—"))
    role = admin.get("role") if isinstance(admin.get("role"), dict) else {}
    parts = [str(role.get("name") or "")] if role.get("name") else []
    if admin.get("is_sudo"):
        parts.append("دسترسی کامل")
    if admin.get("status"):
        parts.append(str(admin["status"]))
    detail = f" — {html.escape(' · '.join(parts))}" if parts else ""
    return f"• <code>{name}</code>{detail}"


async def _show(
    callback: CallbackQuery, session: AsyncSession | None, db_user: BotUser, *, page: str, owner_only: bool,
    title: str, is_reseller_bot: bool, reseller_profile_id: int | None, reseller_owner_id: int | None,
) -> None:
    gate = await authorize_bot_pg_page(
        session, db_user=db_user, page=page, owner_only=owner_only, callback_data=callback.data,
        is_reseller_bot=is_reseller_bot, reseller_profile_id=reseller_profile_id, reseller_owner_id=reseller_owner_id,
    )
    if not gate.allowed or gate.pg_client is None:
        await callback.answer(gate.user_message, show_alert=True)
        return
    try:
        if page == "pg_inbounds":
            lines = [f"• <code>{html.escape(tag)}</code>" for tag in inbound_tags(await gate.pg_client.get_inbounds())]
        else:
            lines = [admin_line(a) for a in await gate.pg_client.get_admins() if isinstance(a, dict)]
    except Exception as exc:  # noqa: BLE001 - the panel may be down; staff gets a safe message
        logger.warning("bot pg page %s failed", page, exc_info=True)
        await callback.answer(user_safe_error(exc, fallback="خطا در ارتباط با پاسارگارد"), show_alert=True)
        return
    await callback.answer()
    body = "\n".join(lines[:LIST_LIMIT]) or "موردی پیدا نشد."
    if len(lines) > LIST_LIMIT:
        body += f"\n… و {len(lines) - LIST_LIMIT} مورد دیگر (در وب‌پنل)"
    if callback.message:
        markup = InlineKeyboardMarkup(inline_keyboard=[[kb.back_button(None, BACK_CALLBACK)]])
        await safe_edit_text(callback.message, format_message(title, body), reply_markup=markup)


@router.callback_query(F.data == PG_INBOUNDS_CALLBACK)
async def show_inbounds(
    callback: CallbackQuery, db_user: BotUser, session: AsyncSession | None = None,
    is_reseller_bot: bool = False, reseller_profile_id: int | None = None, reseller_owner_id: int | None = None,
) -> None:
    await _show(
        callback, session, db_user, page="pg_inbounds", owner_only=False, title="📡 اینباندها",
        is_reseller_bot=is_reseller_bot, reseller_profile_id=reseller_profile_id, reseller_owner_id=reseller_owner_id,
    )


@router.callback_query(F.data == PG_ADMINS_CALLBACK)
async def show_admins(
    callback: CallbackQuery, db_user: BotUser, session: AsyncSession | None = None,
    is_reseller_bot: bool = False, reseller_profile_id: int | None = None, reseller_owner_id: int | None = None,
) -> None:
    await _show(
        callback, session, db_user, page="pg_admins", owner_only=True, title="🛡 ادمین‌های پنل",
        is_reseller_bot=is_reseller_bot, reseller_profile_id=reseller_profile_id, reseller_owner_id=reseller_owner_id,
    )
