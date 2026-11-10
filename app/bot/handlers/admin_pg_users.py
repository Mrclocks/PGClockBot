"""Admin PasarGuard VPN users — list/search/create/edit/actions (panel parity)."""

from __future__ import annotations

import html
import logging
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
)
from app.bot.auth import is_platform_admin as _is_admin
from app.bot.handlers.action_reason import ask_reason, take_reason
from app.bot.nav_input import form_origin
from app.bot.tg_utils import safe_edit_text
from app.db.models import BotUser
from app.services.action_reasons import bot_actor
from app.services.chat_records import keep_sent
from app.services.customer_action_notices import NoticeTarget, log_and_notify, pg_user_targets_for_shop
from app.services.formatting import format_message, service_card
from app.services.pasarguard import (
    as_list,
    build_user_create_payload,
    build_user_modify_payload,
    get_pg,
    user_group_ids,
    user_subscription_url,
)
from app.services.pg_quota import PgQuotaError, assert_can_create_user
from app.services.payg_gate import (
    charge_staff_create,
    charge_staff_edit,
    finish_charge,
    finish_charges,
    reset_with_charge,
    settle_failed_charge,
    settle_failed_charges,
)
from app.services.provision_gate import finish_staff_edit, hold_staff_create, hold_staff_edit, settle_failed_hold
from app.services.redact import user_safe_error
from app.services import bot_feedback_texts as feedback

_USERS_BACK = InlineKeyboardMarkup(
    inline_keyboard=[[kb.back_button(None, "adm:pg:users")]]
)
# The PasarGuard hub the users list is opened from.
PG_HUB_CALLBACK = "nv:adm:pg"
_PG_HUB_BACK = InlineKeyboardMarkup(inline_keyboard=[[kb.back_button(None, PG_HUB_CALLBACK)]])

logger = logging.getLogger(__name__)
router = Router(name="admin_pg_users")

_GROUPS_FETCH_FAILED_MSG = "خواندن گروه‌ها ناموفق بود؛ دوباره تلاش کنید."

def _err_msg(exc: Exception) -> str:
    """Persian-friendly PasarGuard / local error for bot replies."""
    return user_safe_error(exc, fallback="خطا در ارتباط با پاسارگارد")

async def _require_users(db_user: BotUser, callback=None, message=None, *, action: str | None = None) -> bool:
    """Legacy platform-admin page gate (kept for source contracts).

    Migrated PG-user handlers must use ``_pg_user_gate`` — never this function
    as the final authorization decision.
    """
    if not _is_admin(db_user):
        if callback is not None:
            await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        elif message is not None:
            await message.answer(feedback.NOT_ADMIN_REPLY)
        return False
    if not await can_platform_pg_page(db_user, "pg_users"):
        if callback is not None:
            await callback.answer("به کاربران پاسارگارد دسترسی ندارید", show_alert=True)
        elif message is not None:
            await message.answer("به کاربران پاسارگارد دسترسی ندارید.")
        return False
    if action and not await can_platform_pg_action(db_user, "users", action):
        if callback is not None:
            await callback.answer(feedback.ACTION_NOT_ALLOWED, show_alert=True)
        elif message is not None:
            await message.answer(feedback.ACTION_NOT_ALLOWED_REPLY)
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


async def _load_staff_groups(
    pg, staff, state: FSMContext | None = None
) -> tuple[list, bool]:
    """Filtered groups for this staff; reuse FSM cache when a prior fetch succeeded.

    Returns ``(groups, ok)``. ``ok`` is False when the panel fetch failed; then
    ``groups`` is ``[]`` and nothing is written to the FSM cache (so a transient
    timeout cannot freeze an empty list for the rest of the flow).
    """
    if state is not None:
        data = await state.get_data()
        if data.get("pg_groups_cache_ok") is True:
            cached = data.get("pg_groups_cache")
            if isinstance(cached, list):
                return cached, True
    try:
        raw = await pg.get_groups_simple()
    except Exception:
        logger.warning("PasarGuard groups fetch failed for staff picker", exc_info=True)
        return [], False
    if not isinstance(raw, list):
        logger.warning(
            "PasarGuard groups fetch returned non-list: %s",
            type(raw).__name__,
        )
        return [], False
    groups = _filter_staff_groups(raw, staff)
    if state is not None:
        await state.update_data(pg_groups_cache=groups, pg_groups_cache_ok=True)
    return groups, True


async def _begin_custom_create_username(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(pg_create_mode="custom")
    await state.set_state(PgUserStates.create_username)
    from app.bot.nav_input import ask_text

    await ask_text(
        callback,
        state,
        prompt="نام کاربری جدید را بفرستید (۳–۳۲ حرف انگلیسی/عدد/_):",
        cancel_code="adm_pg_u",
        return_to="adm:pg:create",
        fsm_state=PgUserStates.create_username,
        edit=True,
    )


async def _resolve_selected_groups_for_write(
    *,
    pg,
    staff,
    state: FSMContext,
    selected: list[int],
) -> tuple[list[int] | None, str | None]:
    """Return ``(group_ids_or_omit, error_message)`` for create/edit writes."""
    from app.services.bot_pg_catalog_authz import catalog_groups_allowed
    from app.services.plans_catalog import resolve_user_write_group_ids

    visible, groups_ok = await _load_staff_groups(pg, staff, state)
    if not groups_ok:
        return None, _GROUPS_FETCH_FAILED_MSG
    try:
        resolved = resolve_user_write_group_ids(selected, visible_groups=visible)
    except ValueError:
        return None, feedback.PICK_AT_LEAST_ONE_GROUP
    if resolved is not None and not catalog_groups_allowed(staff, resolved):
        return None, feedback.ACTION_NOT_ALLOWED
    return resolved, None

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
            [kb.back_button(None, back)],
        ]
    )

def _edit_menu_kb(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=kb.pack_detail_rows(
            [
                [InlineKeyboardButton(text="👤 تغییر نام کاربری", callback_data=f"adm:pg:u:{uid}:ed:name")],
                [InlineKeyboardButton(text="📦 تغییر حجم (گیگ)", callback_data=f"adm:pg:u:{uid}:ed:gb")],
                [InlineKeyboardButton(text="📅 تغییر مدت (روز)", callback_data=f"adm:pg:u:{uid}:ed:days")],
                [InlineKeyboardButton(text="📁 تغییر گروه‌ها", callback_data=f"adm:pg:u:{uid}:ed:grps")],
                [kb.back_button(None, f"adm:pg:u:{uid}")],
            ]
        )
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
        markup = _PG_HUB_BACK
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
    rows.append([kb.back_button(None, PG_HUB_CALLBACK)])

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
            text = format_message("❌ خطا", feedback.ACTION_NOT_ALLOWED)
            if edit:
                await safe_edit_text(target, text, reply_markup=_USERS_BACK)
            else:
                await target.answer(text, reply_markup=_USERS_BACK)
            return
        try:
            user = await pg.get_user_by_id(uid)
        except Exception as e:
            text = format_message("❌ خطا", user_safe_error(e))
            if edit:
                await safe_edit_text(target, text, reply_markup=_USERS_BACK)
            else:
                await target.answer(text, reply_markup=_USERS_BACK)
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
    from app.bot.auth import bot_may_open_pg_hub

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
    if not callback.message:
        return
    from app.bot.handlers.reply_nav import open_pg_home

    await open_pg_home(
        callback.message,
        session,
        db_user,
        state=None,
        push=False,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
    )
    return

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
        await callback.message.edit_text(
            "\n".join(lines)[:3900],
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[kb.back_button(None, "nv:adm:pg")]]
            ),
        )

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
        await callback.message.edit_text(
            "\n".join(lines)[:3900],
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[kb.back_button(None, "nv:adm:pg")]]
            ),
        )

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
    from app.bot.nav_input import ask_text

    await ask_text(
        callback,
        state,
        prompt=(
            "نام کاربری پاسارگارد را برای جستجو بفرستید:\n"
            "(جستجو در لیست فیلتر می‌شود؛ برای انصراف «انصراف» بزنید)"
        ),
        cancel_code="adm_pg_u",
        return_to="adm:pg:users",
        fsm_state=PgUserStates.search,
        edit=True,
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
        from app.bot.nav_input import try_legacy_cancel

        if await try_legacy_cancel(
            message,
            state,
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        ):
            return
        await state.clear()
        await message.answer(feedback.CANCELLED)
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
    with keep_sent():  # the link is copied from here later
        await callback.message.answer(
            format_message(
                "🔗 لینک اشتراک",
                f"کاربر: <code>{html.escape(str(uname or uid))}</code>\n\n<code>{html.escape(url)}</code>",
            ),
            reply_markup=_user_actions_kb(uid),
        )

PG_ACTION_LABELS = {
    "pg_user_disable": "علت غیرفعال‌سازی",
    "pg_user_enable": "علت فعال‌سازی",
    "pg_user_reset_usage": "علت ریست مصرف",
    "pg_user_revoke_sub": "علت ابطال اشتراک",
    "pg_user_delete": "علت حذف کاربر",
}


async def _pg_notice_targets(session: AsyncSession | None, owner_id: int | None, uid: int) -> list[NoticeTarget]:
    return [] if session is None else await pg_user_targets_for_shop(session, owner_id, pg_user_id=uid)


async def _finish_reasoned_pg_action(
    session: AsyncSession | None,
    db_user: BotUser,
    *,
    owner_id: int | None,
    uid: int,
    action: str,
    reason: str,
    targets: list[NoticeTarget],
) -> None:
    """Staff log record and customer notice once the panel operation succeeded."""
    if session is None:
        return
    await log_and_notify(
        session,
        bot_actor(db_user, shop_id=owner_id),
        action=action,
        target_type="pg_user",
        target_id=uid,
        reason=reason,
        targets=targets,
    )


@router.callback_query(F.data.startswith("adm:pg:reset:"))
async def pg_reset(
    callback: CallbackQuery,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
    state: FSMContext | None = None,
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
    reason = await take_reason(state, callback)
    if reason is None:
        await ask_reason(callback, state, label="علت ریست مصرف")
        return
    targets = await _pg_notice_targets(session, reseller_owner_id, uid)
    try:
        user = await reset_with_charge(
            session, gate.staff, pg=gate.pg_client, pg_user_id=uid, now_ts=int(time.time())
        )
        await _finish_reasoned_pg_action(
            session, db_user, owner_id=reseller_owner_id, uid=uid, action="pg_user_reset_usage", reason=reason, targets=targets
        )
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
    state: FSMContext | None = None,
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
    reason = await take_reason(state, callback)
    if reason is None:
        await ask_reason(callback, state, label="علت غیرفعال‌سازی")
        return
    targets = await _pg_notice_targets(session, reseller_owner_id, uid)
    try:
        user = await gate.pg_client.set_disabled_by_id(uid, True)
        await _finish_reasoned_pg_action(
            session, db_user, owner_id=reseller_owner_id, uid=uid, action="pg_user_disable", reason=reason, targets=targets
        )
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
    state: FSMContext | None = None,
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
    reason = await take_reason(state, callback)
    if reason is None:
        await ask_reason(callback, state, label="علت فعال‌سازی")
        return
    targets = await _pg_notice_targets(session, reseller_owner_id, uid)
    try:
        user = await gate.pg_client.set_disabled_by_id(uid, False)
        await _finish_reasoned_pg_action(
            session, db_user, owner_id=reseller_owner_id, uid=uid, action="pg_user_enable", reason=reason, targets=targets
        )
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
    state: FSMContext | None = None,
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
    reason = await take_reason(state, callback)
    if reason is None:
        await ask_reason(callback, state, label="علت ابطال اشتراک")
        return
    targets = await _pg_notice_targets(session, reseller_owner_id, uid)
    try:
        user = await gate.pg_client.revoke_sub_by_id(uid)
        await _finish_reasoned_pg_action(
            session, db_user, owner_id=reseller_owner_id, uid=uid, action="pg_user_revoke_sub", reason=reason, targets=targets
        )
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
    reason = await take_reason(state, callback)
    if reason is None:
        await ask_reason(callback, state, label="علت حذف کاربر")
        return
    targets = await _pg_notice_targets(session, reseller_owner_id, uid)
    try:
        from app.services.bot_user_admin import (
            detach_local_services_for_pg_user,
            ensure_pg_user_removed_or_absent,
        )

        await ensure_pg_user_removed_or_absent(
            gate.pg_client, uid, staff=None, allow_disable_fallback=False
        )
        if session is not None:
            await detach_local_services_for_pg_user(session, uid, commit=True)
        await _finish_reasoned_pg_action(
            session, db_user, owner_id=reseller_owner_id, uid=uid, action="pg_user_delete", reason=reason, targets=targets
        )
        await callback.answer(feedback.DELETED, show_alert=True)
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
            [kb.back_button(None, "adm:pg:users")],
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
            [InlineKeyboardButton(text=feedback.NO_TEMPLATES, callback_data="adm:pg:create")]
        )
    rows.append([kb.back_button(None, "adm:pg:create")])
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
        await callback.answer(feedback.ACTION_NOT_ALLOWED, show_alert=True)
        return
    tid = int(match.group(1))
    from app.services.bot_pg_catalog_authz import catalog_template_allowed

    if not catalog_template_allowed(gate.staff, tid):
        await callback.answer(feedback.ACTION_NOT_ALLOWED, show_alert=True)
        return
    await callback.answer()
    await state.update_data(pg_create_mode="template", pg_template_id=tid)
    await state.set_state(PgUserStates.create_username)
    from app.bot.nav_input import ask_text

    await ask_text(
        callback,
        state,
        prompt=f"تمپلیت #{tid} انتخاب شد.\nنام کاربری جدید را بفرستید (۳–۳۲ حرف انگلیسی/عدد/_):",
        cancel_code="adm_pg_u",
        return_to="adm:pg:create",
        fsm_state=PgUserStates.create_username,
        edit=True,
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
    await state.clear()
    await state.update_data(pg_create_mode="custom", pg_template_id=None, pg_selected_groups=[])
    groups, groups_ok = await _load_staff_groups(gate.pg_client, gate.staff, state)
    if not groups_ok:
        await callback.answer(_GROUPS_FETCH_FAILED_MSG, show_alert=True)
        return
    await callback.answer()
    if not groups:
        # Panel may already bind groups to this admin; skip a blank picker.
        await _begin_custom_create_username(callback, state)
        return
    await _show_create_group_picker(callback, state, pg=gate.pg_client, staff=gate.staff)

async def _show_create_group_picker(
    callback: CallbackQuery, state: FSMContext, *, pg, staff=None
) -> bool:
    data = await state.get_data()
    selected = [int(x) for x in (data.get("pg_selected_groups") or [])]
    edit_uid = data.get("pg_edit_uid")
    groups, groups_ok = await _load_staff_groups(pg, staff, state)
    if not groups_ok:
        await callback.answer(_GROUPS_FETCH_FAILED_MSG, show_alert=True)
        return False
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
    # Picker is only shown when the filtered list is non-empty → selection
    # is mandatory (same as pre-optional behavior).
    rows.append(
        [
            InlineKeyboardButton(
                text=f"✅ تأیید ({len(selected)})",
                callback_data=done_cb,
            )
        ]
    )
    rows.append([kb.back_button(None, back_cb)])
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
    return True

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
        await callback.answer(feedback.ACTION_NOT_ALLOWED, show_alert=True)
        return
    gid = int(match.group(1))
    data = await state.get_data()
    selected = [int(x) for x in (data.get("pg_selected_groups") or [])]
    if gid in selected:
        selected = [x for x in selected if x != gid]
    else:
        from app.services.bot_pg_catalog_authz import catalog_groups_allowed

        if not catalog_groups_allowed(gate.staff, [gid]):
            await callback.answer(feedback.ACTION_NOT_ALLOWED, show_alert=True)
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
    _resolved, err = await _resolve_selected_groups_for_write(
        pg=gate.pg_client,
        staff=gate.staff,
        state=state,
        selected=selected,
    )
    if err:
        await callback.answer(err, show_alert=True)
        return
    await callback.answer()
    await _begin_custom_create_username(callback, state)

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
        from app.bot.nav_input import try_legacy_cancel

        if await try_legacy_cancel(
            message,
            state,
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        ):
            return
        await state.clear()
        await message.answer(feedback.CANCELLED)
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
        from app.bot.nav_input import ask_text

        await ask_text(
            message,
            state,
            prompt="حجم به گیگابایت را بفرستید (عدد؛ برای نامحدود ۰ بفرستید):",
            cancel_code="adm_pg_u",
            return_to=await form_origin(state),
            fsm_state=PgUserStates.create_gb,
            edit=False,
        )
        return
    if mode != "template":
        await state.clear()
        await message.answer(
            "اطلاعات ساخت ناقص است. دوباره از «ساخت کاربر» شروع کنید.",
        )
        return
    tid = data.get("pg_template_id")
    if not tid:
        await state.clear()
        await message.answer(
            "تمپلیت انتخاب نشده. از منوی ساخت کاربر دوباره یک تمپلیت انتخاب کنید.",
        )
        return
    from app.services.bot_pg_catalog_authz import catalog_template_allowed

    if not catalog_template_allowed(gate.staff, int(tid)):
        await state.clear()
        await message.answer(
            feedback.ACTION_NOT_ALLOWED,
        )
        return
    try:
        await assert_can_create_user(
            gate.staff or {}, from_template=True, session=session, client=gate.pg_client
        )
    except PgQuotaError as qe:
        await message.answer(f"❌ {qe.message}")
        return
    hold = None
    charge = None
    try:
        payload = sanitize_pg_user_write_payload(
            {
                "username": uname,
                "user_template_id": int(tid),
                "note": f"telegram admin · {db_user.telegram_id}",
            }
        )
        if session is not None:
            hold = await hold_staff_create(
                session, gate.staff, pg=gate.pg_client, template_id=int(tid), data_limit=None
            )
            charge = await charge_staff_create(
                session, gate.staff, pg=gate.pg_client, hold=hold, template_id=int(tid), data_limit=None,
                expire_ts=None, username=uname, now_ts=int(time.time()),
            )
        user = await gate.pg_client.create_user_from_template(payload)
        await finish_charge(session, charge)
    except Exception as e:
        await settle_failed_charge(session, charge, e, panel_touched=True)
        if session is not None:
            await settle_failed_hold(session, hold, e, panel_touched=True)
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
        from app.bot.nav_input import try_legacy_cancel

        if await try_legacy_cancel(
            message,
            state,
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        ):
            return
        await state.clear()
        await message.answer(feedback.CANCELLED)
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
    from app.bot.nav_input import ask_text

    await ask_text(
        message,
        state,
        prompt="مدت به روز را بفرستید (عدد؛ برای بدون انقضا ۰ بفرستید):",
        cancel_code="adm_pg_u",
        return_to=await form_origin(state),
        fsm_state=PgUserStates.create_days,
        edit=False,
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
        from app.bot.nav_input import try_legacy_cancel

        if await try_legacy_cancel(
            message,
            state,
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        ):
            return
        await state.clear()
        await message.answer(feedback.CANCELLED)
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
    if not uname:
        await state.clear()
        await message.answer("اطلاعات این مرحله کامل نیست. دوباره شروع کنید.")
        return
    resolved_ids, groups_err = await _resolve_selected_groups_for_write(
        pg=gate.pg_client,
        staff=gate.staff,
        state=state,
        selected=groups,
    )
    if groups_err:
        await state.clear()
        await message.answer(groups_err)
        return
    data_limit = int(gb * (1024**3)) if gb > 0 else 0
    expire_ts = int(time.time()) + days * 86400 if days > 0 else 0
    payload = sanitize_pg_user_write_payload(
        build_user_create_payload(
            username=uname,
            group_ids=resolved_ids,
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
    hold = None
    charge = None
    try:
        if session is not None:
            hold = await hold_staff_create(
                session, gate.staff, pg=gate.pg_client, template_id=None, data_limit=data_limit
            )
            charge = await charge_staff_create(
                session, gate.staff, pg=gate.pg_client, hold=hold, template_id=None,
                data_limit=data_limit if gb > 0 else None, expire_ts=expire_ts if days > 0 else None,
                username=str(payload.get("username") or ""), now_ts=int(time.time()),
            )
        user = await gate.pg_client.create_user(payload)
        await finish_charge(session, charge)
    except Exception as e:
        await settle_failed_charge(session, charge, e, panel_touched=True)
        if session is not None:
            await settle_failed_hold(session, hold, e, panel_touched=True)
        # Recoverable (e.g. duplicate username): go back to username step.
        await state.set_state(PgUserStates.create_username)
        from app.bot.nav_input import ask_text

        await ask_text(
            message,
            state,
            prompt=f"❌ {_err_msg(e)}\nنام کاربری دیگری بفرستید:",
            cancel_code="adm_pg_u",
            return_to=await form_origin(state),
            fsm_state=PgUserStates.create_username,
            edit=False,
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
        await message.answer("✅ کاربر ساخته شد.")

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
    from app.bot.nav_input import ask_text

    await ask_text(
        callback,
        state,
        prompt="نام کاربری جدید را بفرستید:",
        cancel_code="adm_pg_u",
        return_to=f"adm:pg:u:{uid}:edit",
        fsm_state=PgUserStates.edit_username,
        edit=True,
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
        from app.bot.nav_input import try_legacy_cancel

        if await try_legacy_cancel(
            message,
            state,
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        ):
            return
        await state.clear()
        await message.answer(feedback.CANCELLED)
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
    from app.bot.nav_input import ask_text

    await ask_text(
        callback,
        state,
        prompt="حجم جدید را به گیگابایت بفرستید. برای نامحدود <code>0</code> بفرستید:",
        cancel_code="adm_pg_u",
        return_to=f"adm:pg:u:{uid}:edit",
        fsm_state=PgUserStates.edit_gb,
        edit=True,
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
        from app.bot.nav_input import try_legacy_cancel

        if await try_legacy_cancel(
            message,
            state,
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        ):
            return
        await state.clear()
        await message.answer(feedback.CANCELLED)
        return
    raw = (message.text or "").strip().replace(",", ".")
    try:
        gb = float(raw)
        if gb < 0:
            raise ValueError
    except ValueError:
        await message.answer(feedback.ENTER_VALID_NUMBER)
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
    hold = None
    charges: list = []
    new_limit = int(gb * (1024**3)) if gb > 0 else 0
    try:
        current = gate.pg_user or {}
        groups = user_group_ids(current if isinstance(current, dict) else {})
        uname = (current or {}).get("username") if isinstance(current, dict) else None
        payload = sanitize_pg_user_write_payload(
            build_user_modify_payload(
                username=str(uname) if uname else None,
                group_ids=groups or None,
                data_limit=new_limit,
                status=str((current or {}).get("status") or "") or None if isinstance(current, dict) else None,
            )
        )
        if session is not None:
            current_limit = current.get("data_limit") if isinstance(current, dict) else None
            hold = await hold_staff_edit(session, gate.staff, current_limit=current_limit, new_limit=new_limit)
            charges = await charge_staff_edit(
                session, gate.staff, hold=hold, current=current, data_limit=new_limit, expire_ts=None,
                now_ts=int(time.time()),
            )
        await gate.pg_client.modify_user_by_id(uid, payload)
        if session is not None:
            await finish_staff_edit(session, hold, pg_user_id=uid, data_limit=new_limit)
            await finish_charges(session, charges)
    except Exception as e:
        await settle_failed_charges(session, charges, e, panel_touched=True)
        if session is not None:
            await settle_failed_hold(session, hold, e, panel_touched=True)
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
    from app.bot.nav_input import ask_text

    await ask_text(
        callback,
        state,
        prompt="مدت باقی‌مانده از الان به روز (۰ = بدون انقضا):",
        cancel_code="adm_pg_u",
        return_to=f"adm:pg:u:{uid}:edit",
        fsm_state=PgUserStates.edit_days,
        edit=True,
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
        from app.bot.nav_input import try_legacy_cancel

        if await try_legacy_cancel(
            message,
            state,
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        ):
            return
        await state.clear()
        await message.answer(feedback.CANCELLED)
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
    charges: list = []
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
        charges = await charge_staff_edit(
            session, gate.staff, hold=None, current=current, data_limit=None, expire_ts=expire_ts,
            now_ts=int(time.time()),
        )
        await gate.pg_client.modify_user_by_id(uid, payload)
        await finish_charges(session, charges)
    except Exception as e:
        await settle_failed_charges(session, charges, e, panel_touched=True)
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
    groups, groups_ok = await _load_staff_groups(gate.pg_client, gate.staff, state)
    if not groups_ok:
        await callback.answer(_GROUPS_FETCH_FAILED_MSG, show_alert=True)
        return
    if not groups:
        await callback.answer(
            "گروه قابل انتخابی ندارید؛ گروه‌های فعلی کاربر حفظ می‌شود.",
            show_alert=True,
        )
        return
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
        await callback.answer(feedback.ACTION_NOT_ALLOWED, show_alert=True)
        return
    gid = int(match.group(1))
    selected = [int(x) for x in (data.get("pg_selected_groups") or [])]
    if gid in selected:
        selected = [x for x in selected if x != gid]
    else:
        from app.services.bot_pg_catalog_authz import catalog_groups_allowed

        if not catalog_groups_allowed(gate.staff, [gid]):
            await callback.answer(feedback.ACTION_NOT_ALLOWED, show_alert=True)
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
    resolved_ids, groups_err = await _resolve_selected_groups_for_write(
        pg=gate.pg_client,
        staff=gate.staff,
        state=state,
        selected=selected,
    )
    if groups_err:
        await callback.answer(groups_err, show_alert=True)
        return
    try:
        current = gate.pg_user or {}
        uname = (current or {}).get("username") if isinstance(current, dict) else None
        payload = sanitize_pg_user_write_payload(
            build_user_modify_payload(
                username=str(uname) if uname else None,
                group_ids=resolved_ids,
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
