"""Platform admin plan editor in the bot: viewing, adding and editing plans and the custom service link.

The handlers stay on the admin router: admin.py calls ``register_admin_plan_editor`` where they
were defined, and it registers them in the same order with the same filters, so the dispatcher
tries them exactly as before.
"""

from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.auth import is_platform_admin as _is_admin, require_bot_owner_handler
from app.bot.handlers.admin_states import AdminStates
from app.bot.nav_input import finish_text_step, form_origin
from app.bot.tg_utils import parse_bot_float, parse_bot_int, safe_edit_text
from app.config import get_settings
from app.db.models import BotUser, Plan
from app.services import bot_feedback_texts as feedback
from app.services.formatting import format_toman
from app.services.pasarguard import get_pg
from app.services.plans_catalog import plan_groups_denial, plan_template_denial, role_requires_template
from app.services.plan_panel_groups import PANEL_GROUPS_LABEL, plan_has_pg_link, plan_uses_panel_groups
from app.services.redact import user_safe_error
from app.services.renewal_carry import carry_button_text, CARRY_CODES, carry_status_text, toggle_plan_carry
from app.services.users import get_all_settings, get_setting, on, set_setting

logger = logging.getLogger(__name__)


def _plan_line(p: Plan) -> str:
    flag = "✅" if p.is_active else "⏸"
    if p.pg_template_id:
        link = f"تمپلیت #{p.pg_template_id}"
    elif p.pg_group_ids:
        link = f"گروه {p.pg_group_ids}"
    elif plan_uses_panel_groups(p):
        link = PANEL_GROUPS_LABEL
    else:
        link = "⚠️ بدون اتصال پاسارگارد"
    return (
        f"{flag} <b>#{p.id} {html.escape(p.name)}</b>\n"
        f"💰 {format_toman(p.price, get_settings().currency)} · {link}"
    )


def _plan_needs_link(p: Plan) -> bool:
    return not plan_has_pg_link(p)


async def _render_plans_list(callback: CallbackQuery, session: AsyncSession) -> None:
    from app.bot.handlers.admin_plans import users_fixed_list_view  # admin_plans imports this module

    if callback.message:
        text, markup = await users_fixed_list_view(session)
        await callback.message.edit_text(text, reply_markup=markup)


async def _custom_link_summary(session: AsyncSession) -> tuple[str, InlineKeyboardMarkup]:
    ui = await get_all_settings(session)
    enabled = on(ui.get("custom_plan_enabled"))
    tpl = (ui.get("custom_plan_template_id") or "").strip()
    groups = (ui.get("custom_plan_group_ids") or "").strip()
    if tpl:
        link = f"تمپلیت #{tpl}"
    elif groups:
        link = f"گروه‌ها: {groups}"
    else:
        link = "⚠️ بدون اتصال"
    text = (
        "✨ <b>اتصال پلن دلخواه</b>\n\n"
        f"فروش: {'فعال' if enabled else 'خاموش'}\n"
        f"پاسارگارد: {link}\n\n"
        "قیمت و محدوده → پلن‌ها ← پلن دلخواه (یا وب‌پنل)"
    )
    rows = [
        [
            InlineKeyboardButton(
                text="خاموش کردن فروش" if enabled else "روشن کردن فروش",
                callback_data="adm:custom:toggle",
            )
        ],
        [InlineKeyboardButton(text="تمپلیت", callback_data="adm:custom:picktpl")],
        [InlineKeyboardButton(text="گروه", callback_data="adm:custom:pickgrp")],
    ]
    if tpl or groups:
        rows.append(
            [InlineKeyboardButton(text="حذف اتصال", callback_data="adm:custom:clearlink")]
        )
    rows.append(
        [
            kb.back_button(ui, "adm:plans:aud:users"),
            InlineKeyboardButton(text="پلن‌ها", callback_data="adm:plans"),
        ]
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _plan_detail_text(p: Plan, *, category_name: str | None = None) -> str:
    from app.services.formatting import format_plan_duration_days

    gb = f"{p.data_limit_gb:g} گیگ" if p.data_limit_gb is not None else "نامحدود"
    if p.pg_template_id:
        link = f"تمپلیت #{p.pg_template_id}"
    elif p.pg_group_ids:
        link = f"گروه‌ها: {p.pg_group_ids}"
    elif plan_uses_panel_groups(p):
        link = PANEL_GROUPS_LABEL
    else:
        link = "⚠️ هنوز به تمپلیت/گروه وصل نشده — خرید تحویل نمی‌شود"
    cat = html.escape(category_name) if category_name else "—"
    return (
        f"💎 <b>پلن #{p.id}</b> — {html.escape(p.name)}\n\n"
        f"قیمت: {format_toman(p.price, get_settings().currency)}\n"
        f"مدت: {format_plan_duration_days(p.duration_days)}\n"
        f"حجم: {gb}\n"
        f"برچسب دسته: {cat}\n"
        f"مانده در تمدید: {carry_status_text(p)}\n"
        f"وضعیت: {'فعال' if p.is_active else 'خاموش'}\n"
        f"اتصال پاسارگارد: {link}"
    )


async def _plan_category_label(session: AsyncSession, plan: Plan) -> str | None:
    if not plan.category_id:
        return None
    from app.db.models import PlanCategory

    cat = await session.get(PlanCategory, int(plan.category_id))
    if not cat:
        return None
    name = cat.name or ""
    if not cat.is_active:
        name = f"{name} (خاموش)"
    return name or None


async def _plan_detail_text_for(session: AsyncSession, plan: Plan) -> str:
    return await _plan_detail_text(
        plan, category_name=await _plan_category_label(session, plan)
    )


def _plan_detail_keyboard(p: Plan, ui: dict | None = None) -> InlineKeyboardMarkup:
    """Plan link/toggle/edit actions — inherit fixed/trial kind color."""
    pid = p.id
    kind_id = "shop_kind_trial" if getattr(p, "is_trial", False) else "shop_kind_fixed"
    st = kb._style(ui, kind_id, fallback="primary")
    rows = [
        [kb._ikb("✏️ نام", callback_data=f"adm:plan:edit:name:{pid}", style=st)],
        [kb._ikb("✏️ قیمت", callback_data=f"adm:plan:edit:price:{pid}", style=st)],
        [kb._ikb("✏️ مدت (روز)", callback_data=f"adm:plan:edit:days:{pid}", style=st)],
        [kb._ikb("✏️ حجم (گیگ)", callback_data=f"adm:plan:edit:gb:{pid}", style=st)],
        [kb._ikb("✏️ توضیح", callback_data=f"adm:plan:edit:desc:{pid}", style=st)],
        [kb._ikb("✏️ ترتیب نمایش", callback_data=f"adm:plan:edit:sort:{pid}", style=st)],
        [kb._ikb("✏️ پیشوند نام", callback_data=f"adm:plan:edit:prefix:{pid}", style=st)],
        [kb._ikb("✏️ پسوند نام", callback_data=f"adm:plan:edit:suffix:{pid}", style=st)],
        [
            kb._ikb(
                "🏷 برچسب دسته",
                callback_data=f"adm:plan:catpick:{pid}",
                style=st,
            )
        ],
        [
            kb._ikb(
                "🎨 رنگ دکمه",
                callback_data=f"adm:plan:colorpick:{pid}",
                style=st,
            )
        ],
        *[
            [kb._ikb(carry_button_text(p, kind), callback_data=f"adm:plan:flag:carry:{code}:{p.id}", style=st)]
            for code, kind in CARRY_CODES.items()
        ],
        [
            kb._ikb(
                "⏸ خاموش" if p.is_active else "▶️ روشن",
                callback_data=f"adm:plan:toggle:{p.id}",
                style=st,
            )
        ],
        [
            kb._ikb(
                "📋 اتصال به تمپلیت",
                callback_data=f"adm:plan:picktpl:{p.id}",
                style=st,
            )
        ],
        [
            kb._ikb(
                "📁 اتصال به گروه",
                callback_data=f"adm:plan:pickgrp:{p.id}",
                style=st,
            )
        ],
    ]
    if p.pg_template_id or p.pg_group_ids:
        rows.append(
            [
                kb._ikb(
                    "🧹 حذف اتصال پاسارگارد",
                    callback_data=f"adm:plan:clearlink:{p.id}",
                    style=st,
                )
            ]
        )
    rows.append(
        [
            kb._ikb(
                "🗑 حذف پلن",
                callback_data=f"adm:plan:delask:{p.id}",
                style=_style_danger(ui),
            )
        ]
    )
    rows.append(
        [
            kb.back_button(ui, "adm:plans:aud:users")
        ]
    )
    return InlineKeyboardMarkup(
        inline_keyboard=kb.pack_detail_rows(
            rows, pair_prefixes=("adm:plan:toggle:", "adm:plan:delask:")
        )
    )


def _style_danger(ui: dict | None) -> str | None:
    return kb._style(ui, "reject", fallback="danger")


async def _plan_detail_markup(session: AsyncSession, plan: Plan) -> InlineKeyboardMarkup:
    ui = await get_all_settings(session)
    return _plan_detail_keyboard(plan, ui)


async def _show_user_plan_color_picker(
    target: CallbackQuery | Message,
    *,
    session: AsyncSession,
    callback_prefix: str,
    back_callback: str,
) -> None:
    ui = await get_all_settings(session)
    markup = kb.plan_button_style_picker_keyboard(
        callback_prefix=callback_prefix,
        back_callback=back_callback,
        ui=ui,
    )
    text = "🎨 <b>رنگ دکمه این پلن در ربات</b>\n\n«ارث از دسته» یعنی رنگ دسته؛ اگر دسته نداشت، رنگ نوع پلن در تنظیمات رنگبندی."
    if isinstance(target, CallbackQuery):
        if target.message:
            await safe_edit_text(target.message, text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


async def _pending_user_plan_finish(
    session: AsyncSession,
    state: FSMContext,
    *,
    button_style: str | None,
) -> Plan:
    data = await state.get_data()
    tpl_id = data.get("pending_tpl_id")
    group_ids = data.get("pending_group_ids")
    if tpl_id is not None:
        return await _finish_new_plan(
            session,
            state,
            template_id=int(tpl_id),
            group_ids=None,
            button_style=button_style,
        )
    if group_ids:
        return await _finish_new_plan(
            session,
            state,
            template_id=None,
            group_ids=str(group_ids),
            button_style=button_style,
        )
    raise ValueError("pending PG link missing")


@require_bot_owner_handler
async def adm_plan_view(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    plan = await session.get(Plan, int(callback.data.split(":")[-1]))
    if not plan:
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            await _plan_detail_text_for(session, plan),
            reply_markup=await _plan_detail_markup(session, plan),
        )


@require_bot_owner_handler
async def adm_plan_edit_ask(
    callback: CallbackQuery, state: FSMContext, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) < 5:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    field, pid_raw = parts[3], parts[4]
    if field not in {"name", "price", "days", "gb", "desc", "sort", "prefix", "suffix"}:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    prompts = {
        "name": "نام جدید پلن:",
        "price": "قیمت جدید را به تومان بفرستید:",
        "days": "مدت را به روز بفرستید. برای نامحدود <code>0</code> بفرستید:",
        "gb": "حجم را به گیگابایت بفرستید. برای نامحدود <code>0</code> بفرستید:",
        "desc": "توضیح (خالی = حذف):",
        "sort": "ترتیب نمایش را با عدد بفرستید؛ عدد کوچک‌تر بالاتر نمایش داده می‌شود:",
        "prefix": "پیشوند نام کاربری پاسارگارد (خالی = پیش‌فرض):",
        "suffix": "پسوند نام کاربری پاسارگارد (خالی = حذف):",
    }
    await state.update_data(
        user_plan_edit_id=int(pid_raw),
        user_plan_edit_field=field,
        _adm_plans_aud="users",
        _adm_plans_kind="fixed",
    )
    await callback.answer()
    from app.bot.nav_input import ask_text

    await ask_text(
        callback,
        state,
        prompt=prompts[field],
        cancel_code="adm_pln",
        return_to=f"adm:plan:view:{pid_raw}",
        fsm_state=AdminStates.plan_edit_field,
        edit=True,
    )


@require_bot_owner_handler
async def adm_plan_edit_save(  # noqa: C901 - moved unchanged from admin.py, where it had this finding
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await state.clear()
        return
    if kb.is_cancel_text(message.text):
        from app.bot.nav_input import try_legacy_cancel

        if await try_legacy_cancel(message, state, session, db_user):
            return
        await state.clear()
        from app.bot.handlers.admin_plans import _answer_plans_cancel

        await _answer_plans_cancel(message, state, session)
        return
    data = await state.get_data()
    plan = await session.get(Plan, int(data.get("user_plan_edit_id") or 0))
    field = data.get("user_plan_edit_field")
    if not plan or not field:
        await state.clear()
        return
    text = (message.text or "").strip()
    try:
        if field == "name":
            if not text:
                await message.answer(feedback.NAME_REQUIRED)
                return
            plan.name = text[:128]
        elif field == "price":
            plan.price = max(0, parse_bot_int(text))
        elif field == "days":
            # 0 or empty → unlimited duration (matches panel)
            if not text:
                plan.duration_days = 0
            else:
                plan.duration_days = max(0, parse_bot_int(text))
        elif field == "gb":
            gb = parse_bot_float(text)
            plan.data_limit_gb = None if gb <= 0 else gb
        elif field == "desc":
            plan.description = text or None
        elif field == "sort":
            plan.sort_order = parse_bot_int(text, default=0)
        elif field == "prefix":
            plan.pg_username_prefix = text[:64] or None
        elif field == "suffix":
            plan.pg_username_suffix = text[:64] or None
    except ValueError:
        await message.answer(feedback.ENTER_VALID_NUMBER)
        return
    await session.commit()
    await state.set_state(None)
    # The plan card, with the outcome on top, replaces the prompt.
    await finish_text_step(
        message,
        state,
        text=f"{feedback.SAVED_CHECK}\n\n{await _plan_detail_text_for(session, plan)}",
        inline=await _plan_detail_markup(session, plan),
    )


@require_bot_owner_handler
async def adm_plan_add(callback: CallbackQuery, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    await callback.answer()
    await state.update_data(_adm_plans_aud="users", _adm_plans_kind="fixed")
    from app.bot.nav_input import ask_text

    await ask_text(
        callback,
        state,
        prompt=feedback.ENTER_PLAN_NAME,
        cancel_code="adm_pln",
        return_to="adm:plans:kind:users:fixed",
        fsm_state=AdminStates.add_plan_name,
        edit=True,
    )


@require_bot_owner_handler
async def plan_name(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await state.clear()
        await message.answer(feedback.NOT_ADMIN)
        return
    if kb.is_cancel_text(message.text):
        from app.bot.nav_input import try_legacy_cancel

        if await try_legacy_cancel(message, state, session, db_user):
            return
        await state.clear()
        from app.bot.handlers.admin_plans import _answer_plans_cancel

        await _answer_plans_cancel(message, state, session)
        return
    await state.update_data(name=(message.text or "").strip())
    from app.bot.nav_input import ask_text

    await ask_text(
        message,
        state,
        prompt=feedback.ENTER_PRICE_TOMAN,
        cancel_code="adm_pln",
        return_to=await form_origin(state),
        fsm_state=AdminStates.add_plan_price,
        edit=False,
    )


@require_bot_owner_handler
async def plan_price(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await state.clear()
        await message.answer(feedback.NOT_ADMIN)
        return
    if kb.is_cancel_text(message.text):
        from app.bot.nav_input import try_legacy_cancel

        if await try_legacy_cancel(message, state, session, db_user):
            return
        await state.clear()
        from app.bot.handlers.admin_plans import _answer_plans_cancel

        await _answer_plans_cancel(message, state, session)
        return
    try:
        price = max(0, parse_bot_int(message.text))
    except ValueError:
        await message.answer(feedback.ENTER_ONE_VALID_NUMBER)
        return
    await state.update_data(price=price)
    from app.bot.nav_input import ask_text

    await ask_text(
        message,
        state,
        prompt="مدت اعتبار به روز را بفرستید:\n<code>0</code> یا خالی = نامحدود",
        cancel_code="adm_pln",
        return_to=await form_origin(state),
        fsm_state=AdminStates.add_plan_days,
        edit=False,
    )


@require_bot_owner_handler
async def plan_days(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await state.clear()
        await message.answer(feedback.NOT_ADMIN)
        return
    if kb.is_cancel_text(message.text):
        from app.bot.nav_input import try_legacy_cancel

        if await try_legacy_cancel(message, state, session, db_user):
            return
        await state.clear()
        from app.bot.handlers.admin_plans import _answer_plans_cancel

        await _answer_plans_cancel(message, state, session)
        return
    raw = (message.text or "").strip()
    try:
        days = 0 if not raw else max(0, parse_bot_int(raw))
    except ValueError:
        await message.answer(feedback.ENTER_ONE_VALID_NUMBER)
        return
    await state.update_data(days=days)
    from app.bot.nav_input import ask_text

    await ask_text(
        message,
        state,
        prompt=feedback.ENTER_VOLUME_GB,
        cancel_code="adm_pln",
        return_to=await form_origin(state),
        fsm_state=AdminStates.add_plan_gb,
        edit=False,
    )


@require_bot_owner_handler
async def plan_gb(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await state.clear()
        await message.answer(feedback.NOT_ADMIN)
        return
    if kb.is_cancel_text(message.text):
        from app.bot.nav_input import try_legacy_cancel

        if await try_legacy_cancel(message, state, session, db_user):
            return
        await state.clear()
        from app.bot.handlers.admin_plans import _answer_plans_cancel

        await _answer_plans_cancel(message, state, session)
        return
    try:
        gb = parse_bot_float(message.text)
    except ValueError:
        await message.answer(feedback.ENTER_ONE_VALID_NUMBER)
        return
    await state.update_data(gb=None if gb <= 0 else gb)
    await state.set_state(AdminStates.add_plan_link)
    staff = await _platform_catalog_staff(session, db_user)
    await message.answer(
        "اتصال پاسارگارد را انتخاب کنید:",
        reply_markup=_plan_link_markup(staff),
    )


@require_bot_owner_handler
async def plan_link_cancel(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    """Allow reply-keyboard انصراف while waiting for inline PG mode pick."""
    if not _is_admin(db_user):
        await state.clear()
        await message.answer(feedback.NOT_ADMIN)
        return
    if kb.is_cancel_text(message.text):
        from app.bot.nav_input import try_legacy_cancel

        if await try_legacy_cancel(message, state, session, db_user):
            return
        await state.clear()
        from app.bot.handlers.admin_plans import _answer_plans_cancel

        await _answer_plans_cancel(message, state, session)
        return
    await message.answer("اتصال را از دکمه‌های زیر پیام انتخاب کنید، یا انصراف بزنید.")


async def _finish_new_plan(
    session: AsyncSession,
    state: FSMContext,
    *,
    template_id: int | None = None,
    group_ids: str | None = None,
    button_style: str | None = None,
) -> Plan:
    data = await state.get_data()
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError("plan name missing from FSM")
    price = int(data.get("price") or 0)
    days = int(data.get("days") or 30)
    cat_raw = data.get("pending_category_id")
    category_id = int(cat_raw) if cat_raw not in (None, "", 0, "0") else None
    plan = Plan(
        name=name[:128],
        price=max(0, price),
        duration_days=max(0, days),
        data_limit_gb=data.get("gb"),
        pg_template_id=template_id,
        pg_group_ids=group_ids,
        button_style=button_style,
        category_id=category_id,
        is_active=True,
    )
    session.add(plan)
    await session.commit()
    await session.refresh(plan)
    await state.set_state(None)
    await state.update_data(
        new_plan=0,
        selected_groups=[],
        pending_tpl_id=None,
        pending_group_ids=None,
        pending_category_id=None,
        pending_button_style=None,
        name=None,
        price=None,
        days=None,
        gb=None,
        _adm_plans_aud="users",
        _adm_plans_kind="fixed",
    )
    return plan


@require_bot_owner_handler
async def adm_plan_new_tpl(callback: CallbackQuery, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    if await state.get_state() != AdminStates.add_plan_link.state:
        await callback.answer(feedback.START_PLAN_CREATION_FIRST, show_alert=True)
        return
    await callback.answer()
    await state.update_data(new_plan=1)
    await _show_template_picker(callback, plan_id=0)


@require_bot_owner_handler
async def adm_plan_new_grp(callback: CallbackQuery, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    if await state.get_state() != AdminStates.add_plan_link.state:
        await callback.answer(feedback.START_PLAN_CREATION_FIRST, show_alert=True)
        return
    await callback.answer()
    await state.update_data(new_plan=1, selected_groups=[])
    await _show_group_picker(callback, state, plan_id=0)


async def _show_template_picker(callback: CallbackQuery, *, plan_id: int) -> None:
    try:
        templates = await get_pg().get_user_templates_simple()
    except Exception:
        templates = []
    rows: list[list[InlineKeyboardButton]] = []
    for t in templates[:20]:
        tid = t.get("id")
        if tid is None:
            continue
        name = t.get("name") or f"تمپلیت {tid}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"#{tid} — {name}"[:60],
                    callback_data=f"adm:plan:settpl:{plan_id}:{tid}",
                )
            ]
        )
    if not rows:
        rows.append(
            [InlineKeyboardButton(text="هنوز تمپلیتی نیست؛ از وب‌پنل بسازید", callback_data="adm:plans")]
        )
    back = f"adm:plan:view:{plan_id}" if plan_id else "adm:plans"
    rows.append([kb.back_button(None, back)])
    text = "📋 یک تمپلیت پاسارگارد انتخاب کنید:"
    if callback.message:
        await safe_edit_text(
            callback.message,
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )


async def _show_group_picker(callback: CallbackQuery, state: FSMContext, *, plan_id: int) -> None:
    data = await state.get_data()
    selected = [int(x) for x in (data.get("selected_groups") or [])]
    try:
        groups = await get_pg().get_groups_simple()
    except Exception:
        groups = []
    rows: list[list[InlineKeyboardButton]] = []
    for g in groups[:25]:
        gid = g.get("id")
        if gid is None:
            continue
        try:
            gid = int(gid)
        except (TypeError, ValueError):
            continue
        mark = "✅ " if gid in selected else ""
        name = g.get("name") or f"گروه {gid}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark}#{gid} — {name}"[:60],
                    callback_data=f"adm:plan:toggrp:{plan_id}:{gid}",
                )
            ]
        )
    if not rows:
        rows.append(
            [InlineKeyboardButton(text="هنوز گروهی نیست؛ از وب‌پنل بسازید", callback_data="adm:plans")]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"✅ تأیید انتخاب ({len(selected)})",
                    callback_data=f"adm:plan:grpdone:{plan_id}",
                )
            ]
        )
    back = f"adm:plan:view:{plan_id}" if plan_id else "adm:plans"
    rows.append([kb.back_button(None, back)])
    text = (
        "📁 گروه‌های اینباند را انتخاب کنید (می‌توانید چندتا بزنید):\n"
        f"انتخاب‌شده: {', '.join(str(x) for x in selected) or '—'}"
    )
    if callback.message:
        await safe_edit_text(
            callback.message,
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )


@require_bot_owner_handler
async def adm_plan_pick_tpl(callback: CallbackQuery, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    plan_id = int(callback.data.split(":")[-1])
    await callback.answer()
    await state.update_data(new_plan=0, selected_groups=[])
    await _show_template_picker(callback, plan_id=plan_id)


@require_bot_owner_handler
async def adm_plan_pick_grp(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    plan_id = int(callback.data.split(":")[-1])
    plan = await session.get(Plan, plan_id)
    selected: list[int] = []
    if plan and plan.pg_group_ids:
        for part in str(plan.pg_group_ids).split(","):
            part = part.strip()
            if part.isdigit():
                selected.append(int(part))
    await callback.answer()
    await state.update_data(new_plan=0, selected_groups=selected)
    await _show_group_picker(callback, state, plan_id=plan_id)


@require_bot_owner_handler
async def adm_plan_set_tpl(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    parts = callback.data.split(":")
    plan_id = int(parts[-2])
    tpl_id = int(parts[-1])
    template_denial = plan_template_denial(await _platform_catalog_staff(session, db_user), tpl_id)
    if template_denial:
        await callback.answer(template_denial, show_alert=True)
        return
    data = await state.get_data()
    creating = bool(data.get("new_plan") or plan_id == 0)
    if creating:
        await state.update_data(
            pending_tpl_id=tpl_id,
            pending_group_ids=None,
        )
        await state.set_state(AdminStates.add_plan_color)
        await callback.answer()
        await _show_user_plan_color_picker(
            callback,
            session=session,
            callback_prefix="adm:plan:newcolor",
            back_callback="adm:plan:new:backlink",
        )
        return
    plan = await session.get(Plan, plan_id)
    if not plan:
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
        return
    plan.pg_template_id = tpl_id
    plan.pg_group_ids = None
    await session.commit()
    await state.update_data(new_plan=0, selected_groups=[])
    await callback.answer(feedback.SAVED)
    if callback.message:
        await safe_edit_text(
            callback.message,
            await _plan_detail_text(plan),
            reply_markup=await _plan_detail_markup(session, plan),
        )


@require_bot_owner_handler
async def adm_plan_tog_grp(callback: CallbackQuery, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    parts = callback.data.split(":")
    plan_id = int(parts[-2])
    gid = int(parts[-1])
    data = await state.get_data()
    selected = [int(x) for x in (data.get("selected_groups") or [])]
    if gid in selected:
        selected = [x for x in selected if x != gid]
    else:
        selected.append(gid)
    await state.update_data(selected_groups=selected)
    await callback.answer()
    await _show_group_picker(callback, state, plan_id=plan_id)


@require_bot_owner_handler
async def adm_plan_grp_done(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    plan_id = int(callback.data.split(":")[-1])
    data = await state.get_data()
    selected = [int(x) for x in (data.get("selected_groups") or [])]
    if not selected:
        await callback.answer(feedback.PICK_AT_LEAST_ONE_GROUP, show_alert=True)
        return
    groups_denial = plan_groups_denial(await _platform_catalog_staff(session, db_user), selected)
    if groups_denial:
        await callback.answer(groups_denial, show_alert=True)
        return
    group_csv = ",".join(str(x) for x in selected)
    creating = bool(data.get("new_plan") or plan_id == 0)
    if creating:
        await state.update_data(
            pending_tpl_id=None,
            pending_group_ids=group_csv,
        )
        await state.set_state(AdminStates.add_plan_color)
        await callback.answer()
        await _show_user_plan_color_picker(
            callback,
            session=session,
            callback_prefix="adm:plan:newcolor",
            back_callback="adm:plan:new:backlink",
        )
        return
    plan = await session.get(Plan, plan_id)
    if not plan:
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
        return
    plan.pg_template_id = None
    plan.pg_group_ids = group_csv
    await session.commit()
    await state.update_data(selected_groups=[], new_plan=0)
    await callback.answer(feedback.SAVED)
    if callback.message:
        await safe_edit_text(
            callback.message,
            await _plan_detail_text(plan),
            reply_markup=await _plan_detail_markup(session, plan),
        )


@require_bot_owner_handler
async def adm_plan_new_back_link(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    await state.set_state(AdminStates.add_plan_link)
    await callback.answer()
    if callback.message:
        staff = await _platform_catalog_staff(session, db_user)
        await safe_edit_text(
            callback.message,
            "اتصال پاسارگارد را انتخاب کنید:",
            reply_markup=_plan_link_markup(staff),
        )


def _plan_link_markup(staff: dict | None) -> InlineKeyboardMarkup:
    """Template / group choice; the group button is left out when the PasarGuard role forbids it."""
    rows = [[InlineKeyboardButton(text="📋 از تمپلیت", callback_data="adm:plan:new:mode:tpl")]]
    if not role_requires_template(staff):
        rows.append([InlineKeyboardButton(text="📁 با گروه (سفارشی)", callback_data="adm:plan:new:mode:grp")])
    rows.append([InlineKeyboardButton(text="❌ انصراف", callback_data="adm:plans")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _platform_catalog_staff(
    session: AsyncSession, db_user: BotUser
) -> dict | None:
    from app.bot.auth import resolve_bot_principal_bridge
    from app.services.shop_scope import is_platform_admin as staff_is_platform_admin

    bridge = await resolve_bot_principal_bridge(
        session, db_user, is_reseller_bot=False
    )
    if not bridge or not bridge.staff or not staff_is_platform_admin(bridge.staff):
        return None
    return bridge.staff


async def _show_plan_category_picker(
    target: CallbackQuery,
    session: AsyncSession,
    staff: dict,
    *,
    plan_id: int | None,
    selected_id: int | None = None,
) -> None:
    """Inline category picker — plan_id None = new-plan flow (adm:plan:newcat)."""
    from app.services.plan_categories import list_categories

    cats = await list_categories(session, staff, active_only=False)
    active = [c for c in cats if c.is_active]
    if selected_id:
        # Keep currently assigned inactive category visible on edit.
        for c in cats:
            if int(c.id) == int(selected_id) and c not in active:
                active.append(c)
                break
    rows: list[list[InlineKeyboardButton]] = []
    if plan_id is None:
        rows.append(
            [InlineKeyboardButton(text="بدون دسته", callback_data="adm:plan:newcat:0")]
        )
        for c in active[:20]:
            rows.append(  # noqa: PERF401 - moved unchanged from admin.py, where it had this finding
                [
                    InlineKeyboardButton(
                        text=c.name[:48],
                        callback_data=f"adm:plan:newcat:{c.id}",
                        icon_custom_emoji_id=getattr(c, "icon_custom_emoji_id", None),
                    )
                ]
            )
        rows.append(
            [
                kb.back_button(None, "adm:plan:new:backcolor")
            ]
        )
    else:
        prefix = f"adm:plan:setcat:{plan_id}"
        mark0 = "✅ " if not selected_id else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark0}بدون دسته",
                    callback_data=f"{prefix}:0",
                )
            ]
        )
        for c in active[:20]:
            mark = "✅ " if selected_id and int(c.id) == int(selected_id) else ""
            suffix = "" if c.is_active else " (خاموش)"
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"{mark}{c.name}{suffix}"[:48],
                        callback_data=f"{prefix}:{c.id}",
                        icon_custom_emoji_id=getattr(c, "icon_custom_emoji_id", None),
                    )
                ]
            )
        rows.append(
            [
                kb.back_button(None, f"adm:plan:view:{plan_id}")
            ]
        )
    if target.message:
        await safe_edit_text(
            target.message,
            "🏷 <b>برچسب دسته</b> (اختیاری)\n"
            "نوع پلن ثابت می‌ماند — این فقط برچسب نمایش در فروشگاه است.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )


@require_bot_owner_handler
async def adm_plan_new_color(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    if await state.get_state() != AdminStates.add_plan_color.state:
        await callback.answer("ابتدا اتصال پاسارگارد را انتخاب کنید", show_alert=True)
        return
    from app.services.button_styles import parse_plan_button_style_callback

    style = parse_plan_button_style_callback(callback.data.rsplit(":", 1)[-1])
    await state.update_data(pending_button_style=style)
    await state.set_state(AdminStates.add_plan_category)
    staff = await _platform_catalog_staff(session, db_user)
    if not staff:
        await callback.answer(feedback.SYSTEM_OWNER_REQUIRED, show_alert=True)
        return
    await callback.answer()
    await _show_plan_category_picker(
        callback, session, staff, plan_id=None, selected_id=None
    )


@require_bot_owner_handler
async def adm_plan_new_back_color(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    await state.set_state(AdminStates.add_plan_color)
    await callback.answer()
    await _show_user_plan_color_picker(
        callback,
        session=session,
        callback_prefix="adm:plan:newcolor",
        back_callback="adm:plan:new:backlink",
    )


@require_bot_owner_handler
async def adm_plan_new_category(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    staff = await _platform_catalog_staff(session, db_user)
    if not staff:
        await callback.answer(feedback.SYSTEM_OWNER_REQUIRED, show_alert=True)
        return
    from app.services.plan_categories import resolve_category_for_plan_write
    from app.services.shop_scope import ShopScopeError

    raw = callback.data.rsplit(":", 1)[-1]
    try:
        if raw in {"0", ""}:
            cat_id = None
        else:
            cat_id = await resolve_category_for_plan_write(session, staff, raw)
    except (ShopScopeError, ValueError) as e:
        await callback.answer(user_safe_error(e, limit=160), show_alert=True)
        return
    data = await state.get_data()
    style = data.get("pending_button_style")
    await state.update_data(pending_category_id=cat_id)
    try:
        plan = await _pending_user_plan_finish(
            session, state, button_style=style
        )
    except Exception:
        await callback.answer("ساخت پلن انجام نشد. دوباره تلاش کنید.", show_alert=True)
        return
    await callback.answer(feedback.SAVED)
    if callback.message:
        link_note = ""
        if plan.pg_template_id:
            link_note = f" با تمپلیت #{plan.pg_template_id}"
        elif plan.pg_group_ids:
            link_note = f" با گروه(ها) {plan.pg_group_ids}"
        text = (
            f"پلن #{plan.id}{link_note} ساخته شد ✅\n\n"
            + await _plan_detail_text_for(session, plan)
        )
        await safe_edit_text(
            callback.message,
            text,
            reply_markup=await _plan_detail_markup(session, plan),
        )


@require_bot_owner_handler
async def adm_plan_cat_pick(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    staff = await _platform_catalog_staff(session, db_user)
    if not staff:
        await callback.answer(feedback.SYSTEM_OWNER_REQUIRED, show_alert=True)
        return
    pid = int(callback.data.rsplit(":", 1)[-1])
    plan = await session.get(Plan, pid)
    if not plan or plan.owner_reseller_id is not None:
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
        return
    await callback.answer()
    await _show_plan_category_picker(
        callback,
        session,
        staff,
        plan_id=pid,
        selected_id=int(plan.category_id) if plan.category_id else None,
    )


@require_bot_owner_handler
async def adm_plan_set_cat(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    staff = await _platform_catalog_staff(session, db_user)
    if not staff:
        await callback.answer(feedback.SYSTEM_OWNER_REQUIRED, show_alert=True)
        return
    parts = callback.data.split(":")
    # adm:plan:setcat:{pid}:{cid}
    if len(parts) < 5:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    pid = int(parts[3])
    raw = parts[4]
    plan = await session.get(Plan, pid)
    if not plan or plan.owner_reseller_id is not None:
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
        return
    from app.services.plan_categories import resolve_category_for_plan_write
    from app.services.shop_scope import ShopScopeError

    try:
        if raw in {"0", ""}:
            plan.category_id = None
        else:
            plan.category_id = await resolve_category_for_plan_write(
                session,
                staff,
                raw,
                allow_inactive_id=plan.category_id,
            )
        await session.commit()
    except (ShopScopeError, ValueError) as e:
        await callback.answer(user_safe_error(e, limit=160), show_alert=True)
        return
    await callback.answer(feedback.SAVED)
    if callback.message:
        await safe_edit_text(
            callback.message,
            await _plan_detail_text_for(session, plan),
            reply_markup=await _plan_detail_markup(session, plan),
        )


@require_bot_owner_handler
async def adm_plan_color_pick(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    pid = int(callback.data.rsplit(":", 1)[-1])
    plan = await session.get(Plan, pid)
    if not plan:
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
        return
    await callback.answer()
    await _show_user_plan_color_picker(
        callback,
        session=session,
        callback_prefix=f"adm:plan:setcolor:{pid}",
        back_callback=f"adm:plan:view:{pid}",
    )


@require_bot_owner_handler
async def adm_plan_set_color(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) < 5:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    pid = int(parts[3])
    from app.services.button_styles import parse_plan_button_style_callback

    plan = await session.get(Plan, pid)
    if not plan:
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
        return
    plan.button_style = parse_plan_button_style_callback(parts[4])
    await session.commit()
    await callback.answer(feedback.COLOR_SAVED)
    if callback.message:
        await safe_edit_text(
            callback.message,
            await _plan_detail_text(plan),
            reply_markup=await _plan_detail_markup(session, plan),
        )


@require_bot_owner_handler
async def adm_plan_clear_link(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    plan = await session.get(Plan, int(callback.data.split(":")[-1]))
    if not plan:
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
        return
    plan.pg_template_id = None
    plan.pg_group_ids = None
    await session.commit()
    await callback.answer(feedback.CONNECTION_DELETED)
    if callback.message:
        await callback.message.edit_text(
            await _plan_detail_text(plan),
            reply_markup=await _plan_detail_markup(session, plan),
        )


@require_bot_owner_handler
async def plan_toggle(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    plan = await session.get(Plan, int(callback.data.split(":")[-1]))
    if not plan:
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
        return
    plan.is_active = not plan.is_active
    await session.commit()
    await callback.answer(feedback.UPDATED)
    # Prefer detail view if we came from there; otherwise list
    if callback.message and callback.message.reply_markup:
        # re-render detail when possible
        try:
            await callback.message.edit_text(
                await _plan_detail_text(plan),
                reply_markup=await _plan_detail_markup(session, plan),
            )
            return
        except Exception:
            logger.debug("plan_toggle: ignored failure", exc_info=True)
    await _render_plans_list(callback, session)


@require_bot_owner_handler
async def plan_carry_toggle(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    _, _, _, _, code, plan_id = callback.data.split(":")
    plan = await session.get(Plan, int(plan_id))
    if not plan or plan.is_trial:
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
        return
    toggle_plan_carry(plan, CARRY_CODES[code])
    await session.commit()
    await callback.answer(feedback.UPDATED)
    if callback.message:
        await callback.message.edit_text(
            await _plan_detail_text_for(session, plan),
            reply_markup=await _plan_detail_markup(session, plan),
        )


@require_bot_owner_handler
async def adm_plan_del_ask(callback: CallbackQuery, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    pid = int(callback.data.rsplit(":", 1)[-1])
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            "⚠️ این پلن از فروشگاه حذف شود؟",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🗑 تأیید حذف",
                            callback_data=f"adm:plan:del:{pid}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="انصراف",
                            callback_data=f"adm:plan:view:{pid}",
                        )
                    ],
                ]
            ),
        )


@require_bot_owner_handler
async def adm_plan_del(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    from app.services.plans_catalog import PlanDeleteBlocked, delete_shop_plan

    pid = int(callback.data.rsplit(":", 1)[-1])
    plan = await session.get(Plan, pid)
    if not plan or plan.is_trial:
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
        return
    try:
        await delete_shop_plan(session, plan)
        await session.commit()
    except PlanDeleteBlocked as e:
        try:
            await session.rollback()
        except Exception:
            logger.debug("adm_plan_del: ignored failure", exc_info=True)
        msg = e.message if len(e.message) <= 180 else e.message[:177] + "…"
        await callback.answer(msg, show_alert=True)
        return
    await callback.answer(feedback.DELETED)
    await _render_plans_list(callback, session)


@require_bot_owner_handler
async def adm_custom(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    await callback.answer()
    text, markup = await _custom_link_summary(session)
    if callback.message:
        await callback.message.edit_text(text, reply_markup=markup)


@require_bot_owner_handler
async def adm_custom_toggle(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    cur = await get_setting(session, "custom_plan_enabled")
    await set_setting(session, "custom_plan_enabled", "0" if on(cur) else "1")
    await callback.answer(feedback.UPDATED)
    text, markup = await _custom_link_summary(session)
    if callback.message:
        await callback.message.edit_text(text, reply_markup=markup)


@require_bot_owner_handler
async def adm_custom_clear(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    await set_setting(session, "custom_plan_template_id", "")
    await set_setting(session, "custom_plan_group_ids", "")
    await callback.answer(feedback.CONNECTION_DELETED)
    text, markup = await _custom_link_summary(session)
    if callback.message:
        await callback.message.edit_text(text, reply_markup=markup)


async def _show_custom_template_picker(callback: CallbackQuery) -> None:
    try:
        templates = await get_pg().get_user_templates_simple()
    except Exception:
        templates = []
    rows: list[list[InlineKeyboardButton]] = []
    for t in templates[:20]:
        tid = t.get("id")
        if tid is None:
            continue
        name = t.get("name") or f"تمپلیت {tid}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"#{tid} — {name}"[:60],
                    callback_data=f"adm:custom:settpl:{tid}",
                )
            ]
        )
    if not rows:
        rows.append(
            [InlineKeyboardButton(text="هنوز تمپلیتی نیست؛ از وب‌پنل بسازید", callback_data="adm:custom")]
        )
    rows.append([kb.back_button(None, "adm:custom")])
    if callback.message:
        await callback.message.edit_text(
            "📋 تمپلیت پاسارگارد برای پلن دلخواه:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )


async def _show_custom_group_picker(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    selected = [int(x) for x in (data.get("custom_selected_groups") or [])]
    try:
        groups = await get_pg().get_groups_simple()
    except Exception:
        groups = []
    rows: list[list[InlineKeyboardButton]] = []
    for g in groups[:25]:
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
                    callback_data=f"adm:custom:toggrp:{gid}",
                )
            ]
        )
    if not rows:
        rows.append(
            [InlineKeyboardButton(text="هنوز گروهی نیست؛ از وب‌پنل بسازید", callback_data="adm:custom")]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"✅ تأیید انتخاب ({len(selected)})",
                    callback_data="adm:custom:grpdone",
                )
            ]
        )
    rows.append([kb.back_button(None, "adm:custom")])
    text = (
        "📁 گروه‌های اینباند برای پلن دلخواه:\n"
        f"انتخاب‌شده: {', '.join(str(x) for x in selected) or '—'}"
    )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@require_bot_owner_handler
async def adm_custom_pick_tpl(callback: CallbackQuery, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    await callback.answer()
    await _show_custom_template_picker(callback)


@require_bot_owner_handler
async def adm_custom_set_tpl(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    tpl_id = callback.data.split(":")[-1]
    await set_setting(session, "custom_plan_template_id", tpl_id)
    await set_setting(session, "custom_plan_group_ids", "")
    await callback.answer(feedback.SAVED)
    text, markup = await _custom_link_summary(session)
    if callback.message:
        await callback.message.edit_text(text, reply_markup=markup)


@require_bot_owner_handler
async def adm_custom_pick_grp(callback: CallbackQuery, session: AsyncSession, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    groups_raw = (await get_setting(session, "custom_plan_group_ids") or "").strip()
    selected: list[int] = []
    for part in groups_raw.split(","):
        part = part.strip()
        if part.isdigit():
            selected.append(int(part))
    await state.update_data(custom_selected_groups=selected)
    await callback.answer()
    await _show_custom_group_picker(callback, state)


@require_bot_owner_handler
async def adm_custom_tog_grp(callback: CallbackQuery, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    gid = int(callback.data.split(":")[-1])
    data = await state.get_data()
    selected = [int(x) for x in (data.get("custom_selected_groups") or [])]
    if gid in selected:
        selected = [x for x in selected if x != gid]
    else:
        selected.append(gid)
    await state.update_data(custom_selected_groups=selected)
    await callback.answer()
    await _show_custom_group_picker(callback, state)


@require_bot_owner_handler
async def adm_custom_grp_done(callback: CallbackQuery, session: AsyncSession, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    data = await state.get_data()
    selected = [int(x) for x in (data.get("custom_selected_groups") or [])]
    if not selected:
        await callback.answer(feedback.PICK_AT_LEAST_ONE_GROUP, show_alert=True)
        return
    await set_setting(session, "custom_plan_group_ids", ",".join(str(x) for x in selected))
    await set_setting(session, "custom_plan_template_id", "")
    await state.update_data(custom_selected_groups=[])
    await callback.answer(feedback.SAVED)
    text, markup = await _custom_link_summary(session)
    if callback.message:
        await callback.message.edit_text(text, reply_markup=markup)


def register_admin_plan_editor(router: Router) -> None:
    """Register the handlers on ``router`` in their original order."""
    router.callback_query.register(adm_plan_view, F.data.startswith("adm:plan:view:"))
    router.callback_query.register(adm_plan_edit_ask, F.data.startswith("adm:plan:edit:"))
    router.message.register(adm_plan_edit_save, AdminStates.plan_edit_field)
    router.callback_query.register(adm_plan_add, F.data == "adm:plan:add")
    router.message.register(plan_name, AdminStates.add_plan_name)
    router.message.register(plan_price, AdminStates.add_plan_price)
    router.message.register(plan_days, AdminStates.add_plan_days)
    router.message.register(plan_gb, AdminStates.add_plan_gb)
    router.message.register(plan_link_cancel, AdminStates.add_plan_link)
    router.callback_query.register(adm_plan_new_tpl, F.data == "adm:plan:new:mode:tpl")
    router.callback_query.register(adm_plan_new_grp, F.data == "adm:plan:new:mode:grp")
    router.callback_query.register(adm_plan_pick_tpl, F.data.startswith("adm:plan:picktpl:"))
    router.callback_query.register(adm_plan_pick_grp, F.data.startswith("adm:plan:pickgrp:"))
    router.callback_query.register(adm_plan_set_tpl, F.data.startswith("adm:plan:settpl:"))
    router.callback_query.register(adm_plan_tog_grp, F.data.startswith("adm:plan:toggrp:"))
    router.callback_query.register(adm_plan_grp_done, F.data.startswith("adm:plan:grpdone:"))
    router.callback_query.register(adm_plan_new_back_link, F.data == "adm:plan:new:backlink")
    router.callback_query.register(adm_plan_new_color, F.data.startswith("adm:plan:newcolor:"))
    router.callback_query.register(adm_plan_new_back_color, F.data == "adm:plan:new:backcolor")
    router.callback_query.register(adm_plan_new_category, F.data.startswith("adm:plan:newcat:"), AdminStates.add_plan_category)
    router.callback_query.register(adm_plan_cat_pick, F.data.startswith("adm:plan:catpick:"))
    router.callback_query.register(adm_plan_set_cat, F.data.startswith("adm:plan:setcat:"))
    router.callback_query.register(adm_plan_color_pick, F.data.startswith("adm:plan:colorpick:"))
    router.callback_query.register(adm_plan_set_color, F.data.startswith("adm:plan:setcolor:"))
    router.callback_query.register(adm_plan_clear_link, F.data.startswith("adm:plan:clearlink:"))
    router.callback_query.register(plan_toggle, F.data.startswith("adm:plan:toggle:"))
    router.callback_query.register(plan_carry_toggle, F.data.regexp(r"^adm:plan:flag:carry:[vt]:\d+$"))
    router.callback_query.register(adm_plan_del_ask, F.data.startswith("adm:plan:delask:"))
    router.callback_query.register(adm_plan_del, F.data.startswith("adm:plan:del:"))
    router.callback_query.register(adm_custom, F.data == "adm:custom")
    router.callback_query.register(adm_custom_toggle, F.data == "adm:custom:toggle")
    router.callback_query.register(adm_custom_clear, F.data == "adm:custom:clearlink")
    router.callback_query.register(adm_custom_pick_tpl, F.data == "adm:custom:picktpl")
    router.callback_query.register(adm_custom_set_tpl, F.data.startswith("adm:custom:settpl:"))
    router.callback_query.register(adm_custom_pick_grp, F.data == "adm:custom:pickgrp")
    router.callback_query.register(adm_custom_tog_grp, F.data.startswith("adm:custom:toggrp:"))
    router.callback_query.register(adm_custom_grp_done, F.data == "adm:custom:grpdone")
