"""Reseller sales-plan management via Telegram bot."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.tg_utils import safe_edit_text
from app.db.models import BotUser, Plan, ResellerProfile
from app.services.plans_catalog import (
    groups_allowed_for_staff,
    load_pg_plan_options,
    template_allowed_for_staff,
)
from app.services.resellers import has_bot_perm

router = Router(name="reseller_plans")


class ResellerPlanStates(StatesGroup):
    name = State()
    price = State()
    days = State()
    gb = State()
    mode = State()


async def _actor(
    session: AsyncSession,
    db_user: BotUser,
    *,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> tuple[int | None, ResellerProfile | None]:
    from app.services.reseller_access import load_reseller_actor

    return await load_reseller_actor(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )


async def _staff_ctx(profile: ResellerProfile, session=None) -> dict:
    """Build staff dict via the same enrich path as the web panel."""
    from app.services.authz import enrich_reseller_actor
    from app.services.pg_credentials import enc_has_secret

    if session is not None:
        return await enrich_reseller_actor(
            session,
            {"role": "reseller", "username": profile.web_username or ""},
            profile,
        )
    # Fallback when no session (should be rare): map role features + readiness.
    from app.services.pg_access import map_pg_role_writes, resolve_reseller_pg_features, role_access_limits

    pg_permissions, pg_role = await resolve_reseller_pg_features(profile.pg_role_id)
    ready = bool(
        (profile.pg_admin_username or "").strip()
        and enc_has_secret(profile.pg_admin_password_enc)
    )
    return {
        "role": "reseller",
        "bot_user_id": profile.user_id,
        "pg_admin_username": profile.pg_admin_username,
        "pg_role_id": profile.pg_role_id,
        "pg_permissions": pg_permissions,
        "pg_access": role_access_limits(pg_role),
        "pg_writes": map_pg_role_writes(pg_role),
        "pg_client_ready": ready,
    }


def _plans_kb(plans: list[Plan]) -> InlineKeyboardMarkup:
    """Dynamic plan rows only — static «پلن جدید» lives on reply keyboard."""
    from app.bot.keyboards import reseller_plans_list_keyboard

    return reseller_plans_list_keyboard(plans)


def _plan_item_kb(plan: Plan) -> InlineKeyboardMarkup:
    """Plan actions only (no list-back chrome — use reply Back)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="خاموش" if plan.is_active else "روشن",
                    callback_data=f"res:plan:tog:{plan.id}",
                ),
                InlineKeyboardButton(text="🗑 حذف", callback_data=f"res:plan:del:{plan.id}"),
            ],
        ]
    )


async def _list_plans(session: AsyncSession, owner_id: int) -> list[Plan]:
    result = await session.execute(
        select(Plan)
        .where(Plan.owner_reseller_id == owner_id, Plan.is_trial.is_(False))
        .order_by(Plan.sort_order, Plan.id)
    )
    return list(result.scalars().all())


def _plan_text(plan: Plan) -> str:
    gb = f"{plan.data_limit_gb:g} گیگ" if plan.data_limit_gb is not None else "نامحدود"
    src = (
        f"تمپلیت #{plan.pg_template_id}"
        if plan.pg_template_id
        else (f"گروه {plan.pg_group_ids}" if plan.pg_group_ids else "—")
    )
    return (
        f"💎 <b>{plan.name}</b>\n"
        f"قیمت: {plan.price:,} تومان\n"
        f"مدت: {plan.duration_days} روز · حجم: {gb}\n"
        f"پاسارگارد: {src}\n"
        f"وضعیت: {'فعال' if plan.is_active else 'خاموش'}"
    )


@router.callback_query(F.data == "res:plans")
async def res_plans(callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    owner_id, profile = await _actor(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if not owner_id or not profile:
        await callback.answer("نماینده نیستید", show_alert=True)
        return
    if not has_bot_perm(profile, "plans"):
        await callback.answer("دسترسی پلن ندارید", show_alert=True)
        return
    await state.clear()
    await callback.answer()
    plans = await _list_plans(session, owner_id)
    text = "💎 <b>پلن‌های فروش فروشگاه شما</b>\n"
    if not plans:
        text += "هنوز پلنی نساخته‌اید. از کیبورد «پلن جدید» بسازید یا در وب‌پنل کامل‌تر تنظیم کنید."
    else:
        text += f"تعداد: {len(plans)}"
    if callback.message:
        await safe_edit_text(callback.message, text, reply_markup=_plans_kb(plans))


@router.callback_query(F.data == "res:plan:webhint")
async def res_plan_webhint(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    owner_id, profile = await _actor(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if not owner_id or not profile:
        await callback.answer("نماینده نیستید", show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "در وب‌پنل از منوی «پلن‌ها» می‌توانید پلن تست، پلن دلخواه و اتصال دقیق "
            "به گروه‌ها/تمپلیت‌های مجاز پاسارگارد را مدیریت کنید."
        )


@router.callback_query(F.data.startswith("res:plan:view:"))
async def res_plan_view(callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    owner_id, profile = await _actor(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if not owner_id or not profile:
        await callback.answer("نماینده نیستید", show_alert=True)
        return
    if not has_bot_perm(profile, "plans"):
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    plan = await session.get(Plan, int(callback.data.split(":")[-1]))
    if not plan or plan.owner_reseller_id != owner_id:
        await callback.answer("یافت نشد", show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await safe_edit_text(callback.message, _plan_text(plan), reply_markup=_plan_item_kb(plan))


@router.callback_query(F.data.startswith("res:plan:tog:"))
async def res_plan_toggle(callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    owner_id, profile = await _actor(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if not owner_id or not profile:
        await callback.answer("نماینده نیستید", show_alert=True)
        return
    if not has_bot_perm(profile, "plans"):
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    plan = await session.get(Plan, int(callback.data.split(":")[-1]))
    if not plan or plan.owner_reseller_id != owner_id or plan.is_trial:
        await callback.answer("یافت نشد", show_alert=True)
        return
    plan.is_active = not plan.is_active
    await session.commit()
    await callback.answer("ذخیره شد")
    if callback.message:
        await safe_edit_text(callback.message, _plan_text(plan), reply_markup=_plan_item_kb(plan))


@router.callback_query(F.data.startswith("res:plan:del:"))
async def res_plan_delete(callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    owner_id, profile = await _actor(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if not owner_id or not profile:
        await callback.answer("نماینده نیستید", show_alert=True)
        return
    if not has_bot_perm(profile, "plans"):
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    plan = await session.get(Plan, int(callback.data.split(":")[-1]))
    if not plan or plan.owner_reseller_id != owner_id or plan.is_trial:
        await callback.answer("یافت نشد", show_alert=True)
        return
    await session.delete(plan)
    await session.commit()
    await callback.answer("حذف شد")
    plans = await _list_plans(session, owner_id)
    if callback.message:
        await safe_edit_text(
            callback.message,
            "پلن حذف شد.\n💎 <b>پلن‌های فروش</b>",
            reply_markup=_plans_kb(plans),
        )


@router.callback_query(F.data == "res:plan:add")
async def res_plan_add(callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    owner_id, profile = await _actor(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if not owner_id or not profile:
        await callback.answer("نماینده نیستید", show_alert=True)
        return
    if not has_bot_perm(profile, "plans"):
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    await callback.answer()
    await state.set_state(ResellerPlanStates.name)
    if callback.message:
        await callback.message.answer("نام پلن را بفرستید:", reply_markup=kb.cancel_reply())


@router.message(ResellerPlanStates.name)
async def res_plan_name(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    owner_id, profile = await _actor(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if not owner_id or not profile:
        await state.clear()
        return
    if kb.is_cancel_text(message.text):
        await state.clear()
        await message.answer(
            "لغو شد.",
            reply_markup=kb.reseller_plans_reply_keyboard(),
        )
        return
    await state.update_data(name=(message.text or "").strip(), owner_id=owner_id)
    await state.set_state(ResellerPlanStates.price)
    await message.answer("قیمت به تومان را بفرستید:", reply_markup=kb.cancel_reply())


@router.message(ResellerPlanStates.price)
async def res_plan_price(message: Message, state: FSMContext, db_user: BotUser):
    if kb.is_cancel_text(message.text):
        await state.clear()
        await message.answer(
            "لغو شد.",
            reply_markup=kb.reseller_plans_reply_keyboard(),
        )
        return
    try:
        price = int((message.text or "").replace(",", "").replace("٬", ""))
    except ValueError:
        await message.answer("یک عدد معتبر بفرستید.", reply_markup=kb.cancel_reply())
        return
    await state.update_data(price=price)
    await state.set_state(ResellerPlanStates.days)
    await message.answer("مدت اعتبار به روز را بفرستید:", reply_markup=kb.cancel_reply())


@router.message(ResellerPlanStates.days)
async def res_plan_days(message: Message, state: FSMContext):
    if kb.is_cancel_text(message.text):
        await state.clear()
        await message.answer(
            "لغو شد.",
            reply_markup=kb.reseller_plans_reply_keyboard(),
        )
        return
    try:
        days = max(1, int(message.text or "30"))
    except ValueError:
        await message.answer("یک عدد معتبر بفرستید.", reply_markup=kb.cancel_reply())
        return
    await state.update_data(days=days)
    await state.set_state(ResellerPlanStates.gb)
    await message.answer(
        "حجم به گیگ را بفرستید:\n<code>0</code> = نامحدود",
        reply_markup=kb.cancel_reply(),
    )


@router.message(ResellerPlanStates.gb)
async def res_plan_gb(message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    if kb.is_cancel_text(message.text):
        await state.clear()
        await message.answer(
            "لغو شد.",
            reply_markup=kb.reseller_plans_reply_keyboard(),
        )
        return
    raw = (message.text or "").strip().replace(",", ".")
    try:
        gb_val = float(raw)
    except ValueError:
        await message.answer("عدد معتبر بفرستید")
        return
    gb = None if gb_val <= 0 else gb_val
    await state.update_data(gb=gb)
    owner_id, profile = await _actor(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if not owner_id or not profile:
        await state.clear()
        await message.answer("نماینده نیستید.")
        return
    staff = await _staff_ctx(profile, session=session)
    templates, groups, pg_error = await load_pg_plan_options(staff, session=session)
    await state.update_data(templates=templates, groups=groups, owner_id=owner_id)
    rows: list[list[InlineKeyboardButton]] = []
    if groups:
        rows.append(
            [InlineKeyboardButton(text="📦 انتخاب گروه پاسارگارد", callback_data="res:plan:mode:groups")]
        )
    if templates:
        rows.append(
            [InlineKeyboardButton(text="🧩 انتخاب تمپلیت", callback_data="res:plan:mode:tpl")]
        )
    rows.append([InlineKeyboardButton(text="❌ انصراف", callback_data="res:plans")])
    await state.set_state(ResellerPlanStates.mode)
    note = f"\n⚠️ پاسارگارد: {pg_error}" if pg_error else ""
    if not groups and not templates:
        await state.clear()
        await message.answer(
            "گروه یا تمپلیت مجازی در نقش پاسارگارد شما نیست.\n"
            "از وب‌پنل → پاسارگارد دسترسی‌ها را بررسی کنید یا از ادمین بخواهید نقش را باز کند."
            + note
        )
        return
    await message.answer(
        "منبع ساخت سرویس در پاسارگارد را انتخاب کنید:" + note,
        reply_markup=kb.reseller_plans_reply_keyboard(),
    )
    await message.answer(
        "یکی از گزینه‌های زیر را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.message(ResellerPlanStates.mode)
async def res_plan_mode_cancel(message: Message, state: FSMContext):
    if kb.is_cancel_text(message.text):
        await state.clear()
        await message.answer("لغو شد.", reply_markup=kb.reseller_plans_reply_keyboard())
        return
    await message.answer(
        "منبع را از دکمه‌های زیر پیام انتخاب کنید، یا انصراف بزنید.",
        reply_markup=kb.reseller_plans_reply_keyboard(),
    )


@router.callback_query(F.data == "res:plan:mode:groups", ResellerPlanStates.mode)
async def res_plan_pick_groups(callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser):
    data = await state.get_data()
    groups = data.get("groups") or []
    if not groups:
        await callback.answer("گروهی نیست", show_alert=True)
        return
    await callback.answer()
    # Multi-select via toggling; start with empty then save
    await state.update_data(selected_groups=[])
    rows = []
    for g in groups[:30]:
        gid = int(g.get("id") or 0)
        name = g.get("name") or gid
        rows.append(
            [InlineKeyboardButton(text=f"▫️ {name}", callback_data=f"res:plan:g:{gid}")]
        )
    rows.append([InlineKeyboardButton(text="✅ ذخیره پلن", callback_data="res:plan:save:groups")])
    rows.append([InlineKeyboardButton(text="⬅️ انصراف", callback_data="res:plans")])
    if callback.message:
        await callback.message.edit_text(
            "گروه‌ها را انتخاب کنید (چندتایی)، بعد ذخیره:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )


@router.callback_query(F.data.startswith("res:plan:g:"), ResellerPlanStates.mode)
async def res_plan_toggle_group(callback: CallbackQuery, state: FSMContext):
    gid = int(callback.data.split(":")[-1])
    data = await state.get_data()
    selected = list(data.get("selected_groups") or [])
    if gid in selected:
        selected.remove(gid)
    else:
        selected.append(gid)
    await state.update_data(selected_groups=selected)
    groups = data.get("groups") or []
    rows = []
    for g in groups[:30]:
        id_ = int(g.get("id") or 0)
        name = g.get("name") or id_
        mark = "✅" if id_ in selected else "▫️"
        rows.append(
            [InlineKeyboardButton(text=f"{mark} {name}", callback_data=f"res:plan:g:{id_}")]
        )
    rows.append([InlineKeyboardButton(text="✅ ذخیره پلن", callback_data="res:plan:save:groups")])
    rows.append([InlineKeyboardButton(text="⬅️ انصراف", callback_data="res:plans")])
    await callback.answer()
    if callback.message:
        await callback.message.edit_reply_markup(
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
        )


@router.callback_query(F.data == "res:plan:save:groups", ResellerPlanStates.mode)
async def res_plan_save_groups(callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    owner_id, profile = await _actor(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if not owner_id or not profile:
        await callback.answer("نماینده نیستید", show_alert=True)
        return
    if not has_bot_perm(profile, "plans"):
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    data = await state.get_data()
    selected = [int(x) for x in (data.get("selected_groups") or [])]
    staff = await _staff_ctx(profile, session=session)
    if not selected or not groups_allowed_for_staff(staff, selected):
        await callback.answer("حداقل یک گروه مجاز انتخاب کنید", show_alert=True)
        return
    plan = Plan(
        name=str(data.get("name") or "پلن").strip(),
        price=int(data.get("price") or 0),
        duration_days=int(data.get("days") or 30),
        data_limit_gb=data.get("gb"),
        pg_group_ids=",".join(str(i) for i in selected),
        pg_template_id=None,
        owner_reseller_id=owner_id,
        is_active=True,
    )
    session.add(plan)
    await session.commit()
    await state.clear()
    await callback.answer("ذخیره شد")
    plans = await _list_plans(session, owner_id)
    if callback.message:
        await safe_edit_text(
            callback.message,
            f"✅ پلن «{plan.name}» ساخته شد.\n" + _plan_text(plan),
            reply_markup=_plans_kb(plans),
        )


@router.callback_query(F.data == "res:plan:mode:tpl", ResellerPlanStates.mode)
async def res_plan_pick_tpl(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    templates = data.get("templates") or []
    if not templates:
        await callback.answer("تمپلیتی نیست", show_alert=True)
        return
    await callback.answer()
    rows = []
    for t in templates[:30]:
        tid = int(t.get("id") or 0)
        name = t.get("name") or tid
        rows.append(
            [InlineKeyboardButton(text=f"#{tid} {name}", callback_data=f"res:plan:t:{tid}")]
        )
    rows.append([InlineKeyboardButton(text="⬅️ انصراف", callback_data="res:plans")])
    if callback.message:
        await callback.message.edit_text(
            "تمپلیت را انتخاب کنید:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )


@router.callback_query(F.data.startswith("res:plan:t:"), ResellerPlanStates.mode)
async def res_plan_save_tpl(callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    owner_id, profile = await _actor(session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if not owner_id or not profile:
        await callback.answer("نماینده نیستید", show_alert=True)
        return
    if not has_bot_perm(profile, "plans"):
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    tid = int(callback.data.split(":")[-1])
    staff = await _staff_ctx(profile, session=session)
    if not template_allowed_for_staff(staff, tid):
        await callback.answer("به این تمپلیت دسترسی ندارید", show_alert=True)
        return
    data = await state.get_data()
    plan = Plan(
        name=str(data.get("name") or "پلن").strip(),
        price=int(data.get("price") or 0),
        duration_days=int(data.get("days") or 30),
        data_limit_gb=data.get("gb"),
        pg_template_id=tid,
        pg_group_ids=None,
        owner_reseller_id=owner_id,
        is_active=True,
    )
    session.add(plan)
    await session.commit()
    await state.clear()
    await callback.answer("ذخیره شد")
    plans = await _list_plans(session, owner_id)
    if callback.message:
        await safe_edit_text(
            callback.message,
            f"✅ پلن «{plan.name}» ساخته شد.\n" + _plan_text(plan),
            reply_markup=_plans_kb(plans),
        )
