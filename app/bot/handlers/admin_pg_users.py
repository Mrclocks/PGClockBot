"""Admin PasarGuard VPN users — list/search/create/edit/actions (panel parity)."""

from __future__ import annotations

import html
import re
import time
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.auth import (
    can_platform_pg_action,
    can_platform_pg_page,
    filtered_pg_reply_keyboard,
)
from app.bot.auth import is_platform_admin as _is_admin
from app.bot.tg_utils import safe_edit_text
from app.db.models import BotUser
from app.services.formatting import format_message, service_card
from app.services.pasarguard import (
    PasarGuardError,
    as_list,
    build_user_create_payload,
    build_user_modify_payload,
    get_pg,
    user_group_ids,
    user_subscription_url,
)
from app.services.pg_quota import PgQuotaError, assert_can_create_user

router = Router(name="admin_pg_users")


def _err_msg(exc: Exception) -> str:
    """Persian-friendly PasarGuard / local error for bot replies."""
    if isinstance(exc, PasarGuardError):
        return exc.user_message(fallback="خطا در ارتباط با پاسارگارد")
    return str(exc) or "خطا"

async def _require_users(db_user: BotUser, callback=None, message=None, *, action: str | None = None) -> bool:
    """Legacy platform-admin page gate (kept for source contracts).

    Migrated PG-user handlers must use ``_pg_user_gate`` — never this function
    as the final authorization decision.
    """
    if not _is_admin(db_user):
        if callback is not None:
            await callback.answer("ادمین نیستید", show_alert=True)
        elif message is not None:
            await message.answer("ادمین نیستید.")
        return False
    if not await can_platform_pg_page(db_user, "pg_users"):
        if callback is not None:
            await callback.answer("به کاربران پاسارگارد دسترسی ندارید", show_alert=True)
        elif message is not None:
            await message.answer("به کاربران پاسارگارد دسترسی ندارید.")
        return False
    if action and not await can_platform_pg_action(db_user, "users", action):
        if callback is not None:
            await callback.answer("اجازه این عمل را ندارید", show_alert=True)
        elif message is not None:
            await message.answer("اجازه این عمل را ندارید.")
        return False
    return True


async def _pg_user_gate(
    db_user: BotUser,
    *,
    action: str,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
    callback=None,
    message=None,
    pg_user_id: int | None = None,
    callback_data: str | None = None,
    notify: bool = True,
):
    """Principal + AuthzContext gate (not ``_is_admin``)."""
    from app.services.bot_pg_user_authz import authorize_bot_pg_user_op

    data = callback_data
    if data is None and callback is not None:
        data = getattr(callback, "data", None)
    gate = await authorize_bot_pg_user_op(
        session,
        db_user=db_user,
        action=action,  # type: ignore[arg-type]
        callback_data=data,
        pg_user_id=pg_user_id,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
    )
    if not gate.allowed and notify:
        if callback is not None:
            await callback.answer(gate.user_message, show_alert=True)
        elif message is not None:
            await message.answer(gate.user_message)
    return gate


def _filter_staff_templates(items, staff) -> list:
    from app.services.plans_catalog import filter_templates_for_staff

    if not isinstance(items, list):
        return []
    # Match web panel / list_scoped_pg_catalog (trust own-client when ready).
    return filter_templates_for_staff(items, staff or {})


def _filter_staff_groups(items, staff) -> list:
    from app.services.plans_catalog import filter_groups_for_staff

    if not isinstance(items, list):
        return []
    return filter_groups_for_staff(items, staff or {})


_PAGE_RE = re.compile(r"^adm:pg:users:p:(\d+)$")
_SETTPL_RE = re.compile(r"^adm:pg:settpl:(\d+)$")
_TOGGRP_RE = re.compile(r"^adm:pg:toggrp:(\d+)$")
_EDGRP_RE = re.compile(r"^adm:pg:edgrp:(\d+)$")


def _unused_owner_client_import_anchor():
    """Source contract: this module still references get_pg(); live Owner client is the Principal bridge."""
    return get_pg()



PAGE_SIZE = 10
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,32}$")


class PgUserStates(StatesGroup):
    search = State()
    create_username = State()
    create_gb = State()
    create_days = State()
    edit_username = State()
    edit_gb = State()
    edit_days = State()


def _user_label(u: dict) -> str:
    uname = str(u.get("username") or "—")
    status = str(u.get("status") or "")
    mark = ""
    if status in {"disabled", "limited", "expired"}:
        mark = "⛔ "
    elif status == "on_hold":
        mark = "⏸ "
    uid = u.get("id")
    return f"{mark}{uname}"[:48] if uid is not None else uname[:48]


def _user_actions_kb(uid: int, *, back: str = "adm:pg:users") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="♻️ ریست حجم", callback_data=f"adm:pg:reset:{uid}"),
                InlineKeyboardButton(text="🚫 غیرفعال", callback_data=f"adm:pg:dis:{uid}"),
            ],
            [
                InlineKeyboardButton(text="✅ فعال", callback_data=f"adm:pg:en:{uid}"),
                InlineKeyboardButton(text="🔏 باطل ساب", callback_data=f"adm:pg:rev:{uid}"),
            ],
            [
                InlineKeyboardButton(text="🔗 لینک ساب", callback_data=f"adm:pg:u:{uid}:link"),
                InlineKeyboardButton(text="✏️ ویرایش", callback_data=f"adm:pg:u:{uid}:edit"),
            ],
            [
                InlineKeyboardButton(text="🗑 حذف", callback_data=f"adm:pg:u:{uid}:delask"),
            ],
            [InlineKeyboardButton(text="⬅️ بازگشت", callback_data=back)],
        ]
    )


def _edit_menu_kb(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👤 تغییر نام کاربری", callback_data=f"adm:pg:u:{uid}:ed:name")],
            [InlineKeyboardButton(text="📦 تغییر حجم (گیگ)", callback_data=f"adm:pg:u:{uid}:ed:gb")],
            [InlineKeyboardButton(text="📅 تغییر مدت (روز)", callback_data=f"adm:pg:u:{uid}:ed:days")],
            [InlineKeyboardButton(text="📁 تغییر گروه‌ها", callback_data=f"adm:pg:u:{uid}:ed:grps")],
            [InlineKeyboardButton(text="⬅️ کارت کاربر", callback_data=f"adm:pg:u:{uid}")],
        ]
    )


async def _fetch_users_page(
    page: int,
    *,
    username: str | None = None,
    pg,
) -> tuple[list[dict], int | None]:
    page = max(0, int(page))
    params: dict[str, Any] = {"offset": page * PAGE_SIZE, "limit": PAGE_SIZE}
    q = (username or "").strip()
    if q:
        params["username"] = q
    data = await pg.get_users(**params)
    if isinstance(data, list):
        users = [u for u in data if isinstance(u, dict)]
        total = None
    else:
        users = as_list(data, "users")
        total = None
        if isinstance(data, dict):
            for key in ("total", "count", "total_count"):
                if data.get(key) is not None:
                    try:
                        total = int(data[key])
                        break
                    except (TypeError, ValueError):
                        pass
    return users, total


async def _render_users_list(
    target: Message,
    *,
    page: int = 0,
    query: str | None = None,
    edit: bool = True,
    gate,
) -> None:
    from app.services.bot_pg_user_authz import list_scoped_pg_users

    query = (query or "").strip() or None
    try:
        users, total = await list_scoped_pg_users(
            gate, page=page, username=query, page_size=PAGE_SIZE
        )
    except Exception as e:
        text = format_message("❌ خطا", _err_msg(e))
        markup = None  # navigation is on reply keyboard (pg_reply_keyboard)
        if edit:
            await safe_edit_text(target, text, reply_markup=markup)
        else:
            await target.answer(text, reply_markup=markup)
        return

    rows: list[list[InlineKeyboardButton]] = []
    buttons: list[InlineKeyboardButton] = []
    for u in users:
        uid = u.get("id")
        if uid is None:
            continue
        buttons.append(
            InlineKeyboardButton(
                text=_user_label(u),
                callback_data=f"adm:pg:u:{int(uid)}",
            )
        )
    rows = kb.chunk_buttons(buttons, cols=2)

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ قبل", callback_data=f"adm:pg:users:p:{page - 1}"))
    has_next = False
    if total is not None:
        has_next = (page + 1) * PAGE_SIZE < total
        page_label = f"{page + 1}/{(max(total - 1, 0) // PAGE_SIZE) + 1}"
    else:
        has_next = len(users) >= PAGE_SIZE
        page_label = f"{page + 1}"
    if has_next:
        nav.append(InlineKeyboardButton(text="بعد ▶️", callback_data=f"adm:pg:users:p:{page + 1}"))
    if nav:
        rows.append(nav)

    if query:
        rows.append(
            [InlineKeyboardButton(text="🧹 پاک کردن جستجو", callback_data="adm:pg:users:clear")]
        )
    # Hub actions (search/create/back) live on the reply keyboard — not inline

    title = "👥 کاربران پاسارگارد"
    bits = [f"صفحه {page_label}"]
    if total is not None:
        bits.append(f"جمع: {total}")
    if query:
        bits.append(f"جستجو: <code>{html.escape(query)}</code>")
    body = " · ".join(bits)
    if not users:
        body += "\n\nکاربری یافت نشد."
    else:
        body += f"\n\n{len(users)} کاربر در این صفحه — برای جزئیات انتخاب کنید."

    text = format_message(title, body)
    markup = InlineKeyboardMarkup(inline_keyboard=rows)
    if edit:
        await safe_edit_text(target, text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


async def _show_user_card(
    target: Message,
    uid: int,
    *,
    edit: bool = True,
    notice: str | None = None,
    user: dict | None = None,
    pg=None,
) -> None:
    if user is None:
        if pg is None:
            text = format_message("❌ خطا", "اجازه این عمل را ندارید")
            if edit:
                await safe_edit_text(target, text, reply_markup=None)
            else:
                await target.answer(text, reply_markup=None)
            return
        try:
            user = await pg.get_user_by_id(uid)
        except Exception as e:
            text = format_message("❌ خطا", str(e))
            if edit:
                await safe_edit_text(target, text, reply_markup=None)
            else:
                await target.answer(text, reply_markup=None)
            return
    text = service_card(user if isinstance(user, dict) else {})
    if notice:
        text = f"{notice}\n\n{text}"
    markup = _user_actions_kb(uid)
    if edit:
        await safe_edit_text(target, text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


# ----- PG hub + catalog hints (Principal-gated, not Owner middleware) -----


@router.callback_query(F.data == "adm:pg")
async def adm_pg(
    callback: CallbackQuery,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    """PasarGuard hub for Owner and L1 with migrated PG pages (not platform overview)."""
    from app.bot.auth import bot_may_open_pg_hub, filtered_pg_reply_keyboard

    if not await bot_may_open_pg_hub(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
    ):
        await callback.answer("دسترسی پاسارگارد برای این حساب تعریف نشده", show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            "🖥 <b>عملیات پاسارگارد</b>\n"
            "از کیبورد پایین بخش موردنظر را انتخاب کنید.",
            reply_markup=None,
        )
        try:
            await callback.message.answer(
                "⌨️",
                reply_markup=await filtered_pg_reply_keyboard(
                    db_user,
                    session=session,
                    is_reseller_bot=is_reseller_bot,
                    reseller_profile_id=reseller_profile_id,
                    reseller_owner_id=reseller_owner_id,
                ),
            )
        except Exception:
            pass


@router.callback_query(F.data == "adm:pg:group")
async def adm_pg_group_hint(
    callback: CallbackQuery,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    """Principal-gated groups hint (not ``_is_admin``)."""
    from app.services.bot_pg_catalog_authz import (
        authorize_bot_pg_catalog_op,
        list_scoped_pg_catalog,
    )

    gate = await authorize_bot_pg_catalog_op(
        session,
        db_user=db_user,
        kind="groups",
        action="list",
        callback_data=getattr(callback, "data", None),
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
    )
    if not gate.allowed:
        await callback.answer(gate.user_message, show_alert=True)
        return
    await callback.answer()
    lines = ["📁 <b>گروه‌های پاسارگارد</b>\n"]
    try:
        groups = await list_scoped_pg_catalog(gate, kind="groups")
    except Exception as e:
        groups = []
        lines.append(f"خطا: {_err_msg(e)}")
    if groups:
        for g in groups[:15]:
            if not isinstance(g, dict):
                continue
            gid = g.get("id")
            name = html.escape(str(g.get("name") or gid))
            lines.append(f"#{gid} <b>{name}</b>")
    else:
        lines.append("گروهی در محدوده شما یافت نشد.")
    lines.extend(
        [
            "",
            "ساخت/ویرایش گروه نیاز به انتخاب اینباند دارد.",
            "از وب‌پنل مسیر <code>/pg/groups</code> استفاده کنید.",
            "",
            "مدیریت کاربران از همین ربات: «کاربران».",
        ]
    )
    if callback.message:
        await callback.message.edit_text("\n".join(lines)[:3900], reply_markup=None)


@router.callback_query(F.data == "adm:pg:template")
async def adm_pg_template_hint(
    callback: CallbackQuery,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    """Principal-gated templates hint (not ``_is_admin``)."""
    from app.services.bot_pg_catalog_authz import (
        authorize_bot_pg_catalog_op,
        list_scoped_pg_catalog,
    )

    gate = await authorize_bot_pg_catalog_op(
        session,
        db_user=db_user,
        kind="templates",
        action="list",
        callback_data=getattr(callback, "data", None),
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
    )
    if not gate.allowed:
        await callback.answer(gate.user_message, show_alert=True)
        return
    await callback.answer()
    lines = ["📋 <b>تمپلیت‌های پاسارگارد</b>\n"]
    try:
        templates = await list_scoped_pg_catalog(gate, kind="templates")
    except Exception as e:
        templates = []
        lines.append(f"خطا: {_err_msg(e)}")
    if templates:
        for t in templates[:15]:
            if not isinstance(t, dict):
                continue
            tid = t.get("id")
            name = html.escape(str(t.get("name") or tid))
            lines.append(f"#{tid} <b>{name}</b>")
    else:
        lines.append("تمپلیتی در محدوده شما یافت نشد.")
    lines.extend(
        [
            "",
            "ساخت تمپلیت از وب‌پنل مسیر <code>/pg/templates</code>.",
            "",
            "ساخت کاربر از تمپلیت در ربات: پاسارگارد ← ساخت کاربر.",
        ]
    )
    if callback.message:
        await callback.message.edit_text("\n".join(lines)[:3900], reply_markup=None)


# ----- list / search -----


@router.callback_query(F.data == "adm:pg:users")
@router.callback_query(F.data == "adm:pg:users:clear")
async def pg_users_list(
    callback: CallbackQuery,
    state: FSMContext,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    gate = await _pg_user_gate(
        db_user,
        action="list",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        callback=callback,
    )
    if not gate.allowed:
        return
    await callback.answer()
    await state.update_data(pg_list_q=None, pg_list_page=0)
    if callback.message:
        await _render_users_list(callback.message, page=0, query=None, gate=gate)


@router.callback_query(F.data.startswith("adm:pg:users:p:"))
async def pg_users_page(
    callback: CallbackQuery,
    state: FSMContext,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    gate = await _pg_user_gate(
        db_user,
        action="list",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        callback=callback,
    )
    if not gate.allowed:
        return
    match = _PAGE_RE.match(callback.data or "")
    if match is None:
        await callback.answer("صفحه نامعتبر", show_alert=True)
        return
    page = int(match.group(1))
    await callback.answer()
    data = await state.get_data()
    query = data.get("pg_list_q")
    await state.update_data(pg_list_page=page)
    if callback.message:
        await _render_users_list(callback.message, page=page, query=query, gate=gate)


@router.callback_query(F.data == "adm:pg:search")
async def pg_search_start(
    callback: CallbackQuery,
    state: FSMContext,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    gate = await _pg_user_gate(
        db_user,
        action="search",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        callback=callback,
    )
    if not gate.allowed:
        return
    await callback.answer()
    await state.set_state(PgUserStates.search)
    if callback.message:
        await callback.message.answer(
            "نام کاربری پاسارگارد را برای جستجو بفرستید:\n"
            "(جستجو در لیست فیلتر می‌شود؛ برای انصراف «انصراف» بزنید)",
            reply_markup=kb.cancel_reply(),
        )


@router.message(PgUserStates.search)
async def pg_search_query(
    message: Message,
    state: FSMContext,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    from app.services.bot_pg_user_authz import lookup_scoped_pg_user_by_username

    gate = await _pg_user_gate(
        db_user,
        action="search",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        message=message,
    )
    if not gate.allowed:
        await state.clear()
        return
    if kb.is_cancel_text(message.text):
        await state.clear()
        await message.answer("لغو شد.", reply_markup=await filtered_pg_reply_keyboard(db_user, session=session, is_reseller_bot=is_reseller_bot))
        return
    q = (message.text or "").strip()
    if not q:
        await message.answer("نام کاربری را به‌صورت متن بفرستید.")
        return
    await state.set_state(None)
    await state.update_data(pg_list_q=q, pg_list_page=0)
    exact = await lookup_scoped_pg_user_by_username(gate, q)
    if exact is not None and exact.get("id") is not None:
        await state.clear()
        await _show_user_card(message, int(exact["id"]), edit=False, user=exact)
        return
    await _render_users_list(message, page=0, query=q, edit=False, gate=gate)


# ----- user detail + actions -----


@router.callback_query(F.data.regexp(r"^adm:pg:u:\d+$"))
async def pg_user_detail(
    callback: CallbackQuery,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    """PG user card (read) via OrgPrincipal + AuthzContext."""
    gate = await _pg_user_gate(
        db_user,
        action="read",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        callback=callback,
    )
    if not gate.allowed:
        return
    await callback.answer()
    uid = int(gate.pg_user["id"]) if gate.pg_user and gate.pg_user.get("id") is not None else 0
    if callback.message and uid:
        await _show_user_card(callback.message, uid, user=gate.pg_user)


@router.callback_query(F.data.regexp(r"^adm:pg:u:\d+:link$"))
async def pg_user_link(
    callback: CallbackQuery,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    gate = await _pg_user_gate(
        db_user,
        action="read",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        callback=callback,
    )
    if not gate.allowed:
        return
    uid = int(gate.pg_user["id"]) if gate.pg_user and gate.pg_user.get("id") is not None else 0
    user = gate.pg_user or {}
    url = user_subscription_url(user if isinstance(user, dict) else None)
    if not url:
        await callback.answer("لینک یافت نشد", show_alert=True)
        return
    await callback.answer()
    uname = user.get("username") if isinstance(user, dict) else ""
    await callback.message.answer(
        format_message(
            "🔗 لینک اشتراک",
            f"کاربر: <code>{html.escape(str(uname or uid))}</code>\n\n<code>{html.escape(url)}</code>",
        ),
        reply_markup=_user_actions_kb(uid),
    )


@router.callback_query(F.data.startswith("adm:pg:reset:"))
async def pg_reset(
    callback: CallbackQuery,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    gate = await _pg_user_gate(
        db_user,
        action="reset",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        callback=callback,
    )
    if not gate.allowed:
        return
    uid = int(gate.pg_user["id"]) if gate.pg_user and gate.pg_user.get("id") is not None else 0
    try:
        user = await gate.pg_client.reset_user_by_id(uid)
        await callback.answer("ریست شد ✅", show_alert=True)
        if callback.message:
            if isinstance(user, dict) and user.get("id") is not None:
                text = "♻️ حجم ریست شد\n\n" + service_card(user)
                await safe_edit_text(callback.message, text, reply_markup=_user_actions_kb(uid))
            else:
                await _show_user_card(
                    callback.message, uid, notice="♻️ حجم ریست شد", user=gate.pg_user
                )
    except Exception as e:
        await callback.answer(_err_msg(e), show_alert=True)


@router.callback_query(F.data.startswith("adm:pg:dis:"))
async def pg_dis(
    callback: CallbackQuery,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    """PG user disable via OrgPrincipal + AuthzContext."""
    gate = await _pg_user_gate(
        db_user,
        action="disable",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        callback=callback,
    )
    if not gate.allowed:
        return
    uid = int(gate.pg_user["id"]) if gate.pg_user and gate.pg_user.get("id") is not None else 0
    try:
        user = await gate.pg_client.set_disabled_by_id(uid, True)
        await callback.answer("غیرفعال شد", show_alert=True)
        if callback.message:
            if isinstance(user, dict) and user.get("id") is not None:
                text = "🚫 کاربر غیرفعال شد\n\n" + service_card(user)
                await safe_edit_text(callback.message, text, reply_markup=_user_actions_kb(uid))
            else:
                await _show_user_card(
                    callback.message, uid, notice="🚫 کاربر غیرفعال شد", user=gate.pg_user
                )
    except Exception as e:
        await callback.answer(_err_msg(e), show_alert=True)


@router.callback_query(F.data.startswith("adm:pg:en:"))
async def pg_en(
    callback: CallbackQuery,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    gate = await _pg_user_gate(
        db_user,
        action="enable",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        callback=callback,
    )
    if not gate.allowed:
        return
    uid = int(gate.pg_user["id"]) if gate.pg_user and gate.pg_user.get("id") is not None else 0
    try:
        user = await gate.pg_client.set_disabled_by_id(uid, False)
        await callback.answer("فعال شد", show_alert=True)
        if callback.message:
            if isinstance(user, dict) and user.get("id") is not None:
                text = "✅ کاربر فعال شد\n\n" + service_card(user)
                await safe_edit_text(callback.message, text, reply_markup=_user_actions_kb(uid))
            else:
                await _show_user_card(
                    callback.message, uid, notice="✅ کاربر فعال شد", user=gate.pg_user
                )
    except Exception as e:
        await callback.answer(_err_msg(e), show_alert=True)


@router.callback_query(F.data.startswith("adm:pg:rev:"))
async def pg_rev(
    callback: CallbackQuery,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    gate = await _pg_user_gate(
        db_user,
        action="revoke",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        callback=callback,
    )
    if not gate.allowed:
        return
    uid = int(gate.pg_user["id"]) if gate.pg_user and gate.pg_user.get("id") is not None else 0
    try:
        user = await gate.pg_client.revoke_sub_by_id(uid)
        await callback.answer("ساب باطل شد", show_alert=True)
        if callback.message:
            if isinstance(user, dict) and user.get("id") is not None:
                text = "🔏 سابسکریپشن باطل شد\n\n" + service_card(user)
                await safe_edit_text(callback.message, text, reply_markup=_user_actions_kb(uid))
            else:
                await _show_user_card(
                    callback.message, uid, notice="🔏 سابسکریپشن باطل شد", user=gate.pg_user
                )
    except Exception as e:
        await callback.answer(_err_msg(e), show_alert=True)


@router.callback_query(F.data.regexp(r"^adm:pg:u:\d+:delask$"))
async def pg_user_del_ask(
    callback: CallbackQuery,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    gate = await _pg_user_gate(
        db_user,
        action="delete",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        callback=callback,
    )
    if not gate.allowed:
        return
    uid = int(gate.pg_user["id"]) if gate.pg_user and gate.pg_user.get("id") is not None else 0
    await callback.answer()
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🗑 بله، حذف شود", callback_data=f"adm:pg:u:{uid}:del"),
                InlineKeyboardButton(text="انصراف", callback_data=f"adm:pg:u:{uid}"),
            ]
        ]
    )
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message("⚠️ حذف کاربر", f"کاربر #{uid} برای همیشه حذف شود؟"),
            reply_markup=markup,
        )


@router.callback_query(F.data.regexp(r"^adm:pg:u:\d+:del$"))
async def pg_user_del(
    callback: CallbackQuery,
    state: FSMContext,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    gate = await _pg_user_gate(
        db_user,
        action="delete",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        callback=callback,
    )
    if not gate.allowed:
        return
    uid = int(gate.pg_user["id"]) if gate.pg_user and gate.pg_user.get("id") is not None else 0
    try:
        await gate.pg_client.delete_user_by_id(uid)
        if session is not None:
            from app.services.bot_user_admin import detach_local_services_for_pg_user

            await detach_local_services_for_pg_user(session, uid, commit=True)
        await callback.answer("حذف شد", show_alert=True)
        list_gate = await _pg_user_gate(
            db_user,
            action="list",
            session=session,
            is_reseller_bot=is_reseller_bot,
            reseller_profile_id=reseller_profile_id,
            reseller_owner_id=reseller_owner_id,
            callback=callback,
            notify=False,
        )
        data = await state.get_data()
        if callback.message and list_gate.allowed:
            await _render_users_list(
                callback.message,
                page=int(data.get("pg_list_page") or 0),
                query=data.get("pg_list_q"),
                gate=list_gate,
            )
    except Exception as e:
        await callback.answer(_err_msg(e), show_alert=True)


# ----- create -----


@router.callback_query(F.data == "adm:pg:create")
async def pg_create_menu(
    callback: CallbackQuery,
    state: FSMContext,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    gate = await _pg_user_gate(
        db_user,
        action="create",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        callback=callback,
    )
    if not gate.allowed:
        return
    await callback.answer()
    await state.clear()
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📋 از تمپلیت", callback_data="adm:pg:create:tpl")],
            [InlineKeyboardButton(text="🛠 سفارشی (گروه + حجم + روز)", callback_data="adm:pg:create:custom")],
            [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="adm:pg:users")],
        ]
    )
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message("➕ ساخت کاربر", "نوع ساخت را انتخاب کنید:"),
            reply_markup=markup,
        )


@router.callback_query(F.data == "adm:pg:create:tpl")
async def pg_create_tpl_pick(
    callback: CallbackQuery,
    state: FSMContext,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    gate = await _pg_user_gate(
        db_user,
        action="create",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        callback=callback,
    )
    if not gate.allowed:
        return
    await callback.answer()
    # Never leave create_username active with a null template id (stale inline
    # «از تمپلیت» while mid-create previously poisoned FSM → wrong error).
    await state.clear()
    await state.update_data(pg_create_mode="template", pg_template_id=None, pg_selected_groups=[])
    try:
        templates = await gate.pg_client.get_user_templates_simple()
    except Exception:
        templates = []
    templates = _filter_staff_templates(templates, gate.staff)
    rows: list[list[InlineKeyboardButton]] = []
    for t in templates[:25]:
        if not isinstance(t, dict):
            continue
        tid = t.get("id")
        if tid is None:
            continue
        name = t.get("name") or f"تمپلیت {tid}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"#{tid} — {name}"[:60],
                    callback_data=f"adm:pg:settpl:{int(tid)}",
                )
            ]
        )
    if not rows:
        rows.append(
            [InlineKeyboardButton(text="تمپلیتی نیست", callback_data="adm:pg:create")]
        )
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="adm:pg:create")])
    if callback.message:
        await safe_edit_text(
            callback.message,
            "📋 تمپلیت را انتخاب کنید:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )


@router.callback_query(F.data.startswith("adm:pg:settpl:"))
async def pg_create_tpl_chosen(
    callback: CallbackQuery,
    state: FSMContext,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    gate = await _pg_user_gate(
        db_user,
        action="create",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        callback=callback,
    )
    if not gate.allowed:
        return
    match = _SETTPL_RE.match(callback.data or "")
    if match is None:
        await callback.answer("اجازه این عمل را ندارید", show_alert=True)
        return
    tid = int(match.group(1))
    from app.services.bot_pg_catalog_authz import catalog_template_allowed

    if not catalog_template_allowed(gate.staff, tid):
        await callback.answer("اجازه این عمل را ندارید", show_alert=True)
        return
    await callback.answer()
    await state.update_data(pg_create_mode="template", pg_template_id=tid)
    await state.set_state(PgUserStates.create_username)
    if callback.message:
        await callback.message.answer(
            f"تمپلیت #{tid} انتخاب شد.\nنام کاربری جدید را بفرستید (۳–۳۲ حرف انگلیسی/عدد/_):",
            reply_markup=kb.cancel_reply(),
        )


@router.callback_query(F.data == "adm:pg:create:custom")
async def pg_create_custom_groups(
    callback: CallbackQuery,
    state: FSMContext,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    gate = await _pg_user_gate(
        db_user,
        action="create",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        callback=callback,
    )
    if not gate.allowed:
        return
    await callback.answer()
    await state.clear()
    await state.update_data(pg_create_mode="custom", pg_template_id=None, pg_selected_groups=[])
    await _show_create_group_picker(callback, state, pg=gate.pg_client, staff=gate.staff)


async def _show_create_group_picker(
    callback: CallbackQuery, state: FSMContext, *, pg, staff=None
) -> None:
    data = await state.get_data()
    selected = [int(x) for x in (data.get("pg_selected_groups") or [])]
    edit_uid = data.get("pg_edit_uid")
    groups = data.get("pg_groups_cache")
    if not isinstance(groups, list):
        try:
            groups = await pg.get_groups_simple()
        except Exception:
            groups = []
        groups = _filter_staff_groups(groups, staff)
        await state.update_data(pg_groups_cache=groups)
    rows: list[list[InlineKeyboardButton]] = []
    prefix = "adm:pg:edgrp" if edit_uid else "adm:pg:toggrp"
    for g in groups[:25]:
        if not isinstance(g, dict):
            continue
        gid = g.get("id")
        if gid is None:
            continue
        gid = int(gid)
        mark = "✅ " if gid in selected else ""
        name = g.get("name") or f"گروه {gid}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark}#{gid} — {name}"[:60],
                    callback_data=f"{prefix}:{gid}",
                )
            ]
        )
    done_cb = "adm:pg:edgrpdone" if edit_uid else "adm:pg:grpdone"
    back_cb = f"adm:pg:u:{edit_uid}:edit" if edit_uid else "adm:pg:create"
    if rows:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"✅ تأیید ({len(selected)})",
                    callback_data=done_cb,
                )
            ]
        )
    else:
        rows.append([InlineKeyboardButton(text="گروهی نیست", callback_data=back_cb)])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=back_cb)])
    text = (
        "📁 گروه‌ها را انتخاب کنید (چندتایی):\n"
        f"انتخاب‌شده: {', '.join(str(x) for x in selected) or '—'}"
    )
    if callback.message:
        await safe_edit_text(
            callback.message,
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )


@router.callback_query(F.data.startswith("adm:pg:toggrp:"))
async def pg_create_toggrp(
    callback: CallbackQuery,
    state: FSMContext,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    gate = await _pg_user_gate(
        db_user,
        action="create",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        callback=callback,
    )
    if not gate.allowed:
        return
    match = _TOGGRP_RE.match(callback.data or "")
    if match is None:
        await callback.answer("اجازه این عمل را ندارید", show_alert=True)
        return
    gid = int(match.group(1))
    data = await state.get_data()
    selected = [int(x) for x in (data.get("pg_selected_groups") or [])]
    if gid in selected:
        selected = [x for x in selected if x != gid]
    else:
        from app.services.bot_pg_catalog_authz import catalog_groups_allowed

        if not catalog_groups_allowed(gate.staff, [gid]):
            await callback.answer("اجازه این عمل را ندارید", show_alert=True)
            return
        selected.append(gid)
    await state.update_data(pg_selected_groups=selected)
    await callback.answer()
    await _show_create_group_picker(callback, state, pg=gate.pg_client, staff=gate.staff)


@router.callback_query(F.data == "adm:pg:grpdone")
async def pg_create_grpdone(
    callback: CallbackQuery,
    state: FSMContext,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    gate = await _pg_user_gate(
        db_user,
        action="create",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        callback=callback,
    )
    if not gate.allowed:
        return
    data = await state.get_data()
    selected = [int(x) for x in (data.get("pg_selected_groups") or [])]
    if not selected:
        await callback.answer("حداقل یک گروه انتخاب کنید", show_alert=True)
        return
    from app.services.bot_pg_catalog_authz import catalog_groups_allowed

    if not catalog_groups_allowed(gate.staff, selected):
        await callback.answer("اجازه این عمل را ندارید", show_alert=True)
        return
    await callback.answer()
    await state.update_data(pg_create_mode="custom")
    await state.set_state(PgUserStates.create_username)
    if callback.message:
        await callback.message.answer(
            "نام کاربری جدید را بفرستید (۳–۳۲ حرف انگلیسی/عدد/_):",
            reply_markup=kb.cancel_reply(),
        )


@router.message(PgUserStates.create_username)
async def pg_create_username(
    message: Message,
    state: FSMContext,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    from app.services.bot_pg_user_authz import sanitize_pg_user_write_payload

    gate = await _pg_user_gate(
        db_user,
        action="create",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        message=message,
    )
    if not gate.allowed:
        await state.clear()
        return
    if kb.is_cancel_text(message.text):
        await state.clear()
        await message.answer("لغو شد.", reply_markup=await filtered_pg_reply_keyboard(db_user, session=session, is_reseller_bot=is_reseller_bot))
        return
    uname = (message.text or "").strip()
    if not _USERNAME_RE.fullmatch(uname):
        await message.answer("نام کاربری باید ۳ تا ۳۲ کاراکتر انگلیسی، عدد یا _ باشد.")
        return
    data = await state.get_data()
    mode = data.get("pg_create_mode")
    await state.update_data(pg_create_username=uname)
    if mode == "custom":
        await state.set_state(PgUserStates.create_gb)
        await message.answer(
            "حجم به گیگابایت را بفرستید (عدد؛ برای نامحدود ۰ بفرستید):",
            reply_markup=kb.cancel_reply(),
        )
        return
    if mode != "template":
        await state.clear()
        await message.answer(
            "اطلاعات ساخت ناقص است. دوباره از «ساخت کاربر» شروع کنید.",
            reply_markup=await filtered_pg_reply_keyboard(
                db_user, session=session, is_reseller_bot=is_reseller_bot
            ),
        )
        return
    tid = data.get("pg_template_id")
    if not tid:
        await state.clear()
        await message.answer(
            "تمپلیت انتخاب نشده. از منوی ساخت کاربر دوباره یک تمپلیت انتخاب کنید.",
            reply_markup=await filtered_pg_reply_keyboard(
                db_user, session=session, is_reseller_bot=is_reseller_bot
            ),
        )
        return
    from app.services.bot_pg_catalog_authz import catalog_template_allowed

    if not catalog_template_allowed(gate.staff, int(tid)):
        await state.clear()
        await message.answer(
            "اجازه این عمل را ندارید",
            reply_markup=await filtered_pg_reply_keyboard(
                db_user, session=session, is_reseller_bot=is_reseller_bot
            ),
        )
        return
    try:
        await assert_can_create_user(
            gate.staff or {}, from_template=True, session=session, client=gate.pg_client
        )
    except PgQuotaError as qe:
        await message.answer(f"❌ {qe.message}")
        return
    try:
        payload = sanitize_pg_user_write_payload(
            {
                "username": uname,
                "user_template_id": int(tid),
                "note": f"telegram admin · {db_user.telegram_id}",
            }
        )
        user = await gate.pg_client.create_user_from_template(payload)
    except Exception as e:
        # Keep create_username so the operator can retry with another name
        # (duplicate username must not look like a missing-template failure).
        await message.answer(f"❌ {_err_msg(e)}\nنام دیگری بفرستید یا لغو کنید.")
        return
    await state.clear()
    uid = int((user or {}).get("id") or 0) if isinstance(user, dict) else 0
    if uid:
        await _show_user_card(
            message,
            uid,
            edit=False,
            notice="✅ کاربر ساخته شد",
            user=user if isinstance(user, dict) else None,
            pg=gate.pg_client,
        )
    else:
        await message.answer(
            "✅ کاربر ساخته شد.",
            reply_markup=await filtered_pg_reply_keyboard(
                db_user, session=session, is_reseller_bot=is_reseller_bot
            ),
        )


@router.message(PgUserStates.create_gb)
async def pg_create_gb(
    message: Message,
    state: FSMContext,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    gate = await _pg_user_gate(
        db_user,
        action="create",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        message=message,
    )
    if not gate.allowed:
        await state.clear()
        return
    if kb.is_cancel_text(message.text):
        await state.clear()
        await message.answer("لغو شد.", reply_markup=await filtered_pg_reply_keyboard(db_user, session=session, is_reseller_bot=is_reseller_bot))
        return
    raw = (message.text or "").strip().replace(",", ".")
    try:
        gb = float(raw)
        if gb < 0:
            raise ValueError
    except ValueError:
        await message.answer("عدد معتبر بفرستید (مثلاً ۱۰ یا ۰).")
        return
    await state.update_data(pg_create_gb=gb)
    await state.set_state(PgUserStates.create_days)
    await message.answer(
        "مدت به روز را بفرستید (عدد؛ برای بدون انقضا ۰ بفرستید):",
        reply_markup=kb.cancel_reply(),
    )


@router.message(PgUserStates.create_days)
async def pg_create_days(
    message: Message,
    state: FSMContext,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    from app.services.bot_pg_user_authz import sanitize_pg_user_write_payload

    gate = await _pg_user_gate(
        db_user,
        action="create",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        message=message,
    )
    if not gate.allowed:
        await state.clear()
        return
    if kb.is_cancel_text(message.text):
        await state.clear()
        await message.answer("لغو شد.", reply_markup=await filtered_pg_reply_keyboard(db_user, session=session, is_reseller_bot=is_reseller_bot))
        return
    raw = (message.text or "").strip()
    try:
        days = int(float(raw))
        if days < 0:
            raise ValueError
    except ValueError:
        await message.answer("عدد روز معتبر بفرستید.")
        return
    data = await state.get_data()
    uname = data.get("pg_create_username")
    groups = [int(x) for x in (data.get("pg_selected_groups") or [])]
    gb = float(data.get("pg_create_gb") or 0)
    if not uname or not groups:
        await state.clear()
        await message.answer("داده ناقص است — دوباره شروع کنید.", reply_markup=await filtered_pg_reply_keyboard(db_user, session=session, is_reseller_bot=is_reseller_bot))
        return
    from app.services.bot_pg_catalog_authz import catalog_groups_allowed

    if not catalog_groups_allowed(gate.staff, groups):
        await state.clear()
        await message.answer("اجازه این عمل را ندارید", reply_markup=await filtered_pg_reply_keyboard(db_user, session=session, is_reseller_bot=is_reseller_bot))
        return
    data_limit = int(gb * (1024**3)) if gb > 0 else 0
    expire_ts = int(time.time()) + days * 86400 if days > 0 else 0
    payload = sanitize_pg_user_write_payload(
        build_user_create_payload(
            username=uname,
            group_ids=groups,
            data_limit=data_limit if gb > 0 else 0,
            expire_ts=expire_ts if days > 0 else 0,
            note=f"telegram admin · {db_user.telegram_id}",
        )
    )
    if gb <= 0:
        payload["data_limit"] = 0
    if days <= 0:
        payload["expire"] = 0
    try:
        await assert_can_create_user(
            gate.staff or {},
            data_limit=data_limit if gb > 0 else None,
            expire_ts=expire_ts if days > 0 else None,
            from_template=False,
            session=session,
            client=gate.pg_client,
        )
    except PgQuotaError as qe:
        await message.answer(f"❌ {qe.message}")
        return
    try:
        user = await gate.pg_client.create_user(payload)
    except Exception as e:
        # Recoverable (e.g. duplicate username): go back to username step.
        await state.set_state(PgUserStates.create_username)
        await message.answer(
            f"❌ {_err_msg(e)}\nنام کاربری دیگری بفرستید:",
            reply_markup=kb.cancel_reply(),
        )
        return
    await state.clear()
    uid = int((user or {}).get("id") or 0) if isinstance(user, dict) else 0
    if uid:
        await _show_user_card(
            message,
            uid,
            edit=False,
            notice="✅ کاربر ساخته شد",
            user=user if isinstance(user, dict) else None,
            pg=gate.pg_client,
        )
    else:
        await message.answer("✅ کاربر ساخته شد.", reply_markup=await filtered_pg_reply_keyboard(db_user, session=session, is_reseller_bot=is_reseller_bot))


# ----- edit -----


@router.callback_query(F.data.regexp(r"^adm:pg:u:\d+:edit$"))
async def pg_user_edit_menu(
    callback: CallbackQuery,
    state: FSMContext,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    gate = await _pg_user_gate(
        db_user,
        action="update",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        callback=callback,
    )
    if not gate.allowed:
        return
    uid = int(gate.pg_user["id"]) if gate.pg_user and gate.pg_user.get("id") is not None else 0
    await callback.answer()
    await state.update_data(pg_edit_uid=uid)
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message("✏️ ویرایش کاربر", f"#{uid} — چه چیزی تغییر کند؟"),
            reply_markup=_edit_menu_kb(uid),
        )


@router.callback_query(F.data.regexp(r"^adm:pg:u:\d+:ed:name$"))
async def pg_edit_name_ask(
    callback: CallbackQuery,
    state: FSMContext,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    gate = await _pg_user_gate(
        db_user,
        action="update",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        callback=callback,
    )
    if not gate.allowed:
        return
    uid = int(gate.pg_user["id"]) if gate.pg_user and gate.pg_user.get("id") is not None else 0
    await callback.answer()
    await state.update_data(pg_edit_uid=uid)
    await state.set_state(PgUserStates.edit_username)
    if callback.message:
        await callback.message.answer(
            "نام کاربری جدید را بفرستید:",
            reply_markup=kb.cancel_reply(),
        )


@router.message(PgUserStates.edit_username)
async def pg_edit_name_save(
    message: Message,
    state: FSMContext,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    from app.services.bot_pg_user_authz import sanitize_pg_user_write_payload

    if kb.is_cancel_text(message.text):
        await state.clear()
        await message.answer("لغو شد.", reply_markup=await filtered_pg_reply_keyboard(db_user, session=session, is_reseller_bot=is_reseller_bot))
        return
    uname = (message.text or "").strip()
    if not _USERNAME_RE.fullmatch(uname):
        await message.answer("نام کاربری نامعتبر است.")
        return
    data = await state.get_data()
    uid = int(data.get("pg_edit_uid") or 0)
    gate = await _pg_user_gate(
        db_user,
        action="update",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        message=message,
        pg_user_id=uid,
    )
    if not gate.allowed:
        await state.clear()
        return
    if not uid:
        await state.clear()
        return
    try:
        current = gate.pg_user or {}
        groups = user_group_ids(current if isinstance(current, dict) else {})
        payload = sanitize_pg_user_write_payload(
            build_user_modify_payload(
                username=uname,
                group_ids=groups or None,
                status=str((current or {}).get("status") or "") or None if isinstance(current, dict) else None,
            )
        )
        await gate.pg_client.modify_user_by_id(uid, payload)
    except Exception as e:
        await message.answer(f"❌ {_err_msg(e)}\nنام دیگری بفرستید یا لغو کنید.")
        return
    await state.clear()
    await _show_user_card(message, uid, edit=False, notice="✅ نام کاربری به‌روز شد", pg=gate.pg_client)


@router.callback_query(F.data.regexp(r"^adm:pg:u:\d+:ed:gb$"))
async def pg_edit_gb_ask(
    callback: CallbackQuery,
    state: FSMContext,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    gate = await _pg_user_gate(
        db_user,
        action="update",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        callback=callback,
    )
    if not gate.allowed:
        return
    uid = int(gate.pg_user["id"]) if gate.pg_user and gate.pg_user.get("id") is not None else 0
    await callback.answer()
    await state.update_data(pg_edit_uid=uid)
    await state.set_state(PgUserStates.edit_gb)
    if callback.message:
        await callback.message.answer(
            "حجم جدید به گیگابایت (۰ = نامحدود):",
            reply_markup=kb.cancel_reply(),
        )


@router.message(PgUserStates.edit_gb)
async def pg_edit_gb_save(
    message: Message,
    state: FSMContext,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    from app.services.bot_pg_user_authz import sanitize_pg_user_write_payload

    if kb.is_cancel_text(message.text):
        await state.clear()
        await message.answer("لغو شد.", reply_markup=await filtered_pg_reply_keyboard(db_user, session=session, is_reseller_bot=is_reseller_bot))
        return
    raw = (message.text or "").strip().replace(",", ".")
    try:
        gb = float(raw)
        if gb < 0:
            raise ValueError
    except ValueError:
        await message.answer("عدد معتبر بفرستید.")
        return
    data = await state.get_data()
    uid = int(data.get("pg_edit_uid") or 0)
    gate = await _pg_user_gate(
        db_user,
        action="update",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        message=message,
        pg_user_id=uid,
    )
    if not gate.allowed:
        await state.clear()
        return
    if not uid:
        await state.clear()
        return
    try:
        current = gate.pg_user or {}
        groups = user_group_ids(current if isinstance(current, dict) else {})
        uname = (current or {}).get("username") if isinstance(current, dict) else None
        payload = sanitize_pg_user_write_payload(
            build_user_modify_payload(
                username=str(uname) if uname else None,
                group_ids=groups or None,
                data_limit=int(gb * (1024**3)) if gb > 0 else 0,
                status=str((current or {}).get("status") or "") or None if isinstance(current, dict) else None,
            )
        )
        await gate.pg_client.modify_user_by_id(uid, payload)
    except Exception as e:
        await message.answer(f"❌ {_err_msg(e)}")
        return
    await state.clear()
    await _show_user_card(message, uid, edit=False, notice="✅ حجم به‌روز شد", pg=gate.pg_client)


@router.callback_query(F.data.regexp(r"^adm:pg:u:\d+:ed:days$"))
async def pg_edit_days_ask(
    callback: CallbackQuery,
    state: FSMContext,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    gate = await _pg_user_gate(
        db_user,
        action="update",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        callback=callback,
    )
    if not gate.allowed:
        return
    uid = int(gate.pg_user["id"]) if gate.pg_user and gate.pg_user.get("id") is not None else 0
    await callback.answer()
    await state.update_data(pg_edit_uid=uid)
    await state.set_state(PgUserStates.edit_days)
    if callback.message:
        await callback.message.answer(
            "مدت باقی‌مانده از الان به روز (۰ = بدون انقضا):",
            reply_markup=kb.cancel_reply(),
        )


@router.message(PgUserStates.edit_days)
async def pg_edit_days_save(
    message: Message,
    state: FSMContext,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    from app.services.bot_pg_user_authz import sanitize_pg_user_write_payload

    if kb.is_cancel_text(message.text):
        await state.clear()
        await message.answer("لغو شد.", reply_markup=await filtered_pg_reply_keyboard(db_user, session=session, is_reseller_bot=is_reseller_bot))
        return
    raw = (message.text or "").strip()
    try:
        days = int(float(raw))
        if days < 0:
            raise ValueError
    except ValueError:
        await message.answer("عدد روز معتبر بفرستید.")
        return
    data = await state.get_data()
    uid = int(data.get("pg_edit_uid") or 0)
    gate = await _pg_user_gate(
        db_user,
        action="update",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        message=message,
        pg_user_id=uid,
    )
    if not gate.allowed:
        await state.clear()
        return
    if not uid:
        await state.clear()
        return
    expire_ts = int(time.time()) + days * 86400 if days > 0 else 0
    try:
        current = gate.pg_user or {}
        groups = user_group_ids(current if isinstance(current, dict) else {})
        uname = (current or {}).get("username") if isinstance(current, dict) else None
        payload = sanitize_pg_user_write_payload(
            build_user_modify_payload(
                username=str(uname) if uname else None,
                group_ids=groups or None,
                expire_ts=expire_ts,
                status=str((current or {}).get("status") or "") or None if isinstance(current, dict) else None,
            )
        )
        await gate.pg_client.modify_user_by_id(uid, payload)
    except Exception as e:
        await message.answer(f"❌ {_err_msg(e)}")
        return
    await state.clear()
    await _show_user_card(message, uid, edit=False, notice="✅ انقضا به‌روز شد", pg=gate.pg_client)


@router.callback_query(F.data.regexp(r"^adm:pg:u:\d+:ed:grps$"))
async def pg_edit_groups_start(
    callback: CallbackQuery,
    state: FSMContext,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    gate = await _pg_user_gate(
        db_user,
        action="update",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        callback=callback,
    )
    if not gate.allowed:
        return
    uid = int(gate.pg_user["id"]) if gate.pg_user and gate.pg_user.get("id") is not None else 0
    await callback.answer()
    selected = user_group_ids(gate.pg_user if isinstance(gate.pg_user, dict) else {})
    await state.update_data(pg_edit_uid=uid, pg_selected_groups=selected)
    await _show_create_group_picker(callback, state, pg=gate.pg_client, staff=gate.staff)


@router.callback_query(F.data.startswith("adm:pg:edgrp:"))
async def pg_edit_toggrp(
    callback: CallbackQuery,
    state: FSMContext,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    data = await state.get_data()
    uid = int(data.get("pg_edit_uid") or 0)
    gate = await _pg_user_gate(
        db_user,
        action="update",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        callback=callback,
        pg_user_id=uid,
    )
    if not gate.allowed:
        return
    match = _EDGRP_RE.match(callback.data or "")
    if match is None:
        await callback.answer("اجازه این عمل را ندارید", show_alert=True)
        return
    gid = int(match.group(1))
    selected = [int(x) for x in (data.get("pg_selected_groups") or [])]
    if gid in selected:
        selected = [x for x in selected if x != gid]
    else:
        from app.services.bot_pg_catalog_authz import catalog_groups_allowed

        if not catalog_groups_allowed(gate.staff, [gid]):
            await callback.answer("اجازه این عمل را ندارید", show_alert=True)
            return
        selected.append(gid)
    await state.update_data(pg_selected_groups=selected)
    await callback.answer()
    await _show_create_group_picker(callback, state, pg=gate.pg_client, staff=gate.staff)


@router.callback_query(F.data == "adm:pg:edgrpdone")
async def pg_edit_grpdone(
    callback: CallbackQuery,
    state: FSMContext,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    from app.services.bot_pg_user_authz import sanitize_pg_user_write_payload

    data = await state.get_data()
    uid = int(data.get("pg_edit_uid") or 0)
    selected = [int(x) for x in (data.get("pg_selected_groups") or [])]
    gate = await _pg_user_gate(
        db_user,
        action="update",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        callback=callback,
        pg_user_id=uid,
    )
    if not gate.allowed:
        return
    if not uid:
        await callback.answer("کاربر نامعتبر", show_alert=True)
        return
    if not selected:
        await callback.answer("حداقل یک گروه انتخاب کنید", show_alert=True)
        return
    from app.services.bot_pg_catalog_authz import catalog_groups_allowed

    if not catalog_groups_allowed(gate.staff, selected):
        await callback.answer("اجازه این عمل را ندارید", show_alert=True)
        return
    try:
        current = gate.pg_user or {}
        uname = (current or {}).get("username") if isinstance(current, dict) else None
        payload = sanitize_pg_user_write_payload(
            build_user_modify_payload(
                username=str(uname) if uname else None,
                group_ids=selected,
                status=str((current or {}).get("status") or "") or None if isinstance(current, dict) else None,
            )
        )
        await gate.pg_client.modify_user_by_id(uid, payload)
        await callback.answer("گروه‌ها ذخیره شد ✅", show_alert=True)
        await state.clear()
        if callback.message:
            await _show_user_card(
                callback.message, uid, notice="✅ گروه‌ها به‌روز شد", pg=gate.pg_client
            )
    except Exception as e:
        await callback.answer(_err_msg(e), show_alert=True)
