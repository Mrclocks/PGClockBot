"""PasarGuard hosts in the bot: list, card, enable/disable and delete (web /pg/hosts, without the forms).

Every step goes through the shared PG object gate (Principal -> capability -> object scope), which
fails closed on a shop bot, a missing capability or an object outside the actor's own list.
"""
from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.pg_entries import PG_HOSTS_CALLBACK
from app.bot.tg_utils import safe_edit_text
from app.db.models import BotUser
from app.services.bot_pg_object_authz import (
    BotPgObjectAction,
    BotPgObjectGate,
    authorize_bot_pg_object_op,
    list_scoped_pg_objects,
)
from app.services.formatting import format_message, info_block, kv_line
from app.services.redact import user_safe_error

logger = logging.getLogger(__name__)
router = Router(name="admin_pg_hosts")

LIST_CALLBACK = PG_HOSTS_CALLBACK
BACK_CALLBACK = "adm:pg"
LIST_LIMIT = 25
ID = r"(\d{1,12})"
ADDRESS_PREVIEW = 3


async def _gate(
    callback: CallbackQuery, session: AsyncSession | None, db_user: BotUser, action: BotPgObjectAction, *,
    object_id: int | None, is_reseller_bot: bool, reseller_profile_id: int | None, reseller_owner_id: int | None,
) -> BotPgObjectGate:
    gate = await authorize_bot_pg_object_op(
        session, db_user=db_user, kind="hosts", action=action,
        callback_data=callback.data, object_id=object_id,
        is_reseller_bot=is_reseller_bot, reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
    )
    if not gate.allowed:
        await callback.answer(gate.user_message, show_alert=True)
    return gate


def _addresses(host: dict) -> str:
    raw = host.get("address")
    items = raw if isinstance(raw, list) else [raw] if raw else []
    shown = [html.escape(str(a)) for a in items[:ADDRESS_PREVIEW]]
    more = f" (+{len(items) - ADDRESS_PREVIEW})" if len(items) > ADDRESS_PREVIEW else ""
    return "، ".join(shown) + more if shown else "—"


def _state(host: dict) -> str:
    return "غیرفعال" if host.get("is_disabled") else "فعال"


def _card_text(host: dict) -> str:
    port = host.get("port")
    lines = [
        kv_line("🔢", "شناسه", f"#{int(host.get('id') or 0)}"),
        kv_line("🏷", "نام", html.escape(str(host.get("remark") or "—"))),
        kv_line("🌐", "آدرس", _addresses(host)),
        kv_line("🔌", "پورت", html.escape(str(port)) if port else "—"),
        kv_line("📡", "اینباند", html.escape(str(host.get("inbound_tag") or "—"))),
        kv_line("📌", "وضعیت", _state(host)),
    ]
    return format_message("🌐 هاست", info_block(lines))


def _card_markup(host: dict) -> InlineKeyboardMarkup:
    host_id = int(host.get("id") or 0)
    toggle = "▶️ فعال‌سازی" if host.get("is_disabled") else "⏸ غیرفعال‌سازی"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle, callback_data=f"adm:pg:htog:{host_id}")],
        [InlineKeyboardButton(text="🗑 حذف", callback_data=f"adm:pg:hdelask:{host_id}")],
        [kb.back_button(None, LIST_CALLBACK)],
    ])


async def _show_card(callback: CallbackQuery, host: dict) -> None:
    if callback.message:
        await safe_edit_text(callback.message, _card_text(host), reply_markup=_card_markup(host))


async def _render_hosts(callback: CallbackQuery, gate: BotPgObjectGate) -> None:
    hosts = await list_scoped_pg_objects(gate)
    rows = [
        [InlineKeyboardButton(
            text=f"#{h.get('id')} {str(h.get('remark') or '')[:28]} — {_state(h)}"[:60],
            callback_data=f"adm:pg:h:{int(h.get('id') or 0)}",
        )]
        for h in hosts[:LIST_LIMIT]
        if int(h.get("id") or 0) > 0
    ]
    rows.append([kb.back_button(None, BACK_CALLBACK)])
    body = f"{len(hosts)} هاست" if hosts else "هاستی پیدا نشد."
    if callback.message:
        await safe_edit_text(callback.message, format_message("🌐 هاست‌ها", body), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == LIST_CALLBACK)
async def show_hosts(
    callback: CallbackQuery, db_user: BotUser, session: AsyncSession | None = None,
    is_reseller_bot: bool = False, reseller_profile_id: int | None = None, reseller_owner_id: int | None = None,
) -> None:
    gate = await _gate(
        callback, session, db_user, "list", object_id=None, is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id, reseller_owner_id=reseller_owner_id,
    )
    if not gate.allowed:
        return
    try:
        await _render_hosts(callback, gate)
    except Exception as exc:  # noqa: BLE001 - the panel may be down; staff gets a safe message
        logger.warning("bot pg hosts list failed", exc_info=True)
        await callback.answer(user_safe_error(exc, fallback="خطا در ارتباط با پاسارگارد"), show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.regexp(rf"^adm:pg:h:{ID}$"))
async def show_host(
    callback: CallbackQuery, db_user: BotUser, session: AsyncSession | None = None,
    is_reseller_bot: bool = False, reseller_profile_id: int | None = None, reseller_owner_id: int | None = None,
) -> None:
    gate = await _gate(
        callback, session, db_user, "read", object_id=int(callback.data.split(":")[3]),
        is_reseller_bot=is_reseller_bot, reseller_profile_id=reseller_profile_id, reseller_owner_id=reseller_owner_id,
    )
    if not gate.allowed or gate.pg_object is None:
        return
    await callback.answer()
    await _show_card(callback, gate.pg_object)


@router.callback_query(F.data.regexp(rf"^adm:pg:htog:{ID}$"))
async def toggle_host(
    callback: CallbackQuery, db_user: BotUser, session: AsyncSession | None = None,
    is_reseller_bot: bool = False, reseller_profile_id: int | None = None, reseller_owner_id: int | None = None,
) -> None:
    host_id = int(callback.data.split(":")[3])
    gate = await _gate(
        callback, session, db_user, "update", object_id=host_id,
        is_reseller_bot=is_reseller_bot, reseller_profile_id=reseller_profile_id, reseller_owner_id=reseller_owner_id,
    )
    if not gate.allowed or gate.pg_client is None or gate.pg_object is None:
        return
    try:
        await gate.pg_client.set_host_disabled(host_id, not bool(gate.pg_object.get("is_disabled")))
        host = await gate.pg_client.get_host(host_id)
    except Exception as exc:  # noqa: BLE001 - panel errors are shown as a safe message
        logger.warning("bot pg host toggle failed host=%s", host_id, exc_info=True)
        await callback.answer(user_safe_error(exc, fallback="خطا در ارتباط با پاسارگارد"), show_alert=True)
        return
    await callback.answer("وضعیت هاست تغییر کرد ✅", show_alert=True)
    if isinstance(host, dict):
        await _show_card(callback, {**host, "id": host.get("id", host_id)})


@router.callback_query(F.data.regexp(rf"^adm:pg:hdelask:{ID}$"))
async def ask_delete_host(
    callback: CallbackQuery, db_user: BotUser, session: AsyncSession | None = None,
    is_reseller_bot: bool = False, reseller_profile_id: int | None = None, reseller_owner_id: int | None = None,
) -> None:
    host_id = int(callback.data.split(":")[3])
    gate = await _gate(
        callback, session, db_user, "delete", object_id=host_id,
        is_reseller_bot=is_reseller_bot, reseller_profile_id=reseller_profile_id, reseller_owner_id=reseller_owner_id,
    )
    if not gate.allowed or gate.pg_object is None:
        return
    await callback.answer()
    name = html.escape(str(gate.pg_object.get("remark") or host_id))
    text = format_message("⚠️ حذف هاست", f"هاست «{name}» از پاسارگارد حذف می‌شود و قابل بازگشت نیست.")
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ حذف هاست", callback_data=f"adm:pg:hdel:{host_id}")],
        [kb.back_button(None, f"adm:pg:h:{host_id}")],
    ])
    if callback.message:
        await safe_edit_text(callback.message, text, reply_markup=markup)


@router.callback_query(F.data.regexp(rf"^adm:pg:hdel:{ID}$"))
async def delete_host(
    callback: CallbackQuery, db_user: BotUser, session: AsyncSession | None = None,
    is_reseller_bot: bool = False, reseller_profile_id: int | None = None, reseller_owner_id: int | None = None,
) -> None:
    host_id = int(callback.data.split(":")[3])
    gate = await _gate(
        callback, session, db_user, "delete", object_id=host_id,
        is_reseller_bot=is_reseller_bot, reseller_profile_id=reseller_profile_id, reseller_owner_id=reseller_owner_id,
    )
    if not gate.allowed or gate.pg_client is None:
        return
    try:
        await gate.pg_client.delete_host(host_id)
    except Exception as exc:  # noqa: BLE001 - panel errors are shown as a safe message
        logger.warning("bot pg host delete failed host=%s", host_id, exc_info=True)
        await callback.answer(user_safe_error(exc, fallback="خطا در ارتباط با پاسارگارد"), show_alert=True)
        return
    await callback.answer("هاست حذف شد ✅", show_alert=True)
    list_gate = await authorize_bot_pg_object_op(
        session, db_user=db_user, kind="hosts", action="list", callback_data=LIST_CALLBACK,
        is_reseller_bot=is_reseller_bot, reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
    )
    if list_gate.allowed:
        await _render_hosts(callback, list_gate)
