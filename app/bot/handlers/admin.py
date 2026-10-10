from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyKeyboardMarkup
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.auth import is_platform_admin as _is_admin
from app.bot.auth import require_bot_owner_handler, require_platform_rep_mgmt
from app.config import get_settings
from app.db.models import BotUser, Order, OrderStatus, Payment, PaymentStatus, Plan, Role, Ticket, UserService
from app.services.home_overview import admin_customer_counts, bot_dashboard_summary
from app.services.formatting import (
    format_system_stats,
    format_toman,
    order_status_fa,
)
from app.services.orders import (
    approve_payment,
    fulfill_paid_order,
    reject_payment,
)
from app.services.redact import user_safe_error
from app.services.pasarguard import get_pg
from app.services.tickets import get_ticket, list_open_tickets, reply_ticket
from app.services.updates import local_version
from app.services.users import get_all_settings, get_setting, on, set_setting
from app.bot.tg_utils import parse_bot_float, parse_bot_int, safe_edit_text

async def _admin_hub_kb(session: AsyncSession, db_user: BotUser) -> ReplyKeyboardMarkup:
    from app.bot.menu_nav import admin_hub_reply_keyboard
    from app.bot.nav_chrome import lasting_staff_reply

    classic = await admin_hub_reply_keyboard(session, db_user)
    return await lasting_staff_reply(session, db_user, classic=classic)

async def _staff_reply(
    session: AsyncSession,
    db_user: BotUser,
    classic: ReplyKeyboardMarkup,
) -> ReplyKeyboardMarkup:
    """Return lasting staff ReplyKeyboard (stable main under Option B)."""
    from app.bot.nav_chrome import lasting_staff_reply

    return await lasting_staff_reply(session, db_user, classic=classic)

async def _answer_users_nav(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    text: str,
    state: FSMContext | None = None,
    *,
    reopen: bool = True,
    clear_state: bool = False,
) -> None:
    """After cancel/finish in users flows: heal main KB + restore users inline hub."""
    from app.bot.nav_chrome import answer_staff_nav

    reopen_fn = None
    if reopen:
        from app.bot.handlers.reply_nav import open_admin_users_hub

        reopen_fn = open_admin_users_hub
    await answer_staff_nav(
        message,
        session,
        db_user,
        text=text,
        classic=kb.admin_users_reply_keyboard(),
        state=state,
        reopen_panel=reopen_fn,
        clear_state=clear_state,
    )

def _plan_line(p: Plan) -> str:
    flag = "✅" if p.is_active else "⏸"
    if p.pg_template_id:
        link = f"تمپلیت #{p.pg_template_id}"
    elif p.pg_group_ids:
        link = f"گروه {p.pg_group_ids}"
    else:
        link = "⚠️ بدون اتصال پاسارگارد"
    return (
        f"{flag} <b>#{p.id} {html.escape(p.name)}</b>\n"
        f"💰 {format_toman(p.price, get_settings().currency)} · {link}"
    )

def _plan_needs_link(p: Plan) -> bool:
    return not p.pg_template_id and not (p.pg_group_ids or "").strip()

async def _plans_flow_reply_kb(
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
) -> ReplyKeyboardMarkup:
    data = await state.get_data()
    aud = data.get("_adm_plans_aud")
    classic = kb.admin_plans_reply_keyboard(
        audience=aud if aud in {"users", "resellers"} else None
    )
    return await _staff_reply(session, db_user, classic)

async def _render_plans_list(callback: CallbackQuery, session: AsyncSession) -> None:
    ui = await get_all_settings(session)
    result = await session.execute(select(Plan).order_by(Plan.sort_order, Plan.id))
    plans = list(result.scalars().all())
    fixed = [p for p in plans if not p.is_trial]
    if not fixed:
        text = "📦 <b>پلن ثابت</b>\n\nهنوز پلن ثابتی ثبت نشده است."
    else:
        from app.bot.handlers.admin import _plan_line

        cards = "\n\n".join(_plan_line(p) for p in fixed[:20])
        text = f"📦 <b>پلن‌های ثابت</b>\n\n{cards}"
    if callback.message:
        await callback.message.edit_text(
            text,
            reply_markup=kb.admin_plans_list_keyboard(
                plans,
                ui,
                back_callback="adm:plans:aud:users",
                kind="fixed",
            ),
        )

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
            InlineKeyboardButton(text="⬅️ پلن دلخواه", callback_data="adm:plans:aud:users"),
            InlineKeyboardButton(text="پلن‌ها", callback_data="adm:plans"),
        ]
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)

async def _plan_detail_text(p: Plan, *, category_name: str | None = None) -> str:
    gb = f"{p.data_limit_gb:g} گیگ" if p.data_limit_gb is not None else "نامحدود"
    if p.pg_template_id:
        link = f"تمپلیت #{p.pg_template_id}"
    elif p.pg_group_ids:
        link = f"گروه‌ها: {p.pg_group_ids}"
    else:
        link = "⚠️ هنوز به تمپلیت/گروه وصل نشده — خرید تحویل نمی‌شود"
    cat = html.escape(category_name) if category_name else "—"
    return (
        f"💎 <b>پلن #{p.id}</b> — {html.escape(p.name)}\n\n"
        f"قیمت: {format_toman(p.price, get_settings().currency)}\n"
        f"مدت: {p.duration_days} روز\n"
        f"حجم: {gb}\n"
        f"برچسب دسته: {cat}\n"
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
            kb._ikb(
                "⬅️ پلن‌های کاربران",
                callback_data="adm:plans:aud:users",
                style=kb._style(ui, "back"),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)

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
    text = "🎨 <b>رنگ دکمه این پلن در ربات</b>\n\n«ارث از نوع پلن» همان رنگ بخش «نوع پلن» در تنظیمات رنگبندی است."
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

router = Router(name="admin")

class AdminStates(StatesGroup):
    add_plan_name = State()
    add_plan_price = State()
    add_plan_days = State()
    add_plan_gb = State()
    add_plan_link = State()  # waiting for mode after basics
    add_plan_color = State()  # Telegram button color before category/save
    add_plan_category = State()  # optional PlanCategory before save
    plan_edit_field = State()
    make_reseller = State()
    ticket_reply = State()
    user_search = State()
    user_wallet_credit = State()
    user_message = State()
    revoke_reseller_reason = State()
    block_user_reason = State()
    delete_user_reason = State()
    broadcast_text = State()
    broadcast_audience = State()
    svc_adjust_days_input = State()
    svc_adjust_gb_input = State()
    reseller_cap_days_input = State()
    reseller_cap_gb_input = State()

@router.callback_query(F.data == "adm:home")
@require_bot_owner_handler
async def adm_home(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await callback.answer()
    text = (
        f"🛠 <b>پنل ادمین</b>\n"
        f"<code>v{local_version()}</code>\n\n"
        "از منوی زیر بخش موردنظر را انتخاب کنید."
    )
    if callback.message:
        # ReplyKeyboard cannot be attached via edit_text — clear inline then send reply KB
        from app.bot.tg_utils import safe_edit_text

        await safe_edit_text(callback.message, text, reply_markup=None)
        await callback.message.answer(
            "پنل ادمین:",
            reply_markup=await _admin_hub_kb(session, db_user),
        )

@router.callback_query(F.data == "adm:dash")
@require_bot_owner_handler
async def adm_dash(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await callback.answer()
    summary = await bot_dashboard_summary(session)
    users_count, orders_count = summary["users"], summary["orders"]
    pending_pay, pending_orders = summary["pending"], summary["pending_orders"]
    services = summary["services"]
    text = (
        "📊 <b>داشبورد</b>\n"
        "━━━━━━━━━━━━\n"
        f"👥 کاربران: <b>{users_count}</b>\n"
        f"🛒 سفارش‌ها: <b>{orders_count}</b>\n"
        f"⏳ منتظر تأیید: <b>{pending_orders}</b>\n"
        f"🧾 رسید معلق: <b>{pending_pay}</b>\n"
        f"📦 سرویس‌ها: <b>{services}</b>\n"
        f"🔢 نسخه: <code>v{local_version()}</code>"
    )
    if callback.message:
        from app.bot.nav_inline import with_inline_back
        from app.services.users import get_all_settings

        ui = await get_all_settings(session)
        markup = (
            with_inline_back(None, ui, "nv:adm:ops")
        )
        await callback.message.edit_text(text, reply_markup=markup)

def _order_actions(
    order: Order, payment: Payment | None, ui: dict | None = None
) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    # Shop-tenant orders are never actionable by platform admin
    if order.reseller_id:
        return rows
    can_decide = False
    if payment and payment.status == PaymentStatus.PENDING.value:
        can_decide = True
    elif order.status in {OrderStatus.PAID.value, OrderStatus.AWAITING_APPROVAL.value}:
        can_decide = True
    if can_decide and order.status not in {OrderStatus.DELIVERED.value, OrderStatus.REJECTED.value}:
        rows.append(
            [
                kb._ikb(
                    "✅ تأیید",
                    callback_data=f"ordrev:ok:{order.id}",
                    style=kb._style(ui, "confirm", fallback="success"),
                ),
                kb._ikb(
                    "❌ رد",
                    callback_data=f"ordrev:no:{order.id}",
                    style=kb._style(ui, "reject", fallback="danger"),
                ),
            ]
        )
    return rows

@router.callback_query(F.data.startswith("adm:reports"))
@require_bot_owner_handler
async def adm_reports(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    raw = (callback.data or "adm:reports:week").split(":")
    period = raw[-1] if len(raw) >= 3 else "week"
    if period not in {"day", "week", "month"}:
        period = "week"
    await callback.answer()
    from app.config import get_settings
    from app.services.finance_reports import (
        PERIOD_LABELS_FA,
        build_finance_report,
        format_finance_report_telegram,
    )

    report = await build_finance_report(session, reseller_id=None, period=period)
    text = format_finance_report_telegram(
        report, currency=get_settings().currency
    )
    text += "\n\n<i>جزئیات وب: /finance?tab=reports</i>"
    rows = []
    period_row = []
    for key, fa in PERIOD_LABELS_FA.items():
        mark = "✓ " if key == period else ""
        period_row.append(
            InlineKeyboardButton(
                text=f"{mark}{fa}",
                callback_data=f"adm:reports:{key}",
            )
        )
    rows.append(period_row)
    if callback.message:
        from app.bot.nav_inline import with_inline_back

        ui = await get_all_settings(session)
        markup = InlineKeyboardMarkup(inline_keyboard=rows)
        markup = with_inline_back(markup, ui, "nv:adm:ops")
        await safe_edit_text(
            callback.message,
            text,
            reply_markup=markup,
        )

@router.callback_query(F.data == "adm:orders")
@require_bot_owner_handler
async def adm_orders(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await callback.answer()
    from app.services.ux20 import list_stuck_paid_orders

    stuck = await list_stuck_paid_orders(session, reseller_id=None, limit=12)
    stuck_ids = {int(o.id) for o in stuck}
    result = await session.execute(
        select(Order)
        .where(Order.reseller_id.is_(None))
        .order_by(Order.id.desc())
        .limit(12)
    )
    recent = list(result.scalars().all())
    merged: list[Order] = []
    seen: set[int] = set()
    for o in list(stuck) + recent:
        oid = int(o.id)
        if oid in seen:
            continue
        seen.add(oid)
        merged.append(o)
        if len(merged) >= 12:
            break
    from app.bot.nav_inline import with_inline_back

    ui = await get_all_settings(session)
    if not merged:
        if callback.message:
            markup = (
                with_inline_back(None, ui, "nv:adm:ops")
            )
            await callback.message.edit_text("سفارشی نیست.", reply_markup=markup)
        return
    rows = []
    for o in merged:
        prefix = "⚠ " if int(o.id) in stuck_ids else ""
        label = f"{prefix}#{o.id} · {order_status_fa(o.status)} · {format_toman(o.amount, get_settings().currency)}"
        rows.append([InlineKeyboardButton(text=label[:64], callback_data=f"adm:order:{o.id}")])
    if callback.message:
        markup = InlineKeyboardMarkup(inline_keyboard=rows)
        markup = with_inline_back(markup, ui, "nv:adm:ops")
        await callback.message.edit_text(
            "🛒 <b>سفارش‌ها</b>\nیکی را برای جزئیات و تأیید/رد انتخاب کنید:\n"
            "<i>⚠ = پرداخت‌شده بدون سرویس (نیاز به تلاش مجدد تحویل)</i>",
            reply_markup=markup,
        )

@router.callback_query(F.data.startswith("adm:order:"))
@require_bot_owner_handler
async def adm_order_view(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    order_id = int(callback.data.split(":")[-1])
    order = await session.get(Order, order_id)
    if not order:
        await callback.answer("یافت نشد", show_alert=True)
        return
    if order.reseller_id:
        await callback.answer(
            "این سفارش مربوط به نماینده است — فقط در ربات/پنل همان فروشگاه قابل مشاهده است",
            show_alert=True,
        )
        return
    await callback.answer()
    user = await session.get(BotUser, order.user_id)
    plan = await session.get(Plan, order.plan_id) if order.plan_id else None
    pay = (
        await session.execute(
            select(Payment).where(Payment.order_id == order_id).order_by(Payment.id.desc()).limit(1)
        )
    ).scalar_one_or_none()
    who = html.escape((user.full_name or user.username or str(order.user_id)) if user else str(order.user_id))
    plan_name = html.escape(plan.name) if plan and plan.name else (order.plan_id or "—")
    text = (
        f"🛒 <b>سفارش #{order.id}</b>\n\n"
        f"وضعیت: <b>{order_status_fa(order.status)}</b>\n"
        f"کاربر: {who}\n"
        f"پلن: {plan_name}\n"
        f"مبلغ: {format_toman(order.amount, get_settings().currency)}\n"
        f"روش: {html.escape(order.payment_method or '—')}\n"
    )
    if pay:
        text += f"پرداخت: #{pay.id} ({pay.status})\n"
    ui = await get_all_settings(session)
    rows = _order_actions(order, pay, ui)
    rows.append([InlineKeyboardButton(text="⬅️ لیست سفارش‌ها", callback_data="adm:orders")])
    if callback.message:
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

async def _approve_order_bot(
    session: AsyncSession, order: Order, bot, *, reviewer_tg: int = 0
) -> str:
    pay = (
        await session.execute(
            select(Payment).where(Payment.order_id == order.id).order_by(Payment.id.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if order.status == OrderStatus.DELIVERED.value:
        return "قبلاً تحویل شده"
    delivered = None
    if pay and pay.status == PaymentStatus.PENDING.value:
        delivered = await approve_payment(session, pay, reviewer_tg=reviewer_tg, bot=bot)
        try:
            from app.services.delivery import send_delivery_to_user

            user = await session.get(BotUser, pay.user_id)
            if user:
                await send_delivery_to_user(
                    bot, user.telegram_id, session, pay, delivered or order
                )
        except Exception as send_exc:
            try:
                from app.services.ux20 import note_delivery_send_failure

                await note_delivery_send_failure(
                    session,
                    order=delivered or order,
                    payment=pay,
                    error=str(send_exc),
                )
            except Exception:
                pass
        msg = "سفارش تأیید و تحویل شد"
    elif order.status == OrderStatus.PAID.value or (
        pay and pay.status == PaymentStatus.APPROVED.value and order.status != OrderStatus.DELIVERED.value
    ):
        delivered = await fulfill_paid_order(session, order)
        if pay:
            try:
                from app.services.delivery import send_delivery_to_user

                user = await session.get(BotUser, pay.user_id)
                if user:
                    await send_delivery_to_user(
                        bot, user.telegram_id, session, pay, delivered
                    )
            except Exception as send_exc:
                try:
                    from app.services.ux20 import note_delivery_send_failure

                    await note_delivery_send_failure(
                        session,
                        order=delivered or order,
                        payment=pay,
                        error=str(send_exc),
                    )
                except Exception:
                    pass
        msg = "سفارش تحویل شد"
    else:
        raise ValueError("این سفارش هنوز قابل تأیید نیست (رسید لازم است)")

    try:
        from app.services.notifications import notify_new_subscription

        final = delivered or order
        user = await session.get(BotUser, final.user_id)
        plan = await session.get(Plan, final.plan_id) if final.plan_id else None
        await notify_new_subscription(
            bot,
            session,
            order=final,
            user_tg_id=user.telegram_id if user else None,
            user_name=(user.full_name or user.username) if user else None,
            plan_name=plan.name if plan else None,
            needs_approval=False,
        )
    except Exception:
        pass
    return msg

@router.callback_query(F.data.startswith("ordrev:ok:"))
@require_bot_owner_handler
async def order_approve_cb(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("فقط ادمین", show_alert=True)
        return
    order_id = int(callback.data.split(":")[-1])
    order = await session.get(Order, order_id)
    if not order:
        await callback.answer("یافت نشد", show_alert=True)
        return
    if order.reseller_id:
        await callback.answer(
            "این سفارش مربوط به نماینده است — فقط در ربات/پنل همان فروشگاه قابل تأیید است",
            show_alert=True,
        )
        return
    try:
        msg = await _approve_order_bot(
            session, order, callback.bot, reviewer_tg=int(db_user.telegram_id)
        )
        await callback.answer(msg, show_alert=True)
    except Exception as e:
        await callback.answer(user_safe_error(e), show_alert=True)
        return
    await session.refresh(order)
    text = f"🛒 سفارش #{order.id}\nوضعیت: <b>{order_status_fa(order.status)}</b>\n✅ انجام شد"
    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=kb.order_review(order.id) if order.status != OrderStatus.DELIVERED.value else InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ سفارش‌ها", callback_data="adm:orders")]]))
        except Exception:
            pass

@router.callback_query(F.data.startswith("ordrev:no:"))
@require_bot_owner_handler
async def order_reject_cb(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("فقط ادمین", show_alert=True)
        return
    order_id = int(callback.data.split(":")[-1])
    order = await session.get(Order, order_id)
    if not order:
        await callback.answer("یافت نشد", show_alert=True)
        return
    if order.reseller_id:
        await callback.answer(
            "این سفارش مربوط به نماینده است — فقط در ربات/پنل همان فروشگاه قابل رد است",
            show_alert=True,
        )
        return
    pay = (
        await session.execute(
            select(Payment).where(Payment.order_id == order_id).order_by(Payment.id.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if pay and pay.status == PaymentStatus.PENDING.value:
        await reject_payment(
            session, pay, reviewer_tg=db_user.telegram_id, note="bot reject", bot=callback.bot
        )
        user = await session.get(BotUser, pay.user_id)
        if user:
            try:
                from app.services.formatting import format_message

                await callback.bot.send_message(
                    user.telegram_id,
                    format_message("❌ سفارش رد شد", f"سفارش #{order_id} رد شد."),
                    parse_mode="HTML",
                )
            except Exception:
                pass
    else:
        order.status = OrderStatus.REJECTED.value
        await session.commit()
    await callback.answer("رد شد", show_alert=True)
    if callback.message:
        try:
            await callback.message.edit_text(
                f"🛒 سفارش #{order_id}\nوضعیت: <b>ردشده</b>",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="⬅️ سفارش‌ها", callback_data="adm:orders")]]
                ),
            )
        except Exception:
            pass

@router.callback_query(F.data == "adm:payments")
@require_bot_owner_handler
async def adm_payments(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await callback.answer()
    from sqlalchemy import or_

    result = await session.execute(
        select(Payment)
        .outerjoin(Order, Order.id == Payment.order_id)
        .where(
            Payment.status == PaymentStatus.PENDING.value,
            Payment.receipt_file_id.is_not(None),
            or_(
                Payment.is_wallet_topup.is_(True),
                Order.reseller_id.is_(None),
            ),
        )
        .order_by(Payment.id.desc())
        .limit(15)
    )
    payments = list(result.scalars().all())
    from app.bot.nav_inline import with_inline_back

    ui = await get_all_settings(session)
    back = with_inline_back(None, ui, "nv:adm:ops")
    if not payments:
        if callback.message:
            await callback.message.edit_text("رسید معلقی نیست.", reply_markup=back)
        return
    for p in payments:
        caption = f"پرداخت #{p.id} — {format_toman(p.amount, get_settings().currency)}"
        try:
            await callback.bot.send_photo(
                db_user.telegram_id,
                photo=p.receipt_file_id,
                caption=caption,
                reply_markup=kb.payment_review(p.id),
            )
        except Exception:
            await callback.bot.send_message(
                db_user.telegram_id, caption, reply_markup=kb.payment_review(p.id)
            )
    if callback.message:
        await callback.message.edit_text("رسیدها ارسال شد.", reply_markup=back)

@router.callback_query(F.data.startswith("adm:plan:view:"))
@require_bot_owner_handler
async def adm_plan_view(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    plan = await session.get(Plan, int(callback.data.split(":")[-1]))
    if not plan:
        await callback.answer("یافت نشد", show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            await _plan_detail_text_for(session, plan),
            reply_markup=await _plan_detail_markup(session, plan),
        )

@router.callback_query(F.data.startswith("adm:plan:edit:"))
@require_bot_owner_handler
async def adm_plan_edit_ask(
    callback: CallbackQuery, state: FSMContext, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) < 5:
        await callback.answer("نامعتبر", show_alert=True)
        return
    field, pid_raw = parts[3], parts[4]
    if field not in {"name", "price", "days", "gb", "desc", "sort", "prefix", "suffix"}:
        await callback.answer("نامعتبر", show_alert=True)
        return
    prompts = {
        "name": "نام جدید پلن:",
        "price": "قیمت (تومان):",
        "days": "مدت (روز):",
        "gb": "حجم گیگ (۰ = نامحدود):",
        "desc": "توضیح (خالی = حذف):",
        "sort": "ترتیب نمایش (عدد؛ کمتر = بالاتر):",
        "prefix": "پیشوند نام کاربری پاسارگارد (خالی = پیش‌فرض):",
        "suffix": "پسوند نام کاربری پاسارگارد (خالی = حذف):",
    }
    await state.set_state(AdminStates.plan_edit_field)
    await state.update_data(
        user_plan_edit_id=int(pid_raw),
        user_plan_edit_field=field,
        _adm_plans_aud="users",
        _adm_plans_kind="fixed",
    )
    await callback.answer()
    if callback.message:
        await callback.message.answer(prompts[field], reply_markup=kb.cancel_reply())

@router.message(AdminStates.plan_edit_field)
@require_bot_owner_handler
async def adm_plan_edit_save(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await state.clear()
        return
    if kb.is_cancel_text(message.text):
        await state.clear()
        await message.answer("لغو شد.", reply_markup=await _plans_flow_reply_kb(session, db_user, state))
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
                await message.answer("نام خالی نیست.")
                return
            plan.name = text[:128]
        elif field == "price":
            plan.price = max(0, parse_bot_int(text))
        elif field == "days":
            plan.duration_days = max(1, parse_bot_int(text))
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
        await message.answer("عدد معتبر بفرستید.", reply_markup=kb.cancel_reply())
        return
    await session.commit()
    await state.set_state(None)
    await message.answer("ذخیره شد ✅", reply_markup=await _plans_flow_reply_kb(session, db_user, state))
    await message.answer(
        await _plan_detail_text_for(session, plan),
        reply_markup=await _plan_detail_markup(session, plan),
    )

@router.callback_query(F.data == "adm:plan:add")
@require_bot_owner_handler
async def adm_plan_add(callback: CallbackQuery, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await callback.answer()
    await state.set_state(AdminStates.add_plan_name)
    await state.update_data(_adm_plans_aud="users", _adm_plans_kind="fixed")
    if callback.message:
        await callback.message.answer("نام پلن را بفرستید:", reply_markup=kb.cancel_reply())

@router.message(AdminStates.add_plan_name)
@require_bot_owner_handler
async def plan_name(message: Message, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await state.clear()
        await message.answer("ادمین نیستید")
        return
    if kb.is_cancel_text(message.text):
        await state.clear()
        await message.answer("لغو شد.", reply_markup=await _plans_flow_reply_kb(session, db_user, state))
        return
    await state.update_data(name=(message.text or "").strip())
    await state.set_state(AdminStates.add_plan_price)
    await message.answer("قیمت به تومان را بفرستید:", reply_markup=kb.cancel_reply())

@router.message(AdminStates.add_plan_price)
@require_bot_owner_handler
async def plan_price(message: Message, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await state.clear()
        await message.answer("ادمین نیستید")
        return
    if kb.is_cancel_text(message.text):
        await state.clear()
        await message.answer("لغو شد.", reply_markup=await _plans_flow_reply_kb(session, db_user, state))
        return
    try:
        price = max(0, parse_bot_int(message.text))
    except ValueError:
        await message.answer("یک عدد معتبر بفرستید.", reply_markup=kb.cancel_reply())
        return
    await state.update_data(price=price)
    await state.set_state(AdminStates.add_plan_days)
    await message.answer("مدت اعتبار به روز را بفرستید:", reply_markup=kb.cancel_reply())

@router.message(AdminStates.add_plan_days)
@require_bot_owner_handler
async def plan_days(message: Message, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await state.clear()
        await message.answer("ادمین نیستید")
        return
    if kb.is_cancel_text(message.text):
        await state.clear()
        await message.answer("لغو شد.", reply_markup=await _plans_flow_reply_kb(session, db_user, state))
        return
    try:
        days = max(1, parse_bot_int(message.text, default=30))
    except ValueError:
        await message.answer("یک عدد معتبر بفرستید.", reply_markup=kb.cancel_reply())
        return
    await state.update_data(days=days)
    await state.set_state(AdminStates.add_plan_gb)
    await message.answer(
        "حجم به گیگ را بفرستید:\n<code>0</code> = نامحدود",
        reply_markup=kb.cancel_reply(),
    )

@router.message(AdminStates.add_plan_gb)
@require_bot_owner_handler
async def plan_gb(message: Message, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await state.clear()
        await message.answer("ادمین نیستید")
        return
    if kb.is_cancel_text(message.text):
        await state.clear()
        await message.answer("لغو شد.", reply_markup=await _plans_flow_reply_kb(session, db_user, state))
        return
    try:
        gb = parse_bot_float(message.text)
    except ValueError:
        await message.answer("یک عدد معتبر بفرستید.", reply_markup=kb.cancel_reply())
        return
    await state.update_data(gb=None if gb <= 0 else gb)
    await state.set_state(AdminStates.add_plan_link)
    await message.answer(
        "اتصال پاسارگارد را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📋 از تمپلیت", callback_data="adm:plan:new:mode:tpl")],
                [InlineKeyboardButton(text="📁 با گروه (سفارشی)", callback_data="adm:plan:new:mode:grp")],
                [InlineKeyboardButton(text="❌ انصراف", callback_data="adm:plans")],
            ]
        ),
    )

@router.message(AdminStates.add_plan_link)
@require_bot_owner_handler
async def plan_link_cancel(message: Message, state: FSMContext, db_user: BotUser):
    """Allow reply-keyboard انصراف while waiting for inline PG mode pick."""
    if not _is_admin(db_user):
        await state.clear()
        await message.answer("ادمین نیستید")
        return
    if kb.is_cancel_text(message.text):
        await state.clear()
        await message.answer("لغو شد.", reply_markup=await _plans_flow_reply_kb(session, db_user, state))
        return
    await message.answer(
        "اتصال را از دکمه‌های زیر پیام انتخاب کنید، یا انصراف بزنید.",
        reply_markup=await _plans_flow_reply_kb(session, db_user, state),
    )

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
        duration_days=max(1, days),
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

@router.callback_query(F.data == "adm:plan:new:mode:tpl")
@require_bot_owner_handler
async def adm_plan_new_tpl(callback: CallbackQuery, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    if await state.get_state() != AdminStates.add_plan_link.state:
        await callback.answer("ابتدا ساخت پلن را شروع کنید", show_alert=True)
        return
    await callback.answer()
    await state.update_data(new_plan=1)
    await _show_template_picker(callback, plan_id=0)

@router.callback_query(F.data == "adm:plan:new:mode:grp")
@require_bot_owner_handler
async def adm_plan_new_grp(callback: CallbackQuery, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    if await state.get_state() != AdminStates.add_plan_link.state:
        await callback.answer("ابتدا ساخت پلن را شروع کنید", show_alert=True)
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
            [InlineKeyboardButton(text="تمپلیتی نیست — از وب‌پنل بسازید", callback_data="adm:plans")]
        )
    back = f"adm:plan:view:{plan_id}" if plan_id else "adm:plans"
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=back)])
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
            [InlineKeyboardButton(text="گروهی نیست — از وب‌پنل بسازید", callback_data="adm:plans")]
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
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=back)])
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

@router.callback_query(F.data.startswith("adm:plan:picktpl:"))
@require_bot_owner_handler
async def adm_plan_pick_tpl(callback: CallbackQuery, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    plan_id = int(callback.data.split(":")[-1])
    await callback.answer()
    await state.update_data(new_plan=0, selected_groups=[])
    await _show_template_picker(callback, plan_id=plan_id)

@router.callback_query(F.data.startswith("adm:plan:pickgrp:"))
@require_bot_owner_handler
async def adm_plan_pick_grp(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
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

@router.callback_query(F.data.startswith("adm:plan:settpl:"))
@require_bot_owner_handler
async def adm_plan_set_tpl(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    parts = callback.data.split(":")
    plan_id = int(parts[-2])
    tpl_id = int(parts[-1])
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
        await callback.answer("یافت نشد", show_alert=True)
        return
    plan.pg_template_id = tpl_id
    plan.pg_group_ids = None
    await session.commit()
    await state.update_data(new_plan=0, selected_groups=[])
    await callback.answer("ذخیره شد")
    if callback.message:
        await safe_edit_text(
            callback.message,
            await _plan_detail_text(plan),
            reply_markup=await _plan_detail_markup(session, plan),
        )

@router.callback_query(F.data.startswith("adm:plan:toggrp:"))
@require_bot_owner_handler
async def adm_plan_tog_grp(callback: CallbackQuery, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
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

@router.callback_query(F.data.startswith("adm:plan:grpdone:"))
@require_bot_owner_handler
async def adm_plan_grp_done(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    plan_id = int(callback.data.split(":")[-1])
    data = await state.get_data()
    selected = [int(x) for x in (data.get("selected_groups") or [])]
    if not selected:
        await callback.answer("حداقل یک گروه انتخاب کنید", show_alert=True)
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
        await callback.answer("یافت نشد", show_alert=True)
        return
    plan.pg_template_id = None
    plan.pg_group_ids = group_csv
    await session.commit()
    await state.update_data(selected_groups=[], new_plan=0)
    await callback.answer("ذخیره شد")
    if callback.message:
        await safe_edit_text(
            callback.message,
            await _plan_detail_text(plan),
            reply_markup=await _plan_detail_markup(session, plan),
        )

@router.callback_query(F.data == "adm:plan:new:backlink")
@require_bot_owner_handler
async def adm_plan_new_back_link(callback: CallbackQuery, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await state.set_state(AdminStates.add_plan_link)
    await callback.answer()
    if callback.message:
        await safe_edit_text(
            callback.message,
            "اتصال پاسارگارد را انتخاب کنید:",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="📋 از تمپلیت", callback_data="adm:plan:new:mode:tpl")],
                    [InlineKeyboardButton(text="📁 با گروه (سفارشی)", callback_data="adm:plan:new:mode:grp")],
                    [InlineKeyboardButton(text="❌ انصراف", callback_data="adm:plans")],
                ]
            ),
        )

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
            rows.append(
                [
                    InlineKeyboardButton(
                        text=c.name[:48],
                        callback_data=f"adm:plan:newcat:{c.id}",
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    text="⬅️ رنگ دکمه", callback_data="adm:plan:new:backcolor"
                )
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
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    text="⬅️ بازگشت", callback_data=f"adm:plan:view:{plan_id}"
                )
            ]
        )
    if target.message:
        await safe_edit_text(
            target.message,
            "🏷 <b>برچسب دسته</b> (اختیاری)\n"
            "نوع پلن ثابت می‌ماند — این فقط برچسب نمایش در فروشگاه است.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )

@router.callback_query(F.data.startswith("adm:plan:newcolor:"))
@require_bot_owner_handler
async def adm_plan_new_color(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
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
        await callback.answer("دسترسی مالک سیستم لازم است", show_alert=True)
        return
    await callback.answer()
    await _show_plan_category_picker(
        callback, session, staff, plan_id=None, selected_id=None
    )

@router.callback_query(F.data == "adm:plan:new:backcolor")
@require_bot_owner_handler
async def adm_plan_new_back_color(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await state.set_state(AdminStates.add_plan_color)
    await callback.answer()
    await _show_user_plan_color_picker(
        callback,
        session=session,
        callback_prefix="adm:plan:newcolor",
        back_callback="adm:plan:new:backlink",
    )

@router.callback_query(F.data.startswith("adm:plan:newcat:"), AdminStates.add_plan_category)
@require_bot_owner_handler
async def adm_plan_new_category(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    staff = await _platform_catalog_staff(session, db_user)
    if not staff:
        await callback.answer("دسترسی مالک سیستم لازم است", show_alert=True)
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
        await callback.answer("ساخت پلن ناموفق بود", show_alert=True)
        return
    await callback.answer("ذخیره شد")
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

@router.callback_query(F.data.startswith("adm:plan:catpick:"))
@require_bot_owner_handler
async def adm_plan_cat_pick(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    staff = await _platform_catalog_staff(session, db_user)
    if not staff:
        await callback.answer("دسترسی مالک سیستم لازم است", show_alert=True)
        return
    pid = int(callback.data.rsplit(":", 1)[-1])
    plan = await session.get(Plan, pid)
    if not plan or plan.owner_reseller_id is not None:
        await callback.answer("یافت نشد", show_alert=True)
        return
    await callback.answer()
    await _show_plan_category_picker(
        callback,
        session,
        staff,
        plan_id=pid,
        selected_id=int(plan.category_id) if plan.category_id else None,
    )

@router.callback_query(F.data.startswith("adm:plan:setcat:"))
@require_bot_owner_handler
async def adm_plan_set_cat(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    staff = await _platform_catalog_staff(session, db_user)
    if not staff:
        await callback.answer("دسترسی مالک سیستم لازم است", show_alert=True)
        return
    parts = callback.data.split(":")
    # adm:plan:setcat:{pid}:{cid}
    if len(parts) < 5:
        await callback.answer("نامعتبر", show_alert=True)
        return
    pid = int(parts[3])
    raw = parts[4]
    plan = await session.get(Plan, pid)
    if not plan or plan.owner_reseller_id is not None:
        await callback.answer("یافت نشد", show_alert=True)
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
    await callback.answer("ذخیره شد")
    if callback.message:
        await safe_edit_text(
            callback.message,
            await _plan_detail_text_for(session, plan),
            reply_markup=await _plan_detail_markup(session, plan),
        )

@router.callback_query(F.data.startswith("adm:plan:colorpick:"))
@require_bot_owner_handler
async def adm_plan_color_pick(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    pid = int(callback.data.rsplit(":", 1)[-1])
    plan = await session.get(Plan, pid)
    if not plan:
        await callback.answer("یافت نشد", show_alert=True)
        return
    await callback.answer()
    await _show_user_plan_color_picker(
        callback,
        session=session,
        callback_prefix=f"adm:plan:setcolor:{pid}",
        back_callback=f"adm:plan:view:{pid}",
    )

@router.callback_query(F.data.startswith("adm:plan:setcolor:"))
@require_bot_owner_handler
async def adm_plan_set_color(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) < 5:
        await callback.answer("نامعتبر", show_alert=True)
        return
    pid = int(parts[3])
    from app.services.button_styles import parse_plan_button_style_callback

    plan = await session.get(Plan, pid)
    if not plan:
        await callback.answer("یافت نشد", show_alert=True)
        return
    plan.button_style = parse_plan_button_style_callback(parts[4])
    await session.commit()
    await callback.answer("رنگ ذخیره شد")
    if callback.message:
        await safe_edit_text(
            callback.message,
            await _plan_detail_text(plan),
            reply_markup=await _plan_detail_markup(session, plan),
        )

@router.callback_query(F.data.startswith("adm:plan:clearlink:"))
@require_bot_owner_handler
async def adm_plan_clear_link(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    plan = await session.get(Plan, int(callback.data.split(":")[-1]))
    if not plan:
        await callback.answer("یافت نشد", show_alert=True)
        return
    plan.pg_template_id = None
    plan.pg_group_ids = None
    await session.commit()
    await callback.answer("اتصال حذف شد")
    if callback.message:
        await callback.message.edit_text(
            await _plan_detail_text(plan),
            reply_markup=await _plan_detail_markup(session, plan),
        )

@router.callback_query(F.data.startswith("adm:plan:toggle:"))
@require_bot_owner_handler
async def plan_toggle(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    plan = await session.get(Plan, int(callback.data.split(":")[-1]))
    if not plan:
        await callback.answer("یافت نشد", show_alert=True)
        return
    plan.is_active = not plan.is_active
    await session.commit()
    await callback.answer("بروز شد")
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
            pass
    await _render_plans_list(callback, session)

@router.callback_query(F.data.startswith("adm:plan:delask:"))
@require_bot_owner_handler
async def adm_plan_del_ask(callback: CallbackQuery, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
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

@router.callback_query(F.data.startswith("adm:plan:del:"))
@require_bot_owner_handler
async def adm_plan_del(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    from app.services.plans_catalog import PlanDeleteBlocked, delete_shop_plan

    pid = int(callback.data.rsplit(":", 1)[-1])
    plan = await session.get(Plan, pid)
    if not plan or plan.is_trial:
        await callback.answer("یافت نشد", show_alert=True)
        return
    try:
        await delete_shop_plan(session, plan)
        await session.commit()
    except PlanDeleteBlocked as e:
        try:
            await session.rollback()
        except Exception:
            pass
        msg = e.message if len(e.message) <= 180 else e.message[:177] + "…"
        await callback.answer(msg, show_alert=True)
        return
    await callback.answer("حذف شد")
    await _render_plans_list(callback, session)

@router.callback_query(F.data == "adm:custom")
@require_bot_owner_handler
async def adm_custom(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await callback.answer()
    text, markup = await _custom_link_summary(session)
    if callback.message:
        await callback.message.edit_text(text, reply_markup=markup)

@router.callback_query(F.data == "adm:custom:toggle")
@require_bot_owner_handler
async def adm_custom_toggle(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    cur = await get_setting(session, "custom_plan_enabled")
    await set_setting(session, "custom_plan_enabled", "0" if on(cur) else "1")
    await callback.answer("بروز شد")
    text, markup = await _custom_link_summary(session)
    if callback.message:
        await callback.message.edit_text(text, reply_markup=markup)

@router.callback_query(F.data == "adm:custom:clearlink")
@require_bot_owner_handler
async def adm_custom_clear(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await set_setting(session, "custom_plan_template_id", "")
    await set_setting(session, "custom_plan_group_ids", "")
    await callback.answer("اتصال حذف شد")
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
            [InlineKeyboardButton(text="تمپلیتی نیست — از وب‌پنل بسازید", callback_data="adm:custom")]
        )
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="adm:custom")])
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
            [InlineKeyboardButton(text="گروهی نیست — از وب‌پنل بسازید", callback_data="adm:custom")]
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
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="adm:custom")])
    text = (
        "📁 گروه‌های اینباند برای پلن دلخواه:\n"
        f"انتخاب‌شده: {', '.join(str(x) for x in selected) or '—'}"
    )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

@router.callback_query(F.data == "adm:custom:picktpl")
@require_bot_owner_handler
async def adm_custom_pick_tpl(callback: CallbackQuery, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await callback.answer()
    await _show_custom_template_picker(callback)

@router.callback_query(F.data.startswith("adm:custom:settpl:"))
@require_bot_owner_handler
async def adm_custom_set_tpl(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    tpl_id = callback.data.split(":")[-1]
    await set_setting(session, "custom_plan_template_id", tpl_id)
    await set_setting(session, "custom_plan_group_ids", "")
    await callback.answer("ذخیره شد")
    text, markup = await _custom_link_summary(session)
    if callback.message:
        await callback.message.edit_text(text, reply_markup=markup)

@router.callback_query(F.data == "adm:custom:pickgrp")
@require_bot_owner_handler
async def adm_custom_pick_grp(callback: CallbackQuery, session: AsyncSession, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
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

@router.callback_query(F.data.startswith("adm:custom:toggrp:"))
@require_bot_owner_handler
async def adm_custom_tog_grp(callback: CallbackQuery, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
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

@router.callback_query(F.data == "adm:custom:grpdone")
@require_bot_owner_handler
async def adm_custom_grp_done(callback: CallbackQuery, session: AsyncSession, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    data = await state.get_data()
    selected = [int(x) for x in (data.get("custom_selected_groups") or [])]
    if not selected:
        await callback.answer("حداقل یک گروه انتخاب کنید", show_alert=True)
        return
    await set_setting(session, "custom_plan_group_ids", ",".join(str(x) for x in selected))
    await set_setting(session, "custom_plan_template_id", "")
    await state.update_data(custom_selected_groups=[])
    await callback.answer("ذخیره شد")
    text, markup = await _custom_link_summary(session)
    if callback.message:
        await callback.message.edit_text(text, reply_markup=markup)

@router.callback_query(F.data == "adm:users")
@require_bot_owner_handler
async def adm_users(callback: CallbackQuery, session: AsyncSession, db_user: BotUser, state: FSMContext | None = None):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await callback.answer()
    counts = await admin_customer_counts(session)
    total, blocked, orders = counts["users"], counts["blocked"], counts["orders"]
    text = (
        "👥 <b>کاربران بات</b>\n\n"
        f"کل: {total}\n"
        f"مسدود: {blocked}\n"
        f"سفارش‌ها: {orders}\n\n"
        "از کیبورد پایین لیست یا جستجو را انتخاب کنید."
    )
    if callback.message:
        from app.bot.tg_utils import safe_edit_text

        await safe_edit_text(callback.message, text, reply_markup=None)
        await callback.message.answer(
            "کاربران:",
            reply_markup=await _staff_reply(session, db_user, kb.admin_users_reply_keyboard()),
        )
        if state is not None:
            from app.bot import menu_nav as nav

            await nav.set_nav_level(state, nav.NAV_ADMIN_USERS, push=False)

USERS_PAGE_SIZE = 10

def _deny_if_outside_platform_shop(user: BotUser | None) -> str | None:
    """Owner admin tools are platform-shop only (reseller_id IS NULL)."""
    if user is None:
        return "یافت نشد"
    if user.reseller_id is not None:
        return "این کاربر متعلق به فروشگاه نماینده است"
    return None

async def _platform_shop_user(
    session: AsyncSession, user_id: int
) -> tuple[BotUser | None, str | None]:
    user = await session.get(BotUser, int(user_id))
    return user, _deny_if_outside_platform_shop(user)

@router.callback_query(F.data.startswith("adm:users:list:"))
@require_bot_owner_handler
async def adm_users_list(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    try:
        page = int((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        page = 0
    page = max(0, page)
    await callback.answer()
    from app.services.users_ops import (
        bot_user_alert_flags,
        build_user_ops_row,
        load_services_by_user_ids,
        scoped_users_where,
    )

    total = (
        await session.scalar(
            select(func.count()).select_from(BotUser).where(scoped_users_where(None))
        )
        or 0
    )
    result = await session.execute(
        select(BotUser)
        .where(scoped_users_where(None))
        .order_by(BotUser.id.desc())
        .offset(page * USERS_PAGE_SIZE)
        .limit(USERS_PAGE_SIZE)
    )
    users = list(result.scalars().all())
    by_svc = await load_services_by_user_ids(session, [int(u.id) for u in users])
    rows = []
    for u in users:
        ops = build_user_ops_row(u, by_svc.get(int(u.id), []))
        name = (u.full_name or u.username or str(u.telegram_id))[:16]
        flag = bot_user_alert_flags(ops)
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{flag} {name}",
                    callback_data=f"adm:users:view:{u.id}",
                )
            ]
        )
    has_prev = page > 0
    has_next = (page + 1) * USERS_PAGE_SIZE < total
    text = (
        f"👥 <b>لیست کاربران</b>\n"
        f"صفحه {page + 1} از {max(1, (total + USERS_PAGE_SIZE - 1) // USERS_PAGE_SIZE)}"
        f" · {total} نفر\n"
        f"<i>🔔 اعلان · ⏰ انقضا · 📉 حجم — جزئیات در وب‌پنل /users</i>"
    )
    if callback.message:
        from app.bot.nav_inline import with_inline_back

        ui = await get_all_settings(session)
        markup = kb.admin_users_list_keyboard(
            page=page, has_prev=has_prev, has_next=has_next, rows=rows
        )
        markup = with_inline_back(markup, ui, "nv:adm:users")
        await callback.message.edit_text(text, reply_markup=markup)

@router.callback_query(F.data == "adm:users:webhint")
@require_bot_owner_handler
async def adm_users_webhint(callback: CallbackQuery, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await callback.answer(
        "وب‌پنل /users: فیلتر نزدیک‌انقضا، حجم‌کم، اعلان‌ها و ویرایش کامل کاربران.",
        show_alert=True,
    )

@router.callback_query(F.data == "adm:users:search")
@require_bot_owner_handler
async def adm_users_search_start(callback: CallbackQuery, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await callback.answer()
    await state.set_state(AdminStates.user_search)
    if callback.message:
        await callback.message.answer(
            "آیدی عددی تلگرام کاربر را بفرستید:",
            reply_markup=kb.cancel_reply(),
        )

@router.message(AdminStates.user_search)
@require_bot_owner_handler
async def adm_users_search(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await state.clear()
        await message.answer("ادمین نیستید")
        return
    if kb.is_cancel_text(message.text):
        await _answer_users_nav(
            message,
            session,
            db_user,
            "لغو شد.",
            state,
            clear_state=True,
        )
        return
    try:
        tg_id = int((message.text or "").strip())
    except ValueError:
        await message.answer("آیدی عددی معتبر بفرستید")
        return
    result = await session.execute(
        select(BotUser).where(
            BotUser.telegram_id == tg_id,
            BotUser.reseller_id.is_(None),
        )
    )
    user = result.scalar_one_or_none()
    await state.clear()
    # cancel_reply() replaced the submenu — restore on a *lasting* message.
    # Tip-delete seeding drops the custom keyboard on many mobile clients.
    users_kb = kb.admin_users_reply_keyboard()
    if not user:
        await message.answer(
            "کاربری با این آیدی در فروشگاه پلتفرم یافت نشد.",
            reply_markup=users_kb,
        )
        return
    await message.answer("👤 نتیجه جستجو", reply_markup=users_kb)
    await _render_user_card(message, session, user)

async def _render_user_card(
    message: Message,
    session: AsyncSession,
    user: BotUser,
    *,
    confirm_delete: bool = False,
    edit: bool = False,
) -> None:
    from app.services.users_ops import (
        bot_user_alert_flags,
        build_user_ops_row,
        load_services_by_user_ids,
    )

    by = await load_services_by_user_ids(session, [int(user.id)])
    ops = build_user_ops_row(user, by.get(int(user.id), []))
    flags = bot_user_alert_flags(ops)
    blocked = "بله 🚫" if user.is_blocked else "خیر"
    alert_line = ""
    if ops.has_alert:
        bits = []
        if ops.expiring:
            bits.append(f"انقضا {ops.expire_text}")
        if ops.low_volume:
            bits.append(f"حجم {ops.volume_text}")
        alert_line = "\n🔔 اعلان: " + " · ".join(bits) if bits else "\n🔔 اعلان فعال"
    from app.services.color_tags import bot_tag_button_label, effective_color_tag

    tag_line = bot_tag_button_label(effective_color_tag(user))
    text = (
        f"{flags} <b>{html.escape(user.full_name or user.username or '—')}</b>\n\n"
        f"آیدی تلگرام: <code>{user.telegram_id}</code>\n"
        f"یوزرنیم: @{html.escape(user.username or '—')}\n"
        f"نقش: {html.escape(user.role)}\n"
        f"تگ ریسک: {html.escape(tag_line)}\n"
        f"کیف پول: {format_toman(user.wallet_balance, get_settings().currency)}\n"
        f"سرویس‌ها: {ops.service_count}\n"
        f"مسدود: {blocked}"
        f"{alert_line}\n\n"
        f"<i>وب‌پنل: /users?uid={user.id}</i>"
    )
    if confirm_delete:
        text += (
            "\n\n⚠️ <b>حذف کامل برگشت‌ناپذیر است</b> (سفارش‌ها، سرویس‌ها، تیکت‌ها)."
            "\nبعد از تأیید، علت حذف پرسیده می‌شود و برای کاربر ارسال می‌گردد."
        )
    ui = await get_all_settings(session)
    markup = kb.admin_user_actions(
        user.id,
        is_blocked=user.is_blocked,
        role=user.role,
        confirm_delete=confirm_delete,
        ui=ui,
        has_services=ops.service_count > 0,
        color_tag=effective_color_tag(user),
    )
    if edit:
        try:
            await message.edit_text(text, reply_markup=markup)
            return
        except Exception:
            pass
    await message.answer(text, reply_markup=markup)

@router.callback_query(F.data.startswith("adm:users:msg:"))
@require_bot_owner_handler
async def adm_users_message_start(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    user, deny = await _platform_shop_user(session, int(callback.data.split(":")[-1]))
    if deny:
        await callback.answer(deny, show_alert=True)
        return
    await callback.answer()
    await state.set_state(AdminStates.user_message)
    await state.update_data(msg_user_id=int(user.id))
    if callback.message:
        await callback.message.answer(
            f"✉️ متن پیام برای <b>{html.escape(user.full_name or user.username or str(user.telegram_id))}</b> را بفرستید:",
            reply_markup=kb.cancel_reply(),
        )

@router.message(AdminStates.user_message)
@require_bot_owner_handler
async def adm_users_message_send(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await state.clear()
        return
    if kb.is_cancel_text(message.text):
        await state.clear()
        await message.answer("لغو شد.", reply_markup=await _staff_reply(session, db_user, kb.admin_users_reply_keyboard()))
        return
    data = await state.get_data()
    uid = int(data.get("msg_user_id") or 0)
    user, deny = await _platform_shop_user(session, uid) if uid else (None, "یافت نشد")
    await state.clear()
    if deny:
        await message.answer(deny, reply_markup=await _staff_reply(session, db_user, kb.admin_users_reply_keyboard()))
        return
    from app.services.users_quick import send_staff_dm

    try:
        await send_staff_dm(
            session,
            user,
            message.text or "",
            actor=str(db_user.telegram_id or db_user.id),
        )
        await session.commit()
        await message.answer("پیام ارسال شد ✅", reply_markup=await _staff_reply(session, db_user, kb.admin_users_reply_keyboard()))
    except ValueError as e:
        await message.answer(user_safe_error(e), reply_markup=await _staff_reply(session, db_user, kb.admin_users_reply_keyboard()))
    except Exception as e:
        await message.answer(f"خطا: {user_safe_error(e)}", reply_markup=await _staff_reply(session, db_user, kb.admin_users_reply_keyboard()))
    await _render_user_card(message, session, user)

@router.callback_query(F.data.startswith("adm:users:view:"))
@require_bot_owner_handler
async def adm_users_view(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    user = await session.get(BotUser, int(callback.data.split(":")[-1]))
    deny = _deny_if_outside_platform_shop(user)
    if deny:
        await callback.answer(deny, show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await _render_user_card(callback.message, session, user, edit=True)

@router.callback_query(F.data.regexp(r"^adm:users:tag:\d+$"))
@require_bot_owner_handler
async def adm_users_tag_picker(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser
):
    """Show the 4-level risk-color picker for a bot user."""
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    try:
        uid = int((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer("نامعتبر", show_alert=True)
        return
    user = await session.get(BotUser, uid)
    deny = _deny_if_outside_platform_shop(user)
    if deny:
        await callback.answer(deny, show_alert=True)
        return
    from app.services.color_tags import bot_tag_button_label, effective_color_tag

    await callback.answer()
    if callback.message:
        cur = effective_color_tag(user)
        await safe_edit_text(
            callback.message,
            f"🏷 <b>تگ ریسک</b> — {html.escape(user.full_name or str(user.telegram_id))}\n"
            f"فعلی: {html.escape(bot_tag_button_label(cur))}\n\n"
            "یکی از چهار سطح را انتخاب کنید:",
            reply_markup=kb.admin_user_risk_tag_keyboard(user.id, current=cur),
        )

@router.callback_query(F.data.regexp(r"^adm:users:tagset:\d+:[a-z]+$"))
@require_bot_owner_handler
async def adm_users_tag_set(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser
):
    """Persist a whitelisted risk-color tag from the bot picker."""
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    # adm:users:tagset:{uid}:{key}
    if len(parts) != 5:
        await callback.answer("نامعتبر", show_alert=True)
        return
    try:
        uid = int(parts[3])
    except ValueError:
        await callback.answer("نامعتبر", show_alert=True)
        return
    key = parts[4]
    user = await session.get(BotUser, uid)
    deny = _deny_if_outside_platform_shop(user)
    if deny:
        await callback.answer(deny, show_alert=True)
        return
    from app.services.color_tags import (
        VALID_COLOR_KEYS,
        apply_color_tag,
        bot_tag_button_label,
    )

    if key not in VALID_COLOR_KEYS:
        await callback.answer("تگ نامعتبر", show_alert=True)
        return
    apply_color_tag(user, key)
    await session.commit()
    await callback.answer(f"ذخیره شد: {bot_tag_button_label(key)}"[:180])
    if callback.message:
        await _render_user_card(callback.message, session, user, edit=True)

@router.callback_query(F.data.startswith("adm:users:renew:"))
@require_bot_owner_handler
async def adm_users_quick_renew(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    try:
        uid = int((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer("نامعتبر", show_alert=True)
        return
    user = await session.get(BotUser, uid)
    deny = _deny_if_outside_platform_shop(user)
    if deny:
        await callback.answer(deny, show_alert=True)
        return
    from app.services.users_quick import quick_renew_user

    try:
        _svc, label = await quick_renew_user(session, user)
        await session.commit()
        await callback.answer(f"تمدید شد: {label}"[:180], show_alert=True)
    except ValueError as e:
        await callback.answer(user_safe_error(e, limit=160), show_alert=True)
    except Exception as e:
        await callback.answer(user_safe_error(e, limit=160), show_alert=True)
    if callback.message and user:
        await _render_user_card(callback.message, session, user, edit=True)

@router.callback_query(F.data.startswith("adm:users:wcredit:"))
@require_bot_owner_handler
async def adm_users_wallet_credit_ask(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    user_id = int(callback.data.split(":")[-1])
    user = await session.get(BotUser, user_id)
    deny = _deny_if_outside_platform_shop(user)
    if deny:
        await callback.answer(deny, show_alert=True)
        return
    await state.set_state(AdminStates.user_wallet_credit)
    await state.update_data(admin_credit_user_id=user_id)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            f"💰 مبلغ شارژ کیف پول برای <b>{html.escape(user.full_name or str(user.telegram_id))}</b>\n"
            f"موجودی فعلی: {format_toman(user.wallet_balance, get_settings().currency)}\n\n"
            "مبلغ را به تومان ارسال کنید (حداقل ۱۰۰۰):",
        )

@router.message(AdminStates.user_wallet_credit)
@require_bot_owner_handler
async def adm_users_wallet_credit_save(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await state.clear()
        return
    data = await state.get_data()
    user_id = int(data.get("admin_credit_user_id") or 0)
    user, deny = await _platform_shop_user(session, user_id) if user_id else (None, "یافت نشد")
    if deny:
        await state.clear()
        await message.answer(deny, reply_markup=await _staff_reply(session, db_user, kb.admin_users_reply_keyboard()))
        return
    amount = parse_bot_int(message.text or "")
    if amount is None or amount < 1000:
        await message.answer(
            "مبلغ نامعتبر — حداقل ۱۰۰۰ تومان",
            reply_markup=kb.cancel_reply(),
        )
        return
    from app.services.bot_user_admin import admin_credit_user_wallet

    try:
        await admin_credit_user_wallet(
            session,
            user,
            int(amount),
            actor=f"tg:{db_user.telegram_id}",
            note="شارژ از ربات ادمین",
        )
    except ValueError as e:
        await message.answer(user_safe_error(e), reply_markup=await _staff_reply(session, db_user, kb.admin_users_reply_keyboard()))
        await state.clear()
        return
    await state.clear()
    await session.refresh(user)
    await message.answer(
        f"✅ کیف پول شارژ شد.\n"
        f"مبلغ: {format_toman(amount, get_settings().currency)}\n"
        f"مانده: {format_toman(user.wallet_balance, get_settings().currency)}",
        reply_markup=await _staff_reply(session, db_user, kb.admin_users_reply_keyboard()),
    )
    await _render_user_card(message, session, user)

@router.callback_query(F.data.startswith("adm:users:svcs:"))
@require_bot_owner_handler
async def adm_users_services(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    user_id = int(callback.data.split(":")[-1])
    _user, deny = await _platform_shop_user(session, user_id)
    if deny:
        await callback.answer(deny, show_alert=True)
        return
    from app.services.bot_user_admin import list_service_snapshots, snapshot_telegram_lines

    snaps = await list_service_snapshots(session, user_id)
    await callback.answer()
    if not snaps:
        if callback.message:
            await safe_edit_text(
                callback.message,
                "سرویسی برای این کاربر نیست.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="⬅️ بازگشت",
                                callback_data=f"adm:users:view:{user_id}",
                            )
                        ]
                    ]
                ),
            )
        return
    lines = [f"📦 <b>سرویس‌های کاربر #{user_id}</b>", ""]
    for snap in snaps[:15]:
        lines.append(snapshot_telegram_lines(snap))
        lines.append("")
    if callback.message:
        await safe_edit_text(
            callback.message,
            "\n".join(lines).strip(),
            reply_markup=kb.admin_user_services_keyboard(
                user_id, [s.service.id for s in snaps]
            ),
        )

@router.callback_query(F.data.startswith("adm:users:svc:"))
@require_bot_owner_handler
async def adm_users_service_one(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    parts = callback.data.split(":")
    user_id = int(parts[3])
    service_id = int(parts[4])
    _user, deny = await _platform_shop_user(session, user_id)
    if deny:
        await callback.answer(deny, show_alert=True)
        return
    from app.services.bot_user_admin import get_owned_service, service_snapshot, snapshot_telegram_lines

    try:
        svc = await get_owned_service(
            session, bot_user_id=user_id, service_id=service_id
        )
        snap = await service_snapshot(session, svc)
        await session.commit()
    except ValueError as e:
        await callback.answer(user_safe_error(e, limit=160), show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await safe_edit_text(
            callback.message,
            snapshot_telegram_lines(snap),
            reply_markup=kb.admin_user_service_actions(user_id, service_id),
        )

@router.callback_query(F.data.startswith("adm:users:svclink:"))
@require_bot_owner_handler
async def adm_users_service_link(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    parts = callback.data.split(":")
    user_id = int(parts[3])
    service_id = int(parts[4])
    _user, deny = await _platform_shop_user(session, user_id)
    if deny:
        await callback.answer(deny, show_alert=True)
        return
    from app.services.bot_user_admin import get_owned_service, service_snapshot

    try:
        svc = await get_owned_service(
            session, bot_user_id=user_id, service_id=service_id
        )
        snap = await service_snapshot(session, svc)
        await session.commit()
    except ValueError as e:
        await callback.answer(user_safe_error(e, limit=160), show_alert=True)
        return
    url = snap.subscription_url
    if not url:
        await callback.answer("لینک موجود نیست", show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            f"🔗 لینک سرویس #{service_id}:\n<code>{html.escape(url)}</code>"
        )

@router.callback_query(F.data.startswith("adm:users:svcrenew:"))
@require_bot_owner_handler
async def adm_users_service_renew(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    parts = callback.data.split(":")
    user_id = int(parts[3])
    service_id = int(parts[4])
    _user, deny = await _platform_shop_user(session, user_id)
    if deny:
        await callback.answer(deny, show_alert=True)
        return
    from app.services.bot_user_admin import (
        admin_renew_service,
        get_owned_service,
        service_snapshot,
        snapshot_telegram_lines,
    )

    try:
        svc = await get_owned_service(
            session, bot_user_id=user_id, service_id=service_id
        )
        plan = await session.get(Plan, int(svc.plan_id)) if svc.plan_id else None
        if plan is None:
            await callback.answer("پلن سرویس مشخص نیست — از وب تمدید کنید", show_alert=True)
            return
        await admin_renew_service(session, svc, plan=plan, reset_traffic=True)
        snap = await service_snapshot(session, svc)
    except ValueError as e:
        await callback.answer(user_safe_error(e, limit=160), show_alert=True)
        return
    except Exception as e:
        await callback.answer(user_safe_error(e, limit=160), show_alert=True)
        return
    await callback.answer("تمدید شد")
    if callback.message:
        await safe_edit_text(
            callback.message,
            "✅ تمدید انجام شد\n\n" + snapshot_telegram_lines(snap),
            reply_markup=kb.admin_user_service_actions(user_id, service_id),
        )

@router.callback_query(F.data.startswith("adm:users:svcdelask:"))
@require_bot_owner_handler
async def adm_users_service_delete_ask(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    parts = callback.data.split(":")
    user_id = int(parts[3])
    service_id = int(parts[4])
    _user, deny = await _platform_shop_user(session, user_id)
    if deny:
        await callback.answer(deny, show_alert=True)
        return
    from app.services.bot_user_admin import get_owned_service

    try:
        svc = await get_owned_service(
            session, bot_user_id=user_id, service_id=service_id
        )
    except ValueError as e:
        await callback.answer(user_safe_error(e, limit=160), show_alert=True)
        return
    await callback.answer()
    label = svc.pg_username or f"#{service_id}"
    if callback.message:
        await safe_edit_text(
            callback.message,
            (
                f"⚠️ <b>حذف سرویس #{service_id}</b>\n\n"
                f"سرویس <code>{html.escape(str(label))}</code> و کاربر پاسارگارد مرتبط "
                f"برای همیشه حذف شوند؟\nاین عمل برگشت‌ناپذیر است."
            ),
            reply_markup=kb.admin_user_service_delete_confirm(user_id, service_id),
        )

@router.callback_query(F.data.startswith("adm:users:svcdel:"))
@require_bot_owner_handler
async def adm_users_service_delete(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    parts = callback.data.split(":")
    # adm:users:svcdel:{uid}:{sid} — not svcdelask
    if len(parts) < 5 or parts[2] != "svcdel":
        await callback.answer("نامعتبر", show_alert=True)
        return
    user_id = int(parts[3])
    service_id = int(parts[4])
    _user, deny = await _platform_shop_user(session, user_id)
    if deny:
        await callback.answer(deny, show_alert=True)
        return
    from app.services.bot_user_admin import admin_delete_service, get_owned_service

    try:
        svc = await get_owned_service(
            session, bot_user_id=user_id, service_id=service_id
        )
        await admin_delete_service(session, svc, delete_pg=True)
    except ValueError as e:
        await callback.answer(user_safe_error(e, limit=160), show_alert=True)
        return
    except Exception as e:
        await callback.answer(user_safe_error(e, limit=160), show_alert=True)
        return
    await callback.answer("سرویس حذف شد", show_alert=True)
    if callback.message:
        await safe_edit_text(
            callback.message,
            f"✅ سرویس #{service_id} حذف شد.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="⬅️ سرویس‌ها",
                            callback_data=f"adm:users:svcs:{user_id}",
                        )
                    ]
                ]
            ),
        )

@router.callback_query(F.data.startswith("adm:users:svcadj:"))
@require_bot_owner_handler
async def adm_users_service_adjust(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    # adm:users:svcadj:{uid}:{sid}[ :days|gb|confirm : +/-|input ]
    if len(parts) < 5:
        await callback.answer("نامعتبر", show_alert=True)
        return
    try:
        user_id = int(parts[3])
        service_id = int(parts[4])
    except ValueError:
        await callback.answer("نامعتبر", show_alert=True)
        return
    _user, deny = await _platform_shop_user(session, user_id)
    if deny:
        await callback.answer(deny, show_alert=True)
        return

    from app.services.bot_user_admin import MAX_EXTEND_DAYS, MAX_EXTEND_GB

    data = await state.get_data()
    adj_key = f"svcadj:{user_id}:{service_id}"
    stored = data.get(adj_key) if isinstance(data.get(adj_key), dict) else {}
    days = int(stored.get("days") or 0)
    gb = float(stored.get("gb") or 0)

    action = parts[5] if len(parts) > 5 else ""
    detail = parts[6] if len(parts) > 6 else ""

    def _clamp_days(v: int) -> int:
        return max(-MAX_EXTEND_DAYS, min(MAX_EXTEND_DAYS, int(v)))

    def _clamp_gb(v: float) -> float:
        return max(-float(MAX_EXTEND_GB), min(float(MAX_EXTEND_GB), float(v)))

    if action == "days" and detail in {"+", "-"}:
        days = _clamp_days(days + (1 if detail == "+" else -1))
        await state.update_data(**{adj_key: {"days": days, "gb": gb}})
        await callback.answer()
    elif action == "gb" and detail in {"+", "-"}:
        gb = _clamp_gb(gb + (1 if detail == "+" else -1))
        await state.update_data(**{adj_key: {"days": days, "gb": gb}})
        await callback.answer()
    elif action == "days" and detail == "input":
        await state.update_data(
            **{
                adj_key: {"days": days, "gb": gb},
                "svcadj_uid": user_id,
                "svcadj_sid": service_id,
            }
        )
        await state.set_state(AdminStates.svc_adjust_days_input)
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                f"تعداد روز تغییر را وارد کنید (±{MAX_EXTEND_DAYS}، منفی = کاهش):",
                reply_markup=kb.cancel_reply(),
            )
        return
    elif action == "gb" and detail == "input":
        await state.update_data(
            **{
                adj_key: {"days": days, "gb": gb},
                "svcadj_uid": user_id,
                "svcadj_sid": service_id,
            }
        )
        await state.set_state(AdminStates.svc_adjust_gb_input)
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                f"مقدار گیگ تغییر را وارد کنید (±{MAX_EXTEND_GB}، منفی = کاهش):",
                reply_markup=kb.cancel_reply(),
            )
        return
    elif action == "confirm":
        from app.services.bot_user_admin import (
            admin_extend_service,
            get_owned_service,
            service_snapshot,
            snapshot_telegram_lines,
        )

        try:
            svc = await get_owned_service(
                session, bot_user_id=user_id, service_id=service_id
            )
            await admin_extend_service(
                session, svc, extra_days=days, extra_gb=gb
            )
            snap = await service_snapshot(session, svc)
        except ValueError as e:
            await callback.answer(user_safe_error(e, limit=160), show_alert=True)
            return
        except Exception as e:
            await callback.answer(user_safe_error(e, limit=160), show_alert=True)
            return
        await state.update_data(**{adj_key: {"days": 0, "gb": 0.0}})
        await callback.answer("مانده به‌روز شد")
        if callback.message:
            await safe_edit_text(
                callback.message,
                "✅ مانده سرویس به‌روز شد\n\n" + snapshot_telegram_lines(snap),
                reply_markup=kb.admin_user_service_actions(user_id, service_id),
            )
        return
    else:
        # Open adjust screen
        await state.update_data(**{adj_key: {"days": days, "gb": gb}})
        await callback.answer()

    text = (
        f"⏱ <b>تغییر مانده سرویس #{service_id}</b>\n\n"
        f"روز: <b>{days}</b> · گیگ: <b>{gb}</b>\n"
        "مثبت = افزایش، منفی = کاهش. برای ورود دستی روی عدد بزنید."
    )
    if callback.message:
        await safe_edit_text(
            callback.message,
            text,
            reply_markup=kb.admin_user_service_adjust_keyboard(
                user_id, service_id, days=days, gb=gb
            ),
        )

@router.message(AdminStates.svc_adjust_days_input)
@require_bot_owner_handler
async def adm_svc_adjust_days_entered(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        return
    if kb.is_cancel_text(message.text):
        await state.set_state(None)
        await message.answer("لغو شد.", reply_markup=await _staff_reply(session, db_user, kb.admin_users_reply_keyboard()))
        return
    from app.services.bot_user_admin import MAX_EXTEND_DAYS

    try:
        days = parse_bot_int(message.text)
    except ValueError:
        await message.answer("عدد معتبر بفرستید", reply_markup=kb.cancel_reply())
        return
    if abs(days) > MAX_EXTEND_DAYS:
        await message.answer(
            f"روز باید بین ±{MAX_EXTEND_DAYS} باشد",
            reply_markup=kb.cancel_reply(),
        )
        return
    data = await state.get_data()
    user_id = int(data.get("svcadj_uid") or 0)
    service_id = int(data.get("svcadj_sid") or 0)
    if not user_id or not service_id:
        await state.set_state(None)
        await message.answer(
            "نشست منقضی شد — دوباره از منوی سرویس وارد شوید.",
            reply_markup=await _staff_reply(session, db_user, kb.admin_users_reply_keyboard()),
        )
        return
    adj_key = f"svcadj:{user_id}:{service_id}"
    stored = data.get(adj_key) if isinstance(data.get(adj_key), dict) else {}
    gb = float(stored.get("gb") or 0)
    await state.set_state(None)
    await state.update_data(**{adj_key: {"days": days, "gb": gb}})
    # Restore reply submenu (cancel_reply displaced it); then show inline adjust UI.
    await message.answer("👥 کاربران", reply_markup=await _staff_reply(session, db_user, kb.admin_users_reply_keyboard()))
    await message.answer(
        f"⏱ <b>تغییر مانده سرویس #{service_id}</b>\n\n"
        f"روز: <b>{days}</b> · گیگ: <b>{gb}</b>",
        reply_markup=kb.admin_user_service_adjust_keyboard(
            user_id, service_id, days=days, gb=gb
        ),
    )

@router.message(AdminStates.svc_adjust_gb_input)
@require_bot_owner_handler
async def adm_svc_adjust_gb_entered(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        return
    if kb.is_cancel_text(message.text):
        await state.set_state(None)
        await message.answer("لغو شد.", reply_markup=await _staff_reply(session, db_user, kb.admin_users_reply_keyboard()))
        return
    from app.services.bot_user_admin import MAX_EXTEND_GB

    try:
        gb = parse_bot_float(message.text)
    except ValueError:
        await message.answer("عدد معتبر بفرستید", reply_markup=kb.cancel_reply())
        return
    if abs(gb) > MAX_EXTEND_GB:
        await message.answer(
            f"گیگ باید بین ±{MAX_EXTEND_GB} باشد",
            reply_markup=kb.cancel_reply(),
        )
        return
    data = await state.get_data()
    user_id = int(data.get("svcadj_uid") or 0)
    service_id = int(data.get("svcadj_sid") or 0)
    if not user_id or not service_id:
        await state.set_state(None)
        await message.answer(
            "نشست منقضی شد — دوباره از منوی سرویس وارد شوید.",
            reply_markup=await _staff_reply(session, db_user, kb.admin_users_reply_keyboard()),
        )
        return
    adj_key = f"svcadj:{user_id}:{service_id}"
    stored = data.get(adj_key) if isinstance(data.get(adj_key), dict) else {}
    days = int(stored.get("days") or 0)
    await state.set_state(None)
    await state.update_data(**{adj_key: {"days": days, "gb": gb}})
    # Restore reply submenu (cancel_reply displaced it); then show inline adjust UI.
    await message.answer("👥 کاربران", reply_markup=await _staff_reply(session, db_user, kb.admin_users_reply_keyboard()))
    await message.answer(
        f"⏱ <b>تغییر مانده سرویس #{service_id}</b>\n\n"
        f"روز: <b>{days}</b> · گیگ: <b>{gb}</b>",
        reply_markup=kb.admin_user_service_adjust_keyboard(
            user_id, service_id, days=days, gb=gb
        ),
    )

@router.callback_query(F.data.startswith("adm:users:block:"))
@require_bot_owner_handler
async def adm_users_block(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    user_id = int(callback.data.split(":")[-1])
    user, deny = await _platform_shop_user(session, user_id)
    if deny:
        await callback.answer(deny, show_alert=True)
        return
    from app.services.users import is_protected_admin

    if is_protected_admin(user):
        await callback.answer("مسدود کردن ادمین مجاز نیست", show_alert=True)
        return

    # Unblock immediately; ask reason only when blocking
    if user.is_blocked:
        from app.services.notifications import notify_account_edit

        user.is_blocked = False
        await session.commit()
        await notify_account_edit(
            session,
            user=user,
            event="unblock",
            reason=None,
            actor=db_user.username or db_user.full_name or "ادمین ربات",
        )
        await callback.answer("رفع مسدودی شد ✅", show_alert=True)
        if callback.message:
            await _render_user_card(callback.message, session, user, edit=True)
        return

    await state.set_state(AdminStates.block_user_reason)
    await state.update_data(block_user_id=user_id)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            f"علت مسدودسازی کاربر <code>{user.telegram_id}</code> را بنویسید "
            "(حداقل ۳ کاراکتر — برای کاربر ارسال می‌شود):",
            reply_markup=kb.cancel_reply(),
        )

@router.message(AdminStates.block_user_reason)
@require_bot_owner_handler
async def adm_users_block_reason(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await state.clear()
        return
    if kb.is_cancel_text(message.text):
        await state.clear()
        await message.answer("لغو شد.", reply_markup=await _staff_reply(session, db_user, kb.admin_users_reply_keyboard()))
        return
    data = await state.get_data()
    user_id = int(data.get("block_user_id") or 0)
    reason = (message.text or "").strip()
    if len(reason) < 3:
        await message.answer("علت خیلی کوتاه است — حداقل ۳ کاراکتر.")
        return
    user, deny = await _platform_shop_user(session, user_id) if user_id else (None, "یافت نشد")
    if deny:
        await state.clear()
        await message.answer(deny, reply_markup=await _staff_reply(session, db_user, kb.admin_users_reply_keyboard()))
        return
    from app.services.notifications import notify_account_edit
    from app.services.users import is_protected_admin

    if is_protected_admin(user):
        await state.clear()
        await message.answer(
            "مسدود کردن ادمین مجاز نیست.",
            reply_markup=await _staff_reply(session, db_user, kb.admin_users_reply_keyboard()),
        )
        return
    user.is_blocked = True
    await session.commit()
    await notify_account_edit(
        session,
        user=user,
        event="block",
        reason=reason,
        actor=db_user.username or db_user.full_name or "ادمین ربات",
    )
    await state.clear()
    await message.answer(
        f"🚫 کاربر مسدود شد.\nعلت: {reason}",
        reply_markup=await _staff_reply(session, db_user, kb.admin_users_reply_keyboard()),
    )
    await _render_user_card(message, session, user, edit=False)

@router.callback_query(F.data.startswith("adm:users:delask:"))
@require_bot_owner_handler
async def adm_users_delask(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    user, deny = await _platform_shop_user(session, int(callback.data.split(":")[-1]))
    if deny:
        await callback.answer(deny, show_alert=True)
        return
    from app.services.users import is_protected_admin

    if is_protected_admin(user):
        await callback.answer("حذف ادمین مجاز نیست", show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await _render_user_card(
            callback.message, session, user, confirm_delete=True, edit=True
        )

@router.callback_query(F.data.startswith("adm:users:del:"))
@require_bot_owner_handler
async def adm_users_delete_ask_reason(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    """Confirm button → ask typed delete reason (same pattern as block/revoke)."""
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    user_id = int(callback.data.split(":")[-1])
    user, deny = await _platform_shop_user(session, user_id)
    if deny:
        await callback.answer(deny, show_alert=True)
        return
    from app.services.users import is_protected_admin

    if is_protected_admin(user):
        await callback.answer("حذف ادمین مجاز نیست", show_alert=True)
        return
    await callback.answer()
    await state.set_state(AdminStates.delete_user_reason)
    await state.update_data(delete_user_id=user_id)
    if callback.message:
        await callback.message.answer(
            f"علت حذف کامل کاربر <code>{user.telegram_id}</code> را بنویسید "
            "(حداقل ۳ کاراکتر — برای خود کاربر ارسال می‌شود):\n\nبرای لغو: انصراف",
            reply_markup=kb.cancel_reply(),
        )

@router.message(AdminStates.delete_user_reason)
@require_bot_owner_handler
async def adm_users_delete_reason(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await state.clear()
        return
    if kb.is_cancel_text(message.text):
        await state.clear()
        await message.answer("لغو شد.", reply_markup=await _staff_reply(session, db_user, kb.admin_users_reply_keyboard()))
        return
    reason = (message.text or "").strip()
    if len(reason) < 3:
        await message.answer("علت حداقل ۳ کاراکتر باشد. دوباره بنویسید یا انصراف بزنید:")
        return
    data = await state.get_data()
    user_id = int(data.get("delete_user_id") or 0)
    await state.clear()
    if not user_id:
        await message.answer("نشست منقضی شد. دوباره از کارت کاربر اقدام کنید.")
        return

    from app.services.notifications import notify_account_edit
    from app.services.users import delete_bot_user, is_protected_admin

    user, deny = await _platform_shop_user(session, user_id)
    if deny:
        await message.answer(deny, reply_markup=await _staff_reply(session, db_user, kb.admin_users_reply_keyboard()))
        return
    if is_protected_admin(user):
        await message.answer(
            "حذف ادمین مجاز نیست.",
            reply_markup=await _staff_reply(session, db_user, kb.admin_users_reply_keyboard()),
        )
        return

    await notify_account_edit(
        session,
        user=user,
        event="user_delete",
        reason=reason,
        actor=db_user.username or db_user.full_name or "ادمین ربات",
    )
    try:
        info = await delete_bot_user(
            session,
            user_id,
            actor_user_id=db_user.id,
        )
    except ValueError as e:
        await message.answer(user_safe_error(e), reply_markup=await _staff_reply(session, db_user, kb.admin_users_reply_keyboard()))
        return
    except Exception as e:
        await message.answer(f"خطا: {user_safe_error(e)}", reply_markup=await _staff_reply(session, db_user, kb.admin_users_reply_keyboard()))
        return

    # ReplyKeyboard cannot be attached via edit_text — that threw and surfaced
    # as «خطایی رخ داد» even after a successful delete. Send a new message.
    await message.answer(
        f"🗑 کاربر <code>{info.get('telegram_id')}</code> ({html.escape(str(info.get('name') or '—'))}) "
        f"حذف شد.\nعلت: {html.escape(reason)}",
        reply_markup=await _staff_reply(session, db_user, kb.admin_users_reply_keyboard()),
    )

@router.callback_query(F.data.startswith("adm:users:unres:"))
@require_bot_owner_handler
async def adm_users_unreseller_ask(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    user_id = int(callback.data.split(":")[-1])
    user, deny = await _platform_shop_user(session, user_id)
    if deny:
        await callback.answer(deny, show_alert=True)
        return
    from app.services.resellers import get_reseller_profile

    if not await get_reseller_profile(session, user_id):
        await callback.answer("این کاربر نماینده نیست", show_alert=True)
        return
    await callback.answer()
    await state.set_state(AdminStates.revoke_reseller_reason)
    await state.update_data(revoke_user_id=user_id)
    if callback.message:
        await callback.message.answer(
            f"علت حذف نمایندگی کاربر <code>{user.telegram_id}</code> را بنویسید "
            "(برای خود کاربر ارسال می‌شود):\n\nبرای لغو: انصراف",
            reply_markup=kb.cancel_reply(),
        )

@router.message(AdminStates.revoke_reseller_reason)
@require_bot_owner_handler
async def adm_users_unreseller_reason(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await state.clear()
        return
    if kb.is_cancel_text(message.text):
        await state.clear()
        await message.answer("لغو شد.", reply_markup=await _staff_reply(session, db_user, kb.admin_users_reply_keyboard()))
        return
    reason = (message.text or "").strip()
    if len(reason) < 3:
        await message.answer("علت حداقل ۳ کاراکتر باشد. دوباره بنویسید یا انصراف بزنید:")
        return
    data = await state.get_data()
    user_id = int(data.get("revoke_user_id") or 0)
    await state.clear()
    if not user_id:
        await message.answer("نشست منقضی شد. دوباره از کارت کاربر اقدام کنید.")
        return
    _user, deny = await _platform_shop_user(session, user_id)
    if deny:
        await message.answer(deny, reply_markup=await _staff_reply(session, db_user, kb.admin_users_reply_keyboard()))
        return

    from app.services.resellers import notify_reseller_revoked, revoke_reseller

    try:
        info = await revoke_reseller(
            session, user_id, delete_pg_admin=True, reason=reason
        )
    except ValueError as e:
        await message.answer(f"خطا: {user_safe_error(e)}", reply_markup=await _staff_reply(session, db_user, kb.admin_users_reply_keyboard()))
        return
    except Exception as e:
        await message.answer(f"خطا: {user_safe_error(e)}", reply_markup=await _staff_reply(session, db_user, kb.admin_users_reply_keyboard()))
        return

    user = await session.get(BotUser, user_id)
    notified = False
    if user:
        notified = await notify_reseller_revoked(
            int(info["telegram_id"]),
            reason,
            session=session,
            user=user,
            actor=db_user.username or db_user.full_name or "ادمین ربات",
        )
    else:
        notified = await notify_reseller_revoked(int(info["telegram_id"]), reason)
    note = "پیام علت ارسال شد ✅" if notified else "پیام تلگرام ارسال نشد ⚠️"
    await message.answer(
        f"🤝 نمایندگی حذف شد.\nعلت: {reason}\n{note}",
        reply_markup=await _staff_reply(session, db_user, kb.admin_users_reply_keyboard()),
    )
    if user:
        await _render_user_card(message, session, user)

@router.callback_query(F.data == "adm:resellers")
@require_bot_owner_handler
@require_platform_rep_mgmt
async def adm_resellers(callback: CallbackQuery, db_user: BotUser, state: FSMContext | None = None):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await callback.answer()
    if callback.message:
        from app.bot.tg_utils import safe_edit_text

        await safe_edit_text(
            callback.message,
            "🤝 <b>نمایندگان</b>\nاز کیبورد پایین بخش موردنظر را انتخاب کنید.",
            reply_markup=None,
        )
        await callback.message.answer(
            "پنل نمایندگان:",
            reply_markup=await _staff_reply(session, db_user, kb.admin_resellers_reply_keyboard()),
        )
        if state is not None:
            from app.bot import menu_nav as nav

            await nav.set_nav_level(state, nav.NAV_ADMIN_RESELLERS, push=False)

RESELLERS_PAGE_SIZE = 10

@router.callback_query(F.data.startswith("adm:resellers:list:"))
@require_bot_owner_handler
@require_platform_rep_mgmt
async def adm_resellers_list(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    try:
        page = int((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        page = 0
    page = max(0, page)
    await callback.answer()
    total = await session.scalar(
        select(func.count()).select_from(BotUser).where(BotUser.role == Role.RESELLER.value)
    ) or 0
    result = await session.execute(
        select(BotUser)
        .where(BotUser.role == Role.RESELLER.value)
        .order_by(BotUser.id.desc())
        .offset(page * RESELLERS_PAGE_SIZE)
        .limit(RESELLERS_PAGE_SIZE)
    )
    users = list(result.scalars().all())
    rows = []
    for u in users:
        name = (u.full_name or u.username or str(u.telegram_id))[:18]
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🤝 {name}",
                    callback_data=f"adm:resellers:view:{u.id}",
                )
            ]
        )
    has_prev = page > 0
    has_next = (page + 1) * RESELLERS_PAGE_SIZE < total
    text = (
        f"🤝 <b>لیست نمایندگان</b>\n"
        f"صفحه {page + 1} از {max(1, (total + RESELLERS_PAGE_SIZE - 1) // RESELLERS_PAGE_SIZE)}"
        f" · {total} نفر"
    )
    if not users:
        text += "\n\nنماینده‌ای ثبت نشده."
    if callback.message:
        await callback.message.edit_text(
            text,
            reply_markup=kb.admin_resellers_list_keyboard(
                page=page, has_prev=has_prev, has_next=has_next, rows=rows
            ),
        )

RESELLER_SVCS_PAGE_SIZE = 10

async def _render_reseller_card(
    message: Message,
    session: AsyncSession,
    user: BotUser,
    *,
    edit: bool = False,
) -> None:
    from app.services.color_tags import bot_tag_button_label, effective_color_tag
    from app.services.resellers import get_reseller_profile

    profile = await get_reseller_profile(session, int(user.id))
    shop_svc_count = await session.scalar(
        select(func.count()).select_from(UserService).where(UserService.bot_user_id == user.id)
    ) or 0
    pg_uname = (profile.pg_admin_username if profile else None) or "—"
    mode = (profile.billing_mode if profile else None) or "fixed"
    active = "فعال" if (profile and profile.is_active) else "غیرفعال"
    suspended = ""
    if profile and profile.billing_suspended_at:
        suspended = "\nوضعیت PAYG: <b>مسدود</b>"
    bot_uname = f"@{profile.bot_username}" if profile and profile.bot_username else "—"
    tag_line = bot_tag_button_label(effective_color_tag(user))
    text = (
        f"🤝 <b>{html.escape(user.full_name or user.username or '—')}</b>\n\n"
        f"آیدی تلگرام: <code>{user.telegram_id}</code>\n"
        f"یوزرنیم: @{html.escape(user.username or '—')}\n"
        f"تگ ریسک: {html.escape(tag_line)}\n"
        f"کیف پول: {format_toman(user.wallet_balance, get_settings().currency)}\n"
        f"ادمین پاسارگارد: <code>{html.escape(str(pg_uname))}</code>\n"
        f"حالت پرداخت: {html.escape(mode)}\n"
        f"نمایندگی: {active}{suspended}\n"
        f"ربات اختصاصی: {html.escape(bot_uname)}\n"
        f"سرویس‌های فروشگاه: {shop_svc_count}\n\n"
        f"<i>سرویس‌های پاسارگارد = کاربران VPN زیر ادمین نماینده</i>"
    )
    markup = kb.admin_reseller_actions(
        user.id,
        has_shop_services=int(shop_svc_count) > 0,
        color_tag=effective_color_tag(user),
    )
    if edit:
        try:
            await message.edit_text(text, reply_markup=markup)
            return
        except Exception:
            pass
    await message.answer(text, reply_markup=markup)

@router.callback_query(F.data.startswith("adm:resellers:view:"))
@require_bot_owner_handler
@require_platform_rep_mgmt
async def adm_resellers_view(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    user_id = int(callback.data.split(":")[-1])
    user = await session.get(BotUser, user_id)
    if not user or user.role != Role.RESELLER.value:
        await callback.answer("نماینده یافت نشد", show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await _render_reseller_card(callback.message, session, user, edit=True)

@router.callback_query(F.data.regexp(r"^adm:resellers:tag:\d+$"))
@require_bot_owner_handler
@require_platform_rep_mgmt
async def adm_resellers_tag_picker(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser
):
    """Show the 4-level risk-color picker for a reseller."""
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    try:
        uid = int((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer("نامعتبر", show_alert=True)
        return
    user = await session.get(BotUser, uid)
    if not user or user.role != Role.RESELLER.value:
        await callback.answer("نماینده یافت نشد", show_alert=True)
        return
    from app.services.color_tags import bot_tag_button_label, effective_color_tag

    await callback.answer()
    if callback.message:
        cur = effective_color_tag(user)
        await safe_edit_text(
            callback.message,
            f"🏷 <b>تگ ریسک نماینده</b> — {html.escape(user.full_name or str(user.telegram_id))}\n"
            f"فعلی: {html.escape(bot_tag_button_label(cur))}\n\n"
            "یکی از چهار سطح را انتخاب کنید:",
            reply_markup=kb.admin_reseller_risk_tag_keyboard(user.id, current=cur),
        )

@router.callback_query(F.data.regexp(r"^adm:resellers:tagset:\d+:[a-z]+$"))
@require_bot_owner_handler
@require_platform_rep_mgmt
async def adm_resellers_tag_set(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser
):
    """Persist a whitelisted risk-color tag from the reseller bot picker."""
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    # adm:resellers:tagset:{uid}:{key}
    if len(parts) != 5:
        await callback.answer("نامعتبر", show_alert=True)
        return
    try:
        uid = int(parts[3])
    except ValueError:
        await callback.answer("نامعتبر", show_alert=True)
        return
    key = parts[4]
    user = await session.get(BotUser, uid)
    if not user or user.role != Role.RESELLER.value:
        await callback.answer("نماینده یافت نشد", show_alert=True)
        return
    from app.services.color_tags import (
        VALID_COLOR_KEYS,
        apply_color_tag,
        bot_tag_button_label,
    )

    if key not in VALID_COLOR_KEYS:
        await callback.answer("تگ نامعتبر", show_alert=True)
        return
    apply_color_tag(user, key)
    await session.commit()
    await callback.answer(f"ذخیره شد: {bot_tag_button_label(key)}"[:180])
    if callback.message:
        await _render_reseller_card(callback.message, session, user, edit=True)

@router.callback_query(F.data.startswith("adm:resellers:capadj:"))
@require_bot_owner_handler
@require_platform_rep_mgmt
async def adm_resellers_capacity_adjust(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    # adm:resellers:capadj:{uid}[ :days|gb|confirm : +/-|input ]
    if len(parts) < 4:
        await callback.answer("نامعتبر", show_alert=True)
        return
    try:
        user_id = int(parts[3])
    except ValueError:
        await callback.answer("نامعتبر", show_alert=True)
        return

    from app.services.pg_admin_subscription import MAX_ADJUST_DAYS, MAX_ADJUST_GB
    from app.services.resellers import get_reseller_profile

    profile = await get_reseller_profile(session, user_id)
    if not profile or not (profile.pg_admin_username or "").strip():
        await callback.answer("ادمین پاسارگارد تنظیم نشده", show_alert=True)
        return

    data = await state.get_data()
    adj_key = f"capadj:{user_id}"
    stored = data.get(adj_key) if isinstance(data.get(adj_key), dict) else {}
    days = int(stored.get("days") or 0)
    gb = int(stored.get("gb") or 0)

    action = parts[4] if len(parts) > 4 else ""
    detail = parts[5] if len(parts) > 5 else ""

    def _clamp_days(v: int) -> int:
        return max(-MAX_ADJUST_DAYS, min(MAX_ADJUST_DAYS, int(v)))

    def _clamp_gb(v: int) -> int:
        return max(-MAX_ADJUST_GB, min(MAX_ADJUST_GB, int(v)))

    if action == "days" and detail in {"+", "-"}:
        days = _clamp_days(days + (1 if detail == "+" else -1))
        await state.update_data(**{adj_key: {"days": days, "gb": gb}})
        await callback.answer()
    elif action == "gb" and detail in {"+", "-"}:
        gb = _clamp_gb(gb + (1 if detail == "+" else -1))
        await state.update_data(**{adj_key: {"days": days, "gb": gb}})
        await callback.answer()
    elif action == "days" and detail == "input":
        await state.update_data(
            **{adj_key: {"days": days, "gb": gb}, "capadj_uid": user_id}
        )
        await state.set_state(AdminStates.reseller_cap_days_input)
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                f"تعداد روز تغییر را وارد کنید (±{MAX_ADJUST_DAYS}، منفی = کاهش):",
                reply_markup=kb.cancel_reply(),
            )
        return
    elif action == "gb" and detail == "input":
        await state.update_data(
            **{adj_key: {"days": days, "gb": gb}, "capadj_uid": user_id}
        )
        await state.set_state(AdminStates.reseller_cap_gb_input)
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                f"مقدار گیگ تغییر را وارد کنید (±{MAX_ADJUST_GB}، منفی = کاهش):",
                reply_markup=kb.cancel_reply(),
            )
        return
    elif action == "confirm":
        from app.services.reseller_capacity import admin_adjust_reseller_subscription

        try:
            result = await admin_adjust_reseller_subscription(
                session, profile, extra_days=days, extra_gb=gb
            )
        except ValueError as e:
            await callback.answer(user_safe_error(e, limit=160), show_alert=True)
            return
        except Exception as e:
            await callback.answer(user_safe_error(e, limit=160), show_alert=True)
            return
        await state.update_data(**{adj_key: {"days": 0, "gb": 0}})
        await callback.answer("ظرفیت به‌روز شد")
        total = result.get("total_gb", 0)
        exp = result.get("expires_at")
        exp_txt = str(exp) if exp else "نامحدود"
        shop_count = await session.scalar(
            select(func.count()).select_from(UserService).where(UserService.bot_user_id == user_id)
        ) or 0
        from app.services.color_tags import effective_color_tag

        reseller_user = await session.get(BotUser, user_id)
        if callback.message:
            await safe_edit_text(
                callback.message,
                f"✅ ظرفیت اشتراک به‌روز شد\n\nحجم کل: <b>{total}</b> گیگ\nانقضا: {html.escape(exp_txt)}",
                reply_markup=kb.admin_reseller_actions(
                    user_id,
                    has_shop_services=int(shop_count) > 0,
                    color_tag=effective_color_tag(reseller_user) if reseller_user else None,
                ),
            )
        return
    else:
        await state.update_data(**{adj_key: {"days": days, "gb": gb}})
        await callback.answer()

    text = (
        f"⏱ <b>تغییر ظرفیت نماینده #{user_id}</b>\n\n"
        f"روز: <b>{days}</b> · گیگ: <b>{gb}</b>\n"
        "مثبت = افزایش، منفی = کاهش. برای ورود دستی روی عدد بزنید."
    )
    if callback.message:
        await safe_edit_text(
            callback.message,
            text,
            reply_markup=kb.admin_reseller_capacity_adjust_keyboard(
                user_id, days=days, gb=gb
            ),
        )

@router.message(AdminStates.reseller_cap_days_input)
@require_bot_owner_handler
async def adm_reseller_cap_days_entered(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        return
    if kb.is_cancel_text(message.text):
        await state.set_state(None)
        await message.answer("لغو شد.")
        return
    from app.services.pg_admin_subscription import MAX_ADJUST_DAYS

    try:
        days = parse_bot_int(message.text)
    except ValueError:
        await message.answer("عدد معتبر بفرستید")
        return
    if abs(days) > MAX_ADJUST_DAYS:
        await message.answer(f"روز باید بین ±{MAX_ADJUST_DAYS} باشد")
        return
    data = await state.get_data()
    user_id = int(data.get("capadj_uid") or 0)
    if not user_id:
        await state.set_state(None)
        await message.answer("نشست منقضی شد.")
        return
    adj_key = f"capadj:{user_id}"
    stored = data.get(adj_key) if isinstance(data.get(adj_key), dict) else {}
    gb = int(stored.get("gb") or 0)
    await state.set_state(None)
    await state.update_data(**{adj_key: {"days": days, "gb": gb}})
    await message.answer(
        f"⏱ <b>تغییر ظرفیت نماینده #{user_id}</b>\n\n"
        f"روز: <b>{days}</b> · گیگ: <b>{gb}</b>",
        reply_markup=kb.admin_reseller_capacity_adjust_keyboard(
            user_id, days=days, gb=gb
        ),
    )

@router.message(AdminStates.reseller_cap_gb_input)
@require_bot_owner_handler
async def adm_reseller_cap_gb_entered(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        return
    if kb.is_cancel_text(message.text):
        await state.set_state(None)
        await message.answer("لغو شد.")
        return
    from app.services.pg_admin_subscription import MAX_ADJUST_GB

    try:
        gb = parse_bot_int(message.text)
    except ValueError:
        await message.answer("عدد معتبر بفرستید")
        return
    if abs(gb) > MAX_ADJUST_GB:
        await message.answer(f"گیگ باید بین ±{MAX_ADJUST_GB} باشد")
        return
    data = await state.get_data()
    user_id = int(data.get("capadj_uid") or 0)
    if not user_id:
        await state.set_state(None)
        await message.answer("نشست منقضی شد.")
        return
    adj_key = f"capadj:{user_id}"
    stored = data.get(adj_key) if isinstance(data.get(adj_key), dict) else {}
    days = int(stored.get("days") or 0)
    await state.set_state(None)
    await state.update_data(**{adj_key: {"days": days, "gb": gb}})
    await message.answer(
        f"⏱ <b>تغییر ظرفیت نماینده #{user_id}</b>\n\n"
        f"روز: <b>{days}</b> · گیگ: <b>{gb}</b>",
        reply_markup=kb.admin_reseller_capacity_adjust_keyboard(
            user_id, days=days, gb=gb
        ),
    )

@router.callback_query(F.data.startswith("adm:resellers:svcs:"))
@require_bot_owner_handler
@require_platform_rep_mgmt
async def adm_resellers_services(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    # adm:resellers:svcs:{id} or adm:resellers:svcs:{id}:{page}
    try:
        user_id = int(parts[3])
    except (IndexError, ValueError):
        await callback.answer("نامعتبر", show_alert=True)
        return
    page = 0
    if len(parts) >= 5:
        try:
            page = max(0, int(parts[4]))
        except ValueError:
            page = 0

    from app.services.pasarguard import as_list
    from app.services.resellers import get_reseller_profile

    user = await session.get(BotUser, user_id)
    profile = await get_reseller_profile(session, user_id)
    if not user or not profile:
        await callback.answer("نماینده یافت نشد", show_alert=True)
        return
    uname = (profile.pg_admin_username or "").strip()
    await callback.answer()
    back_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅️ بازگشت",
                    callback_data=f"adm:resellers:view:{user_id}",
                )
            ]
        ]
    )
    if not uname:
        if callback.message:
            await safe_edit_text(
                callback.message,
                "ادمین پاسارگارد برای این نماینده تنظیم نشده.",
                reply_markup=back_kb,
            )
        return
    try:
        data = await get_pg().get_users(
            admin=uname,
            limit=RESELLER_SVCS_PAGE_SIZE,
            offset=page * RESELLER_SVCS_PAGE_SIZE,
        )
    except Exception as e:
        if callback.message:
            await safe_edit_text(
                callback.message,
                f"❌ خطا در دریافت سرویس‌ها:\n{html.escape(user_safe_error(e, limit=200))}",
                reply_markup=back_kb,
            )
        return
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
    has_prev = page > 0
    if total is not None:
        has_next = (page + 1) * RESELLER_SVCS_PAGE_SIZE < total
        page_label = f"{page + 1}/{(max(total - 1, 0) // RESELLER_SVCS_PAGE_SIZE) + 1}"
        total_bit = f" · جمع: {total}"
    else:
        has_next = len(users) >= RESELLER_SVCS_PAGE_SIZE
        page_label = f"{page + 1}"
        total_bit = ""
    text = (
        f"📦 <b>سرویس‌های پاسارگارد نماینده</b>\n"
        f"ادمین: <code>{html.escape(uname)}</code>\n"
        f"صفحه {page_label}{total_bit}"
    )
    if not users:
        text += "\n\nسرویسی زیر این ادمین نیست."
    else:
        text += f"\n\n{len(users)} کاربر در این صفحه — برای جزئیات انتخاب کنید."
    if callback.message:
        await safe_edit_text(
            callback.message,
            text,
            reply_markup=kb.admin_reseller_services_keyboard(
                user_id,
                page=page,
                has_prev=has_prev,
                has_next=has_next,
                pg_users=users,
            ),
        )

@router.callback_query(F.data == "adm:resellers:add")
@require_bot_owner_handler
@require_platform_rep_mgmt
async def adm_resellers_add(callback: CallbackQuery, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await callback.answer()
    await state.set_state(AdminStates.make_reseller)
    if callback.message:
        await callback.message.answer(
            "آیدی عددی تلگرام کاربر را برای نماینده‌شدن بفرستید.\n\n"
            "مثال:\n"
            "<code>123456789 1</code>\n\n"
            "• عدد اول: آیدی تلگرام\n"
            "• عدد دوم: ۱ = اجازه تأیید رسید، ۰ یا خالی = بدون تأیید",
            reply_markup=kb.cancel_reply(),
        )

@router.callback_query(F.data == "adm:resapp:list")
@require_bot_owner_handler
@require_platform_rep_mgmt
async def adm_resapp_list(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    from app.db.models import ResellerApplicationStatus
    from app.services.resellers import list_open_applications, release_stale_pending_payment_apps

    await callback.answer()
    # Heal orphan pending_payment rows (order already cancelled) so the list matches reality.
    released = await release_stale_pending_payment_apps(session)
    if released:
        await session.commit()
    apps = await list_open_applications(session, limit=20)
    if not apps:
        if callback.message:
            await callback.message.edit_text(
                "درخواست معلقی نیست.",
                reply_markup=None,
            )
        return
    status_fa = {
        ResellerApplicationStatus.PENDING_PAYMENT.value: "💳 پرداخت",
        ResellerApplicationStatus.AWAITING_APPROVAL.value: "✅ تأیید",
    }
    rows = []
    for a in apps:
        u = a.user
        plan = a.plan
        st = status_fa.get(a.status, a.status)
        label = (
            f"#{a.id} [{st}] "
            f"{(u.full_name or str(u.telegram_id)) if u else '?'} — "
            f"{(plan.name if plan else '?')}"
        )
        rows.append([InlineKeyboardButton(text=label[:60], callback_data=f"adm:resapp:view:{a.id}")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="adm:resellers")])
    if callback.message:
        await callback.message.edit_text(
            "📋 درخواست‌های باز (پرداخت / تأیید):",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )

@router.callback_query(F.data.startswith("adm:resapp:view:"))
@require_bot_owner_handler
@require_platform_rep_mgmt
async def adm_resapp_view(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    from app.services.resellers import get_application

    app = await get_application(session, int(callback.data.split(":")[-1]))
    if not app:
        await callback.answer("یافت نشد", show_alert=True)
        return
    await callback.answer()
    u = app.user
    plan = app.plan
    from app.services.resellers import format_reseller_plan_apply_detail, reseller_billing_mode_label, reseller_plan_mode_of

    mode_label = reseller_billing_mode_label(reseller_plan_mode_of(plan)) if plan else "—"
    detail = (
        format_reseller_plan_apply_detail(plan, currency=get_settings().currency)
        if plan
        else "—"
    )
    from app.db.models import ResellerApplicationStatus

    status_fa = {
        ResellerApplicationStatus.PENDING_PAYMENT.value: "منتظر پرداخت",
        ResellerApplicationStatus.AWAITING_APPROVAL.value: "منتظر تأیید",
        ResellerApplicationStatus.APPROVED.value: "تأیید شده",
        ResellerApplicationStatus.REJECTED.value: "رد شده",
        ResellerApplicationStatus.CANCELLED.value: "لغو شده",
    }
    text = (
        f"🤝 درخواست #{app.id}\n"
        f"وضعیت: <b>{status_fa.get(app.status, app.status)}</b>\n"
        f"کاربر: {u.full_name or u.username or u.telegram_id if u else '—'}\n"
        f"تلگرام: <code>{u.telegram_id if u else '—'}</code>\n"
        f"نوع: <b>{mode_label}</b>\n\n"
        f"{detail}"
    )
    if callback.message:
        pending_pay = app.status == ResellerApplicationStatus.PENDING_PAYMENT.value
        await callback.message.edit_text(
            text,
            reply_markup=kb.reseller_app_review(app.id, allow_approve=not pending_pay),
        )

@router.callback_query(F.data.startswith("adm:resapp:ok:"))
@require_bot_owner_handler
@require_platform_rep_mgmt
async def adm_resapp_ok(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    from app.services.resellers import (
        approve_application,
        format_credentials_message,
        get_application,
        get_reseller_panel_base_url,
    )

    app = await get_application(session, int(callback.data.split(":")[-1]))
    if not app:
        await callback.answer("یافت نشد", show_alert=True)
        return
    try:
        creds = await approve_application(
            session,
            app,
            reviewer_tg=db_user.telegram_id,
            panel_base_url=await get_reseller_panel_base_url(session),
        )
    except Exception as e:
        await callback.answer(user_safe_error(e), show_alert=True)
        return
    user = await session.get(BotUser, app.user_id)
    if user:
        try:
            await callback.bot.send_message(
                user.telegram_id,
                format_credentials_message(creds),
                parse_mode="HTML",
            )
        except Exception:
            pass
    await callback.answer("تأیید شد ✅", show_alert=True)
    if callback.message:
        await callback.message.edit_text(
            f"✅ درخواست #{app.id} تأیید شد — اطلاعات ورود ارسال شد.",
            reply_markup=None,
        )

@router.callback_query(F.data.startswith("adm:resapp:no:"))
@require_bot_owner_handler
@require_platform_rep_mgmt
async def adm_resapp_no(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    from app.services.resellers import get_application, reject_application

    app = await get_application(session, int(callback.data.split(":")[-1]))
    if not app:
        await callback.answer("یافت نشد", show_alert=True)
        return
    try:
        await reject_application(session, app, reviewer_tg=db_user.telegram_id)
    except Exception as e:
        await callback.answer(user_safe_error(e), show_alert=True)
        return
    user = await session.get(BotUser, app.user_id)
    if user:
        try:
            await callback.bot.send_message(
                user.telegram_id,
                "❌ درخواست نمایندگی شما رد شد.\nدر صورت نیاز با پشتیبانی در ارتباط باشید.",
            )
        except Exception:
            pass
    await callback.answer("رد شد", show_alert=True)
    if callback.message:
        await callback.message.edit_text(
            f"❌ درخواست #{app.id} رد شد.",
            reply_markup=None,
        )

@router.message(AdminStates.make_reseller)
@require_bot_owner_handler
@require_platform_rep_mgmt
async def make_res(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await state.clear()
        await message.answer("ادمین نیستید")
        return
    if kb.is_cancel_text(message.text):
        await state.clear()
        await message.answer("لغو شد.", reply_markup=await _admin_hub_kb(session, db_user))
        return
    parts = (message.text or "").split()
    try:
        tg_id = int(parts[0])
        can_approve = len(parts) > 1 and parts[1] in {"1", "approve=1", "yes", "بله"}
    except ValueError:
        await message.answer(
            "فرمت نامعتبر است.\nمثال: <code>123456789 1</code>"
        )
        return
    result = await session.execute(select(BotUser).where(BotUser.telegram_id == tg_id))
    user = result.scalar_one_or_none()
    if not user:
        await message.answer("کاربر باید حداقل یک بار ربات را استارت کرده باشد.")
        return
    from app.services.resellers import (
        format_credentials_message,
        get_reseller_panel_base_url,
        provision_reseller,
    )

    perms = "dashboard,orders,tickets,stats"
    if can_approve:
        perms = "dashboard,orders,payments,tickets,stats"
    try:
        creds = await provision_reseller(
            session,
            user=user,
            can_approve_receipts=can_approve,
            web_permissions=perms,
            bot_permissions=perms,
            create_pg_admin=True,
            panel_base_url=await get_reseller_panel_base_url(session),
        )
    except Exception as e:
        await message.answer(f"خطا: {user_safe_error(e)}")
        return
    await state.clear()
    try:
        await message.bot.send_message(
            user.telegram_id,
            format_credentials_message(creds),
            parse_mode="HTML",
        )
    except Exception:
        pass
    await message.answer(
        f"کاربر {tg_id} نماینده شد ✅ — اطلاعات ورود ارسال شد",
        reply_markup=await _admin_hub_kb(session, db_user),
    )

@router.callback_query(F.data == "adm:tickets")
@require_bot_owner_handler
async def adm_tickets(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await callback.answer()
    tickets = await list_open_tickets(session, platform_only=True)
    from app.bot.nav_inline import with_inline_back

    ui = await get_all_settings(session)
    if not tickets:
        if callback.message:
            markup = (
                with_inline_back(None, ui, "nv:adm:ops")
            )
            await callback.message.edit_text("تیکت بازی نیست.", reply_markup=markup)
        return
    rows = [
        [InlineKeyboardButton(text=f"#{t.id} {t.subject[:24]}", callback_data=f"adm:ticket:{t.id}")]
        for t in tickets[:20]
    ]
    if callback.message:
        markup = InlineKeyboardMarkup(inline_keyboard=rows)
        markup = with_inline_back(markup, ui, "nv:adm:ops")
        await callback.message.edit_text(
            "🎫 تیکت‌های باز:",
            reply_markup=markup,
        )

@router.callback_query(F.data.startswith("adm:ticket:"))
@require_bot_owner_handler
async def adm_ticket_view(callback: CallbackQuery, session: AsyncSession, db_user: BotUser, state: FSMContext):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    ticket = await get_ticket(session, int(callback.data.split(":")[-1]))
    if not ticket:
        await callback.answer("یافت نشد", show_alert=True)
        return
    if ticket.reseller_id:
        await callback.answer("این تیکت متعلق به فروشگاه نماینده است", show_alert=True)
        return
    ticket_user = await session.get(BotUser, int(ticket.user_id))
    if ticket_user and ticket_user.reseller_id:
        await callback.answer("این تیکت متعلق به فروشگاه نماینده است", show_alert=True)
        return
    await callback.answer()
    lines = [f"🎫 #{ticket.id} — {html.escape(ticket.subject or '')}"]
    for m in ticket.messages[-12:]:
        who = "پشتیبانی" if m.is_staff else "کاربر"
        lines.append(f"<b>{who}:</b> {html.escape(m.body or '')}")
    await state.set_state(AdminStates.ticket_reply)
    await state.update_data(ticket_id=ticket.id)
    if callback.message:
        await callback.message.edit_text("\n".join(lines))
        await callback.message.answer("پاسخ را بنویسید:", reply_markup=kb.cancel_reply())

@router.message(AdminStates.ticket_reply)
async def adm_ticket_reply(message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser):
    if kb.is_cancel_text(message.text):
        await state.clear()
        await message.answer("لغو شد.", reply_markup=await _admin_hub_kb(session, db_user))
        return
    data = await state.get_data()
    ticket = await session.get(Ticket, data.get("ticket_id"))
    if not ticket:
        await state.clear()
        return
    if ticket.reseller_id:
        await state.clear()
        await message.answer("دسترسی به تیکت فروشگاه ندارید.", reply_markup=await _admin_hub_kb(session, db_user))
        return
    ticket_user = await session.get(BotUser, int(ticket.user_id))
    if ticket_user and ticket_user.reseller_id:
        await state.clear()
        await message.answer("دسترسی به تیکت فروشگاه ندارید.", reply_markup=await _admin_hub_kb(session, db_user))
        return
    await reply_ticket(session, ticket, message.text or "", db_user.telegram_id, is_staff=True)
    await state.clear()
    try:
        from app.services.notifications import notify_ticket_message

        await notify_ticket_message(
            message.bot,
            session,
            ticket_id=ticket.id,
            subject=ticket.subject,
            body=message.text or "",
            from_staff=True,
            ticket_user_id=ticket.user_id,
            actor_name=db_user.full_name or db_user.username,
            ticket_reseller_id=ticket.reseller_id,
        )
    except Exception:
        pass
    await message.answer("ارسال شد ✅", reply_markup=await _admin_hub_kb(session, db_user))

@router.callback_query(F.data == "adm:broadcast")
@require_bot_owner_handler
async def adm_broadcast_start(callback: CallbackQuery, db_user: BotUser, state: FSMContext):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await callback.answer()
    await state.clear()
    if callback.message:
        await callback.message.edit_text(
            "📢 <b>پیام گروهی</b>\nمخاطب را از کیبورد پایین انتخاب کنید."
        )
        await callback.message.answer(
            "مخاطب پیام گروهی:",
            reply_markup=await _staff_reply(session, db_user, kb.admin_broadcast_reply_keyboard()),
        )

@router.callback_query(F.data.startswith("adm:broadcast:aud:"))
@require_bot_owner_handler
async def adm_broadcast_audience(callback: CallbackQuery, db_user: BotUser, state: FSMContext):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    audience = callback.data.rsplit(":", 1)[-1]
    if audience not in {"all", "users", "resellers", "admins"}:
        await callback.answer("نامعتبر", show_alert=True)
        return
    await callback.answer()
    await state.set_state(AdminStates.broadcast_text)
    await state.update_data(broadcast_audience=audience)
    audience_fa = {
        "all": "همه",
        "users": "کاربران عادی",
        "resellers": "نمایندگان",
        "admins": "ادمین‌ها",
    }.get(audience, audience)
    if callback.message:
        await callback.message.edit_text(
            f"📢 مخاطب: <b>{audience_fa}</b>\nمتن پیام را بفرستید (HTML ساده).\nبرای لغو: انصراف"
        )
        await callback.message.answer("متن پیام:", reply_markup=kb.cancel_reply())

@router.message(AdminStates.broadcast_text)
@require_bot_owner_handler
async def adm_broadcast_send(message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await state.clear()
        return
    if kb.is_cancel_text(message.text):
        await state.clear()
        await message.answer(
            "لغو شد.",
            reply_markup=await _staff_reply(session, db_user, kb.admin_broadcast_reply_keyboard()),
        )
        return
    data = await state.get_data()
    audience = data.get("broadcast_audience") or "all"
    await state.clear()
    from app.services.broadcast import send_broadcast

    await message.answer("در حال ارسال…")
    try:
        result = await send_broadcast(
            message.bot,
            session,
            text=message.text or "",
            audience=audience,
            created_by=str(db_user.telegram_id),
        )
    except ValueError as e:
        await message.answer(user_safe_error(e), reply_markup=await _admin_hub_kb(session, db_user))
        return
    except Exception as e:
        await message.answer(f"خطا: {user_safe_error(e)}", reply_markup=await _admin_hub_kb(session, db_user))
        return
    await message.answer(
        f"✅ ارسال شد\nموفق: {result['ok']} / {result['total']}\nناموفق: {result['fail']}",
        reply_markup=await _admin_hub_kb(session, db_user),
    )

@router.callback_query(F.data == "adm:pg:stats")
@require_bot_owner_handler
async def pg_stats(callback: CallbackQuery, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    from app.bot.auth import can_platform_pg_page

    if not await can_platform_pg_page(db_user, "pg_overview"):
        await callback.answer("به نمای کلی دسترسی ندارید", show_alert=True)
        return
    await callback.answer()
    try:
        stats = await get_pg().get_system_stats()
    except Exception as e:
        if callback.message:
            await callback.message.edit_text(f"خطا: {user_safe_error(e)}", reply_markup=None)
        return
    text = "🏠 <b>نمای کلی پاسارگارد</b>\n\n" + format_system_stats(stats)
    if callback.message:
        await callback.message.edit_text(text[:3500], reply_markup=None)

# Node ops: app.bot.handlers.admin_pg_nodes (web /pg/nodes parity)

# PG hub / catalog hints live on admin_pg_users (no Owner middleware).
from app.bot.handlers.admin_pg_users import (  # noqa: E402,F401
    adm_pg,
    adm_pg_group_hint,
    adm_pg_template_hint,
)
