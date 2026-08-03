from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.auth import is_platform_admin as _is_admin
from app.config import get_settings
from app.db.models import BotUser, Order, OrderStatus, Payment, PaymentStatus, Plan, Role, Ticket, UserService
from app.services.formatting import (
    format_system_stats,
    format_toman,
    node_status_fa,
    order_status_fa,
)
from app.services.orders import approve_payment, deliver_order, reject_payment
from app.services.pasarguard import get_pg
from app.services.tickets import get_ticket, list_open_tickets, reply_ticket
from app.services.users import get_all_settings, get_setting, on, set_setting
from app.services.updates import local_version


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


async def _render_plans_list(callback: CallbackQuery, session: AsyncSession) -> None:
    result = await session.execute(select(Plan).order_by(Plan.sort_order, Plan.id))
    plans = list(result.scalars().all())
    if not plans:
        text = "📦 <b>پلن‌های فروش</b>\n\nهنوز پلنی ثبت نشده است.\nساخت پلن از کیبورد پایین."
    else:
        cards = "\n\n".join(_plan_line(p) for p in plans[:20])
        text = f"📦 <b>پلن‌های فروش</b>\n\n{cards}"
    if callback.message:
        await callback.message.edit_text(
            text,
            reply_markup=kb.admin_plans_list_keyboard(plans),
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
        "قیمت و محدوده → تنظیمات ← سرویس و دسترسی ← پلن دلخواه"
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
            InlineKeyboardButton(text="⬅️ پلن دلخواه", callback_data="adm:st:sub:service:custom"),
            InlineKeyboardButton(text="پلن‌ها", callback_data="adm:plans"),
        ]
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _plan_detail_text(p: Plan) -> str:
    gb = f"{p.data_limit_gb:g} گیگ" if p.data_limit_gb is not None else "نامحدود"
    if p.pg_template_id:
        link = f"تمپلیت #{p.pg_template_id}"
    elif p.pg_group_ids:
        link = f"گروه‌ها: {p.pg_group_ids}"
    else:
        link = "⚠️ هنوز به تمپلیت/گروه وصل نشده — خرید تحویل نمی‌شود"
    return (
        f"💎 <b>پلن #{p.id}</b> — {p.name}\n\n"
        f"قیمت: {format_toman(p.price, get_settings().currency)}\n"
        f"مدت: {p.duration_days} روز\n"
        f"حجم: {gb}\n"
        f"وضعیت: {'فعال' if p.is_active else 'خاموش'}\n"
        f"اتصال پاسارگارد: {link}"
    )


def _plan_detail_keyboard(p: Plan) -> InlineKeyboardMarkup:
    """Plan link/toggle actions only — list nav via reply Back."""
    rows = [
        [
            InlineKeyboardButton(
                text="⏸ خاموش" if p.is_active else "▶️ روشن",
                callback_data=f"adm:plan:toggle:{p.id}",
            )
        ],
        [
            InlineKeyboardButton(
                text="📋 اتصال به تمپلیت",
                callback_data=f"adm:plan:picktpl:{p.id}",
            )
        ],
        [
            InlineKeyboardButton(
                text="📁 اتصال به گروه",
                callback_data=f"adm:plan:pickgrp:{p.id}",
            )
        ],
    ]
    if p.pg_template_id or p.pg_group_ids:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🧹 حذف اتصال پاسارگارد",
                    callback_data=f"adm:plan:clearlink:{p.id}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)

router = Router(name="admin")


class AdminStates(StatesGroup):
    add_plan_name = State()
    add_plan_price = State()
    add_plan_days = State()
    add_plan_gb = State()
    add_plan_link = State()  # waiting for mode after basics
    make_reseller = State()
    ticket_reply = State()
    user_search = State()
    revoke_reseller_reason = State()
    block_user_reason = State()
    broadcast_text = State()
    broadcast_audience = State()


@router.callback_query(F.data == "adm:home")
async def adm_home(callback: CallbackQuery, db_user: BotUser):
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
            reply_markup=kb.admin_reply_keyboard(),
        )


@router.callback_query(F.data == "adm:dash")
async def adm_dash(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await callback.answer()
    from sqlalchemy import or_

    users_count = await session.scalar(select(func.count()).select_from(BotUser)) or 0
    # Platform-scoped aggregates only (exclude shop-tenant orders/payments)
    orders_count = await session.scalar(
        select(func.count()).select_from(Order).where(Order.reseller_id.is_(None))
    ) or 0
    pending_pay = await session.scalar(
        select(func.count())
        .select_from(Payment)
        .outerjoin(Order, Order.id == Payment.order_id)
        .where(
            Payment.status == PaymentStatus.PENDING.value,
            Payment.receipt_file_id.is_not(None),
            or_(
                Payment.is_wallet_topup.is_(True),
                Order.reseller_id.is_(None),
            ),
        )
    ) or 0
    pending_orders = await session.scalar(
        select(func.count())
        .select_from(Order)
        .where(
            Order.reseller_id.is_(None),
            Order.status.in_([OrderStatus.AWAITING_APPROVAL.value, OrderStatus.PAID.value]),
        )
    ) or 0
    services = await session.scalar(select(func.count()).select_from(UserService)) or 0
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
    rows = [
        # no inline back — reply KB Back restores admin hub
    ]
    if callback.message:
        await callback.message.edit_text(text, reply_markup=None)


def _order_actions(order: Order, payment: Payment | None) -> list[list[InlineKeyboardButton]]:
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
                InlineKeyboardButton(text="✅ تأیید", callback_data=f"ordrev:ok:{order.id}"),
                InlineKeyboardButton(text="❌ رد", callback_data=f"ordrev:no:{order.id}"),
            ]
        )
    return rows


@router.callback_query(F.data == "adm:orders")
async def adm_orders(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await callback.answer()
    result = await session.execute(
        select(Order)
        .where(Order.reseller_id.is_(None))
        .order_by(Order.id.desc())
        .limit(12)
    )
    orders = list(result.scalars().all())
    if not orders:
        if callback.message:
            await callback.message.edit_text("سفارشی نیست.", reply_markup=None)
        return
    rows = []
    for o in orders:
        label = f"#{o.id} · {order_status_fa(o.status)} · {format_toman(o.amount, get_settings().currency)}"
        rows.append([InlineKeyboardButton(text=label[:64], callback_data=f"adm:order:{o.id}")])
    if callback.message:
        await callback.message.edit_text(
            "🛒 <b>سفارش‌ها</b>\nیکی را برای جزئیات و تأیید/رد انتخاب کنید:\n"
            "<i>بازگشت از کیبورد پایین</i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )


@router.callback_query(F.data.startswith("adm:order:"))
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
    rows = _order_actions(order, pay)
    rows.append([InlineKeyboardButton(text="⬅️ لیست سفارش‌ها", callback_data="adm:orders")])
    if callback.message:
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def _approve_order_bot(session: AsyncSession, order: Order, bot) -> str:
    pay = (
        await session.execute(
            select(Payment).where(Payment.order_id == order.id).order_by(Payment.id.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if order.status == OrderStatus.DELIVERED.value:
        return "قبلاً تحویل شده"
    delivered = None
    if pay and pay.status == PaymentStatus.PENDING.value:
        delivered = await approve_payment(session, pay, reviewer_tg=0)
        try:
            from app.services.delivery import send_delivery_to_user

            user = await session.get(BotUser, pay.user_id)
            if user:
                await send_delivery_to_user(
                    bot, user.telegram_id, session, pay, delivered or order
                )
        except Exception:
            pass
        msg = "سفارش تأیید و تحویل شد"
    elif order.status == OrderStatus.PAID.value or (
        pay and pay.status == PaymentStatus.APPROVED.value and order.status != OrderStatus.DELIVERED.value
    ):
        delivered = await deliver_order(session, order)
        if pay:
            try:
                from app.services.delivery import send_delivery_to_user

                user = await session.get(BotUser, pay.user_id)
                if user:
                    await send_delivery_to_user(
                        bot, user.telegram_id, session, pay, delivered
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
        msg = await _approve_order_bot(session, order, callback.bot)
        await callback.answer(msg, show_alert=True)
    except Exception as e:
        await callback.answer(str(e), show_alert=True)
        return
    await session.refresh(order)
    text = f"🛒 سفارش #{order.id}\nوضعیت: <b>{order_status_fa(order.status)}</b>\n✅ انجام شد"
    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=kb.order_review(order.id) if order.status != OrderStatus.DELIVERED.value else InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ سفارش‌ها", callback_data="adm:orders")]]))
        except Exception:
            pass


@router.callback_query(F.data.startswith("ordrev:no:"))
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
        await reject_payment(session, pay, reviewer_tg=db_user.telegram_id, note="bot reject")
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
    if not payments:
        if callback.message:
            await callback.message.edit_text("رسید معلقی نیست.", reply_markup=None)
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
        await callback.message.edit_text("رسیدها ارسال شد.", reply_markup=None)


@router.callback_query(F.data == "adm:plans")
async def adm_plans(callback: CallbackQuery, session: AsyncSession, db_user: BotUser, state: FSMContext):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await state.clear()
    await callback.answer()
    await _render_plans_list(callback, session)


@router.callback_query(F.data.startswith("adm:plan:view:"))
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
            await _plan_detail_text(plan),
            reply_markup=_plan_detail_keyboard(plan),
        )


@router.callback_query(F.data == "adm:plan:add")
async def adm_plan_add(callback: CallbackQuery, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await callback.answer()
    await state.set_state(AdminStates.add_plan_name)
    if callback.message:
        await callback.message.answer("نام پلن را بفرستید:", reply_markup=kb.cancel_reply())


@router.message(AdminStates.add_plan_name)
async def plan_name(message: Message, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await state.clear()
        await message.answer("ادمین نیستید")
        return
    if kb.is_cancel_text(message.text):
        await state.clear()
        await message.answer("لغو شد.", reply_markup=kb.admin_plans_reply_keyboard())
        return
    await state.update_data(name=(message.text or "").strip())
    await state.set_state(AdminStates.add_plan_price)
    await message.answer("قیمت به تومان را بفرستید:", reply_markup=kb.cancel_reply())


@router.message(AdminStates.add_plan_price)
async def plan_price(message: Message, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await state.clear()
        await message.answer("ادمین نیستید")
        return
    if kb.is_cancel_text(message.text):
        await state.clear()
        await message.answer("لغو شد.", reply_markup=kb.admin_plans_reply_keyboard())
        return
    try:
        price = int((message.text or "").replace(",", "").replace("٬", ""))
    except ValueError:
        await message.answer("یک عدد معتبر بفرستید.", reply_markup=kb.cancel_reply())
        return
    await state.update_data(price=price)
    await state.set_state(AdminStates.add_plan_days)
    await message.answer("مدت اعتبار به روز را بفرستید:", reply_markup=kb.cancel_reply())


@router.message(AdminStates.add_plan_days)
async def plan_days(message: Message, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await state.clear()
        await message.answer("ادمین نیستید")
        return
    if kb.is_cancel_text(message.text):
        await state.clear()
        await message.answer("لغو شد.", reply_markup=kb.admin_plans_reply_keyboard())
        return
    try:
        days = int(message.text or "30")
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
async def plan_gb(message: Message, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await state.clear()
        await message.answer("ادمین نیستید")
        return
    if kb.is_cancel_text(message.text):
        await state.clear()
        await message.answer("لغو شد.", reply_markup=kb.admin_plans_reply_keyboard())
        return
    try:
        gb = float(message.text or "0")
    except ValueError:
        await message.answer("یک عدد معتبر بفرستید.", reply_markup=kb.cancel_reply())
        return
    await state.update_data(gb=None if gb <= 0 else gb)
    await state.set_state(AdminStates.add_plan_link)
    await message.answer(
        "اتصال پاسارگارد را انتخاب کنید:",
        reply_markup=kb.admin_plans_reply_keyboard(),
    )
    await message.answer(
        "یکی از گزینه‌های زیر را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📋 از تمپلیت", callback_data="adm:plan:new:mode:tpl")],
                [InlineKeyboardButton(text="📁 با گروه (سفارشی)", callback_data="adm:plan:new:mode:grp")],
                [InlineKeyboardButton(text="❌ انصراف", callback_data="adm:plans")],
            ]
        ),
    )


@router.message(AdminStates.add_plan_link)
async def plan_link_cancel(message: Message, state: FSMContext, db_user: BotUser):
    """Allow reply-keyboard انصراف while waiting for inline PG mode pick."""
    if not _is_admin(db_user):
        await state.clear()
        await message.answer("ادمین نیستید")
        return
    if kb.is_cancel_text(message.text):
        await state.clear()
        await message.answer("لغو شد.", reply_markup=kb.admin_plans_reply_keyboard())
        return
    await message.answer(
        "اتصال را از دکمه‌های زیر پیام انتخاب کنید، یا انصراف بزنید.",
        reply_markup=kb.admin_plans_reply_keyboard(),
    )


async def _finish_new_plan(
    session: AsyncSession,
    state: FSMContext,
    *,
    template_id: int | None = None,
    group_ids: str | None = None,
) -> Plan:
    data = await state.get_data()
    await state.clear()
    plan = Plan(
        name=data["name"],
        price=data["price"],
        duration_days=data["days"],
        data_limit_gb=data.get("gb"),
        pg_template_id=template_id,
        pg_group_ids=group_ids,
        is_active=True,
    )
    session.add(plan)
    await session.commit()
    await session.refresh(plan)
    return plan


@router.callback_query(F.data == "adm:plan:new:mode:tpl")
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
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


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
        gid = int(gid)
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
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("adm:plan:picktpl:"))
async def adm_plan_pick_tpl(callback: CallbackQuery, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    plan_id = int(callback.data.split(":")[-1])
    await callback.answer()
    await state.update_data(new_plan=0, selected_groups=[])
    await _show_template_picker(callback, plan_id=plan_id)


@router.callback_query(F.data.startswith("adm:plan:pickgrp:"))
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
    await callback.answer("ذخیره شد")
    if data.get("new_plan") or plan_id == 0:
        plan = await _finish_new_plan(session, state, template_id=tpl_id, group_ids=None)
        if callback.message:
            await callback.message.edit_text(
                f"پلن #{plan.id} با تمپلیت #{tpl_id} ساخته شد ✅\n\n"
                + await _plan_detail_text(plan),
                reply_markup=_plan_detail_keyboard(plan),
            )
        return
    plan = await session.get(Plan, plan_id)
    if not plan:
        return
    plan.pg_template_id = tpl_id
    plan.pg_group_ids = None
    await session.commit()
    await state.clear()
    if callback.message:
        await callback.message.edit_text(
            await _plan_detail_text(plan),
            reply_markup=_plan_detail_keyboard(plan),
        )


@router.callback_query(F.data.startswith("adm:plan:toggrp:"))
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
    await callback.answer("ذخیره شد")
    if data.get("new_plan") or plan_id == 0:
        plan = await _finish_new_plan(session, state, template_id=None, group_ids=group_csv)
        if callback.message:
            await callback.message.edit_text(
                f"پلن #{plan.id} با گروه(ها) {group_csv} ساخته شد ✅\n\n"
                + await _plan_detail_text(plan),
                reply_markup=_plan_detail_keyboard(plan),
            )
        return
    plan = await session.get(Plan, plan_id)
    if not plan:
        return
    plan.pg_template_id = None
    plan.pg_group_ids = group_csv
    await session.commit()
    await state.clear()
    if callback.message:
        await callback.message.edit_text(
            await _plan_detail_text(plan),
            reply_markup=_plan_detail_keyboard(plan),
        )


@router.callback_query(F.data.startswith("adm:plan:clearlink:"))
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
            reply_markup=_plan_detail_keyboard(plan),
        )


@router.callback_query(F.data.startswith("adm:plan:toggle:"))
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
                reply_markup=_plan_detail_keyboard(plan),
            )
            return
        except Exception:
            pass
    await _render_plans_list(callback, session)


@router.callback_query(F.data == "adm:custom")
async def adm_custom(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await callback.answer()
    text, markup = await _custom_link_summary(session)
    if callback.message:
        await callback.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data == "adm:custom:toggle")
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
async def adm_custom_pick_tpl(callback: CallbackQuery, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await callback.answer()
    await _show_custom_template_picker(callback)


@router.callback_query(F.data.startswith("adm:custom:settpl:"))
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
async def adm_users(callback: CallbackQuery, session: AsyncSession, db_user: BotUser, state: FSMContext | None = None):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await callback.answer()
    total = await session.scalar(select(func.count()).select_from(BotUser))
    blocked = await session.scalar(
        select(func.count()).select_from(BotUser).where(BotUser.is_blocked.is_(True))
    ) or 0
    orders = await session.scalar(select(func.count()).select_from(Order))
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
            reply_markup=kb.admin_users_reply_keyboard(),
        )
        if state is not None:
            from app.bot import menu_nav as nav

            await nav.set_nav_level(state, nav.NAV_ADMIN_USERS, push=False)


USERS_PAGE_SIZE = 10


@router.callback_query(F.data.startswith("adm:users:list:"))
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
    total = await session.scalar(select(func.count()).select_from(BotUser)) or 0
    result = await session.execute(
        select(BotUser)
        .order_by(BotUser.id.desc())
        .offset(page * USERS_PAGE_SIZE)
        .limit(USERS_PAGE_SIZE)
    )
    users = list(result.scalars().all())
    rows = []
    for u in users:
        name = (u.full_name or u.username or str(u.telegram_id))[:18]
        flag = "🚫" if u.is_blocked else ("🤝" if u.role == Role.RESELLER.value else "👤")
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
        f"<i>برای جزئیات روی کاربر بزنید.</i>"
    )
    if callback.message:
        await callback.message.edit_text(
            text,
            reply_markup=kb.admin_users_list_keyboard(
                page=page, has_prev=has_prev, has_next=has_next, rows=rows
            ),
        )


@router.callback_query(F.data == "adm:users:webhint")
async def adm_users_webhint(callback: CallbackQuery, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await callback.answer(
        "از وب‌پنل مسیر /users برای ایجاد، ویرایش و مدیریت کامل کاربران استفاده کنید.",
        show_alert=True,
    )


@router.callback_query(F.data == "adm:users:search")
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
async def adm_users_search(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await state.clear()
        await message.answer("ادمین نیستید")
        return
    if kb.is_cancel_text(message.text):
        await state.clear()
        await message.answer("لغو شد.", reply_markup=kb.persistent_reply_keyboard())
        await message.answer("👥 کاربران", reply_markup=kb.admin_users_reply_keyboard())
        return
    try:
        tg_id = int((message.text or "").strip())
    except ValueError:
        await message.answer("آیدی عددی معتبر بفرستید")
        return
    result = await session.execute(select(BotUser).where(BotUser.telegram_id == tg_id))
    user = result.scalar_one_or_none()
    await state.clear()
    from app.bot.tg_utils import seed_persistent_reply_kb

    await seed_persistent_reply_kb(message)
    if not user:
        await message.answer(
            "کاربری با این آیدی یافت نشد.",
            reply_markup=kb.admin_users_reply_keyboard(),
        )
        return
    svc_count = await session.scalar(
        select(func.count()).select_from(UserService).where(UserService.bot_user_id == user.id)
    ) or 0
    blocked = "بله 🚫" if user.is_blocked else "خیر"
    text = (
        f"👤 <b>{user.full_name or user.username or '—'}</b>\n\n"
        f"آیدی تلگرام: <code>{user.telegram_id}</code>\n"
        f"یوزرنیم: @{user.username or '—'}\n"
        f"نقش: {user.role}\n"
        f"کیف پول: {format_toman(user.wallet_balance, get_settings().currency)}\n"
        f"سرویس‌ها: {svc_count}\n"
        f"مسدود: {blocked}"
    )
    await message.answer(
        text,
        reply_markup=kb.admin_user_actions(
            user.id, is_blocked=user.is_blocked, role=user.role
        ),
    )


async def _render_user_card(
    message: Message,
    session: AsyncSession,
    user: BotUser,
    *,
    confirm_delete: bool = False,
    edit: bool = False,
) -> None:
    svc_count = await session.scalar(
        select(func.count()).select_from(UserService).where(UserService.bot_user_id == user.id)
    ) or 0
    blocked = "بله 🚫" if user.is_blocked else "خیر"
    text = (
        f"👤 <b>{user.full_name or user.username or '—'}</b>\n\n"
        f"آیدی تلگرام: <code>{user.telegram_id}</code>\n"
        f"یوزرنیم: @{user.username or '—'}\n"
        f"نقش: {user.role}\n"
        f"کیف پول: {format_toman(user.wallet_balance, get_settings().currency)}\n"
        f"سرویس‌ها: {svc_count}\n"
        f"مسدود: {blocked}"
    )
    if confirm_delete:
        text += "\n\n⚠️ <b>حذف کامل برگشت‌ناپذیر است</b> (سفارش‌ها، سرویس‌ها، تیکت‌ها)."
    markup = kb.admin_user_actions(
        user.id,
        is_blocked=user.is_blocked,
        role=user.role,
        confirm_delete=confirm_delete,
    )
    if edit:
        try:
            await message.edit_text(text, reply_markup=markup)
            return
        except Exception:
            pass
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("adm:users:view:"))
async def adm_users_view(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    user = await session.get(BotUser, int(callback.data.split(":")[-1]))
    if not user:
        await callback.answer("یافت نشد", show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await _render_user_card(callback.message, session, user, edit=True)


@router.callback_query(F.data.startswith("adm:users:block:"))
async def adm_users_block(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    user_id = int(callback.data.split(":")[-1])
    user = await session.get(BotUser, user_id)
    if not user:
        await callback.answer("یافت نشد", show_alert=True)
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
async def adm_users_block_reason(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await state.clear()
        return
    if kb.is_cancel_text(message.text):
        await state.clear()
        await message.answer("لغو شد.", reply_markup=kb.admin_users_reply_keyboard())
        return
    data = await state.get_data()
    user_id = int(data.get("block_user_id") or 0)
    reason = (message.text or "").strip()
    if len(reason) < 3:
        await message.answer("علت خیلی کوتاه است — حداقل ۳ کاراکتر.")
        return
    user = await session.get(BotUser, user_id)
    if not user:
        await state.clear()
        await message.answer("کاربر یافت نشد.")
        return
    from app.services.notifications import notify_account_edit
    from app.services.users import is_protected_admin

    if is_protected_admin(user):
        await state.clear()
        await message.answer("مسدود کردن ادمین مجاز نیست.")
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
    await message.answer(f"🚫 کاربر مسدود شد.\nعلت: {reason}")
    await _render_user_card(message, session, user, edit=False)


@router.callback_query(F.data.startswith("adm:users:delask:"))
async def adm_users_delask(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    user = await session.get(BotUser, int(callback.data.split(":")[-1]))
    if not user:
        await callback.answer("یافت نشد", show_alert=True)
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
async def adm_users_delete(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    user_id = int(callback.data.split(":")[-1])
    from app.services.notifications import notify_account_edit
    from app.services.users import delete_bot_user

    user = await session.get(BotUser, user_id)
    if not user:
        await callback.answer("یافت نشد", show_alert=True)
        return
    await notify_account_edit(
        session,
        user=user,
        event="user_delete",
        reason="حذف از پنل ربات ادمین",
        actor=db_user.username or db_user.full_name or "ادمین ربات",
    )
    try:
        info = await delete_bot_user(
            session,
            user_id,
            actor_user_id=db_user.id,
        )
    except ValueError as e:
        await callback.answer(str(e), show_alert=True)
        return
    except Exception as e:
        await callback.answer(f"خطا: {e}", show_alert=True)
        return
    await callback.answer("حذف شد", show_alert=True)
    if callback.message:
        await callback.message.edit_text(
            f"🗑 کاربر <code>{info.get('telegram_id')}</code> ({info.get('name')}) حذف شد.",
            reply_markup=kb.admin_users_reply_keyboard(),
        )


@router.callback_query(F.data.startswith("adm:users:unres:"))
async def adm_users_unreseller_ask(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    user_id = int(callback.data.split(":")[-1])
    user = await session.get(BotUser, user_id)
    if not user:
        await callback.answer("یافت نشد", show_alert=True)
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
async def adm_users_unreseller_reason(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await state.clear()
        return
    if kb.is_cancel_text(message.text):
        await state.clear()
        await message.answer("لغو شد.", reply_markup=kb.admin_users_reply_keyboard())
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

    from app.services.resellers import notify_reseller_revoked, revoke_reseller

    try:
        info = await revoke_reseller(
            session, user_id, delete_pg_admin=True, reason=reason
        )
    except ValueError as e:
        await message.answer(f"خطا: {e}", reply_markup=kb.admin_users_reply_keyboard())
        return
    except Exception as e:
        await message.answer(f"خطا: {e}", reply_markup=kb.admin_users_reply_keyboard())
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
        reply_markup=kb.admin_users_reply_keyboard(),
    )
    if user:
        await _render_user_card(message, session, user)


@router.callback_query(F.data == "adm:resellers")
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
            reply_markup=kb.admin_resellers_reply_keyboard(),
        )
        if state is not None:
            from app.bot import menu_nav as nav

            await nav.set_nav_level(state, nav.NAV_ADMIN_RESELLERS, push=False)


RESELLERS_PAGE_SIZE = 10


@router.callback_query(F.data.startswith("adm:resellers:list:"))
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
                    callback_data=f"adm:users:view:{u.id}",
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


@router.callback_query(F.data == "adm:resellers:add")
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
            "<code>123456789 15 1</code>\n\n"
            "• عدد اول: آیدی تلگرام\n"
            "• عدد دوم: درصد کمیسیون (پیش‌فرض ۱۰)\n"
            "• عدد سوم: ۱ = اجازه تأیید رسید، ۰ یا خالی = بدون تأیید",
            reply_markup=kb.cancel_reply(),
        )


@router.callback_query(F.data == "adm:resapp:list")
async def adm_resapp_list(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    from app.db.models import ResellerApplicationStatus
    from app.services.resellers import list_applications

    await callback.answer()
    apps = await list_applications(
        session, status=ResellerApplicationStatus.AWAITING_APPROVAL.value, limit=20
    )
    if not apps:
        if callback.message:
            await callback.message.edit_text(
                "درخواست معلقی نیست.",
                reply_markup=None,
            )
        return
    rows = []
    for a in apps:
        u = a.user
        plan = a.plan
        label = f"#{a.id} {(u.full_name or str(u.telegram_id)) if u else '?'} — {(plan.name if plan else '?')}"
        rows.append([InlineKeyboardButton(text=label[:60], callback_data=f"adm:resapp:view:{a.id}")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="adm:resellers")])
    if callback.message:
        await callback.message.edit_text(
            "📋 درخواست‌های منتظر تأیید:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )


@router.callback_query(F.data.startswith("adm:resapp:view:"))
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
    text = (
        f"🤝 درخواست #{app.id}\n"
        f"وضعیت: <b>{app.status}</b>\n"
        f"کاربر: {u.full_name or u.username or u.telegram_id if u else '—'}\n"
        f"تلگرام: <code>{u.telegram_id if u else '—'}</code>\n"
        f"پلن: {plan.name if plan else '—'}\n"
        f"مبلغ: {format_toman(plan.price if plan else 0, get_settings().currency)}"
    )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=kb.reseller_app_review(app.id))


@router.callback_query(F.data.startswith("adm:resapp:ok:"))
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
        await callback.answer(str(e), show_alert=True)
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
        await callback.answer(str(e), show_alert=True)
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
async def make_res(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await state.clear()
        await message.answer("ادمین نیستید")
        return
    if kb.is_cancel_text(message.text):
        await state.clear()
        await message.answer("لغو شد.", reply_markup=kb.admin_reply_keyboard())
        return
    parts = (message.text or "").split()
    try:
        tg_id = int(parts[0])
        commission = int(parts[1]) if len(parts) > 1 else 10
        can_approve = len(parts) > 2 and parts[2] in {"1", "approve=1", "yes", "بله"}
    except ValueError:
        await message.answer(
            "فرمت نامعتبر است.\nمثال: <code>123456789 15 1</code>"
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
            commission_percent=commission,
            can_approve_receipts=can_approve,
            web_permissions=perms,
            bot_permissions=perms,
            create_pg_admin=True,
            panel_base_url=await get_reseller_panel_base_url(session),
        )
    except Exception as e:
        await message.answer(f"خطا: {e}")
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
        reply_markup=kb.admin_reply_keyboard(),
    )


@router.callback_query(F.data == "adm:tickets")
async def adm_tickets(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await callback.answer()
    tickets = await list_open_tickets(session, platform_only=True)
    if not tickets:
        if callback.message:
            await callback.message.edit_text("تیکت بازی نیست.", reply_markup=None)
        return
    rows = [
        [InlineKeyboardButton(text=f"#{t.id} {t.subject[:24]}", callback_data=f"adm:ticket:{t.id}")]
        for t in tickets[:20]
    ]
    if callback.message:
        await callback.message.edit_text(
            "🎫 تیکت‌های باز:\n<i>بازگشت از کیبورد پایین</i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )


@router.callback_query(F.data.startswith("adm:ticket:"))
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
    # Re-validate admin ACL on every reply (role may have changed mid-FSM).
    if not _is_admin(db_user):
        await state.clear()
        await message.answer("ادمین نیستید.")
        return
    if kb.is_cancel_text(message.text):
        await state.clear()
        await message.answer("لغو شد.", reply_markup=kb.admin_reply_keyboard())
        return
    data = await state.get_data()
    ticket = await session.get(Ticket, data.get("ticket_id"))
    if not ticket:
        await state.clear()
        return
    if ticket.reseller_id:
        await state.clear()
        await message.answer("دسترسی به تیکت فروشگاه ندارید.", reply_markup=kb.admin_reply_keyboard())
        return
    ticket_user = await session.get(BotUser, int(ticket.user_id))
    if ticket_user and ticket_user.reseller_id:
        await state.clear()
        await message.answer("دسترسی به تیکت فروشگاه ندارید.", reply_markup=kb.admin_reply_keyboard())
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
    await message.answer("ارسال شد ✅", reply_markup=kb.admin_reply_keyboard())


@router.callback_query(F.data == "adm:broadcast")
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
            reply_markup=kb.admin_broadcast_reply_keyboard(),
        )


@router.callback_query(F.data.startswith("adm:broadcast:aud:"))
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
async def adm_broadcast_send(message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await state.clear()
        return
    if kb.is_cancel_text(message.text):
        await state.clear()
        await message.answer(
            "لغو شد.",
            reply_markup=kb.admin_broadcast_reply_keyboard(),
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
        await message.answer(str(e), reply_markup=kb.admin_reply_keyboard())
        return
    except Exception as e:
        await message.answer(f"خطا: {e}", reply_markup=kb.admin_reply_keyboard())
        return
    await message.answer(
        f"✅ ارسال شد\nموفق: {result['ok']} / {result['total']}\nناموفق: {result['fail']}",
        reply_markup=kb.admin_reply_keyboard(),
    )


@router.callback_query(F.data == "adm:pg")
async def adm_pg(callback: CallbackQuery, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
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
                reply_markup=kb.pg_reply_keyboard(),
            )
        except Exception:
            pass


@router.callback_query(F.data == "adm:pg:group")
async def adm_pg_group_hint(callback: CallbackQuery, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await callback.answer()
    text = (
        "📁 <b>گروه‌های پاسارگارد</b>\n\n"
        "ساخت/ویرایش گروه نیاز به انتخاب اینباند دارد.\n"
        "از وب‌پنل مسیر <code>/pg/groups</code> استفاده کنید.\n\n"
        "مدیریت کاربران VPN از همین ربات: «کاربران VPN»."
    )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=None)


@router.callback_query(F.data == "adm:pg:template")
async def adm_pg_template_hint(callback: CallbackQuery, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await callback.answer()
    text = (
        "📋 <b>تمپلیت‌های پاسارگارد</b>\n\n"
        "ساخت تمپلیت از وب‌پنل مسیر <code>/pg/templates</code>.\n\n"
        "ساخت کاربر از تمپلیت در ربات: پاسارگارد ← ساخت کاربر."
    )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=None)


@router.callback_query(F.data == "adm:pg:stats")
async def pg_stats(callback: CallbackQuery, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await callback.answer()
    try:
        stats = await get_pg().get_system_stats()
    except Exception as e:
        if callback.message:
            await callback.message.edit_text(f"خطا: {e}", reply_markup=None)
        return
    text = "🏠 <b>نمای کلی پاسارگارد</b>\n\n" + format_system_stats(stats)
    if callback.message:
        await callback.message.edit_text(text[:3500], reply_markup=None)


@router.callback_query(F.data == "adm:pg:nodes")
async def pg_nodes(callback: CallbackQuery, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await callback.answer()
    try:
        nodes = await get_pg().get_nodes()
    except Exception as e:
        if callback.message:
            await callback.message.edit_text(f"خطا: {e}", reply_markup=None)
        return
    items = nodes if isinstance(nodes, list) else nodes.get("nodes", nodes.get("items", []))
    lines = ["🕸 <b>نودها</b>\n"]
    rows = []
    for n in items[:20]:
        if not isinstance(n, dict):
            continue
        nid = n.get("id")
        name = n.get("name") or n.get("address") or nid
        status = node_status_fa(n.get("status") or n.get("connection_status"))
        lines.append(f"#{nid} {name} — {status}")
        if nid is not None:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"♻️ اتصال مجدد #{nid}",
                        callback_data=f"adm:pg:recon:{nid}",
                    )
                ]
            )
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="adm:pg")])
    if callback.message:
        await callback.message.edit_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )


@router.callback_query(F.data.startswith("adm:pg:recon:"))
async def pg_recon(callback: CallbackQuery, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    node_id = int(callback.data.split(":")[-1])
    try:
        await get_pg().reconnect_node(node_id)
        await callback.answer("درخواست اتصال مجدد ارسال شد ✅", show_alert=True)
    except Exception as e:
        await callback.answer(str(e), show_alert=True)
