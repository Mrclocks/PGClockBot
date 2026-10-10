from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.config import get_settings
from app.db.models import (
    BotUser,
    Order,
    Payment,
    PaymentStatus,
    Role,
    Ticket,
    TicketStatus,
)
from app.services.home_overview import reseller_shop_summary
from app.services.formatting import format_message, format_toman, order_status_fa
from app.services.reseller_access import load_reseller_actor
from app.services.resellers import (
    create_application,
    format_reseller_plan_apply_detail,
    has_bot_perm,
    list_active_reseller_plans,
    normalize_reseller_billing_mode,
    reseller_billing_mode_label,
    reseller_plan_mode_of,
)
from app.bot.tg_utils import safe_edit_text
from app.services.users import get_all_settings
from app.services.redact import user_safe_error

router = Router(name="reseller")


class ResellerStates(StatesGroup):
    user_message = State()


async def _actor(
    session: AsyncSession,
    db_user: BotUser,
    *,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    return await load_reseller_actor(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )


def _can_start_reseller_apply(db_user: BotUser, owner_id: int | None) -> str | None:
    """Return Persian deny reason, or None if allowed."""
    if owner_id or db_user.role == Role.RESELLER.value:
        return "شما هم‌اکنون نماینده هستید"
    if db_user.role == Role.ADMIN.value:
        return "ادمین نیاز به درخواست ندارد"
    return None


async def _resapply_mode_keyboard(session: AsyncSession, ui: dict) -> InlineKeyboardMarkup:
    from app.services.billing import BILLING_MODE_FIXED, BILLING_MODE_PAYG

    fixed_n = len(await list_active_reseller_plans(session, billing_mode=BILLING_MODE_FIXED))
    payg_n = len(await list_active_reseller_plans(session, billing_mode=BILLING_MODE_PAYG))
    rows = [
        [
            InlineKeyboardButton(
                text=f"📦 ثابت — {fixed_n} پلن",
                callback_data="resapply:mode:fixed",
            )
        ],
        [
            InlineKeyboardButton(
                text=f"⚡ PAYG — {payg_n} پلن",
                callback_data="resapply:mode:payg",
            )
        ],
        [InlineKeyboardButton(text=ui.get("btn_back") or "بازگشت", callback_data="menu:home")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _resapply_plan_list_keyboard(
    plans: list,
    *,
    mode: str,
    ui: dict,
) -> InlineKeyboardMarkup:
    rows = []
    for p in plans:
        price = format_toman(p.price, get_settings().currency) if p.price else "رایگان"
        if reseller_plan_mode_of(p) == "payg":
            rate = int(getattr(p, "price_per_gb", 0) or 0)
            extra = f" · {format_toman(rate, get_settings().currency)}/گیگ" if rate else ""
        else:
            extra = ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{p.name} — {price}{extra}",
                    callback_data=f"resapply:plan:{p.id}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="⬅️ انتخاب نوع پلن", callback_data="resapply:home")]
    )
    rows.append(
        [InlineKeyboardButton(text=ui.get("btn_back") or "بازگشت", callback_data="menu:home")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "res:creds")
async def res_creds(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    """Credentials / deep-link card — available on the main bot for shop owners."""
    from app.services.resellers import format_reseller_access_card, get_reseller_profile

    if is_reseller_bot:
        # On shop bot, send them to the real panel
        owner_id, profile = await _actor(
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        if owner_id and profile:
            await callback.answer()
            if callback.message:
                await safe_edit_text(
                    callback.message,
                    format_message("🤝 پنل نماینده", "دسترسی‌ها با وب‌پنل یکسان است."),
                    reply_markup=None,
                )
            return
    if db_user.role != Role.RESELLER.value:
        await callback.answer("فقط نمایندگان", show_alert=True)
        return
    profile = await get_reseller_profile(session, db_user.id)
    if not profile or not profile.is_active:
        await callback.answer("پروفایل نماینده یافت نشد", show_alert=True)
        return
    await callback.answer()
    text = await format_reseller_access_card(session, profile)
    # URL / renew buttons are allowed (not a shop-panel menu); nav via reply KB
    rows: list[list[InlineKeyboardButton]] = []
    if profile.bot_username:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"باز کردن @{profile.bot_username}",
                    url=f"https://t.me/{profile.bot_username}",
                )
            ]
        )
    try:
        from app.services.pg_admin_subscription import is_subscription_plan

        plan = getattr(profile, "plan", None)
        if plan is not None and is_subscription_plan(plan):
            rows.append(
                [InlineKeyboardButton(text="🔄 تمدید سرویس", callback_data="res:renew")]
            )
    except Exception:
        pass
    if callback.message:
        await safe_edit_text(
            callback.message,
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None,
        )


@router.callback_query(F.data == "res:home")
async def res_home(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    """Option B: open inline manage hub (same as ``nv:res:home``)."""
    from app.bot.handlers.reply_nav import open_reseller_home

    await callback.answer()
    if not callback.message:
        return
    await open_reseller_home(
        callback.message,
        session,
        db_user,
        state,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
        push=False,
    )


@router.callback_query(F.data == "res:dash")
async def res_dash(
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
        await callback.answer("فقط نمایندگان", show_alert=True)
        return
    if not has_bot_perm(profile, "dashboard"):
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    await callback.answer()
    summary = await reseller_shop_summary(
        session, int(owner_id), ticket_statuses=(TicketStatus.OPEN.value, TicketStatus.ANSWERED.value),
    )
    users_n, orders_n = summary["users"], summary["orders"]
    pending_pay, open_tickets = summary["pending"], summary["tickets"]
    text = (
        "🏠 <b>خانه نماینده</b>\n\n"
        f"👥 مشتریان: {users_n}\n"
        f"🛒 سفارش‌ها: {orders_n}\n"
        f"🧾 رسید معلق: {pending_pay}\n"
        f"🎫 تیکت باز: {open_tickets}"
    )
    if callback.message:
        await safe_edit_text(callback.message, text, reply_markup=None)
        mini = kb.miniapp_reseller_keyboard()
        if mini:
            await callback.message.answer(
                "مینی‌اپ نماینده — آمار و میانبر وب‌پنل:",
                reply_markup=mini,
            )


RES_USERS_PAGE = 10


@router.callback_query(F.data.startswith("res:users:"))
async def res_users_list(
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
        await callback.answer("فقط نمایندگان", show_alert=True)
        return
    if not has_bot_perm(profile, "dashboard"):
        await callback.answer("دسترسی ندارید", show_alert=True)
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
            select(func.count())
            .select_from(BotUser)
            .where(scoped_users_where(int(owner_id)))
        )
        or 0
    )
    result = await session.execute(
        select(BotUser)
        .where(scoped_users_where(int(owner_id)))
        .order_by(BotUser.id.desc())
        .offset(page * RES_USERS_PAGE)
        .limit(RES_USERS_PAGE)
    )
    users = list(result.scalars().all())
    by_svc = await load_services_by_user_ids(session, [int(u.id) for u in users])
    buttons: list[InlineKeyboardButton] = []
    for u in users:
        ops = build_user_ops_row(u, by_svc.get(int(u.id), []))
        name = (u.full_name or u.username or str(u.telegram_id))[:16]
        flag = bot_user_alert_flags(ops)
        buttons.append(
            InlineKeyboardButton(
                text=f"{flag} {name}",
                callback_data=f"res:user:{u.id}",
            )
        )
    rows: list[list[InlineKeyboardButton]] = kb.chunk_buttons(buttons, cols=2)
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ قبل", callback_data=f"res:users:{page - 1}"))
    if (page + 1) * RES_USERS_PAGE < total:
        nav.append(InlineKeyboardButton(text="بعد ▶️", callback_data=f"res:users:{page + 1}"))
    if nav:
        rows.append(nav)
    text = (
        f"👥 <b>مشتریان من</b>\n"
        f"صفحه {page + 1} از {max(1, (total + RES_USERS_PAGE - 1) // RES_USERS_PAGE)}"
        f" · {total} نفر\n"
        f"<i>🔔 اعلان · ⏰ انقضا · 📉 حجم — جزئیات در وب‌پنل /users</i>"
    )
    if not users:
        text += "\n\nهنوز مشتری ثبت‌شده‌ای ندارید."
    if callback.message:
        await safe_edit_text(
            callback.message,
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None,
        )


@router.callback_query(F.data.startswith("res:user:"))
async def res_user_view(
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
        await callback.answer("فقط نمایندگان", show_alert=True)
        return
    if not has_bot_perm(profile, "dashboard"):
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    try:
        uid = int((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer("نامعتبر", show_alert=True)
        return
    user = await session.get(BotUser, uid)
    if not user or int(user.reseller_id or 0) != int(owner_id):
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    await callback.answer()
    from app.services.formatting import copyable
    from app.services.users_ops import (
        bot_user_alert_flags,
        build_user_ops_row,
        load_services_by_user_ids,
    )

    from app.services.wallet import get_wallet_balance

    by = await load_services_by_user_ids(session, [int(user.id)])
    ops = build_user_ops_row(user, by.get(int(user.id), []))
    flags = bot_user_alert_flags(ops)
    blocked = "بله" if user.is_blocked else "خیر"
    display = html.escape(user.full_name or user.username or "—")
    alert_line = ""
    if ops.has_alert:
        bits = []
        if ops.expiring:
            bits.append(f"انقضا {ops.expire_text}")
        if ops.low_volume:
            bits.append(f"حجم {ops.volume_text}")
        if bits:
            alert_line = "\n🔔 " + " · ".join(bits)
    shop_bal = await get_wallet_balance(session, user, shop_id=owner_id)
    text = (
        f"{flags} <b>{display}</b>\n\n"
        f"آیدی: {copyable(user.telegram_id)}\n"
        f"یوزرنیم: {copyable('@' + user.username) if user.username else '—'}\n"
        f"کیف پول فروشگاه: {format_toman(shop_bal, get_settings().currency)}\n"
        f"سرویس‌ها: {ops.service_count}\n"
        f"مسدود: {blocked}"
        f"{alert_line}\n\n"
        f"<i>وب‌پنل: /users?uid={user.id}</i>"
    )
    rows_kb: list[list[InlineKeyboardButton]] = []
    if not user.is_blocked:
        rows_kb.append(
            [InlineKeyboardButton(text="✉️ پیام", callback_data=f"res:usermsg:{user.id}")]
        )
    if ops.service_count > 0:
        rows_kb.append(
            [
                InlineKeyboardButton(
                    text="🔄 تمدید سریع", callback_data=f"res:userrenew:{user.id}"
                )
            ]
        )
    markup = InlineKeyboardMarkup(inline_keyboard=rows_kb) if rows_kb else None
    if callback.message:
        await safe_edit_text(callback.message, text, reply_markup=markup)


@router.callback_query(F.data.startswith("res:usermsg:"))
async def res_user_message_start(
    callback: CallbackQuery,
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
        await callback.answer("فقط نمایندگان", show_alert=True)
        return
    if not has_bot_perm(profile, "dashboard"):
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    try:
        uid = int((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer("نامعتبر", show_alert=True)
        return
    user = await session.get(BotUser, uid)
    if not user or int(user.reseller_id or 0) != int(owner_id):
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    await callback.answer()
    await state.set_state(ResellerStates.user_message)
    await state.update_data(msg_user_id=int(user.id))
    if callback.message:
        await callback.message.answer(
            f"✉️ متن پیام برای <b>{html.escape(user.full_name or user.username or str(user.telegram_id))}</b>:",
            reply_markup=kb.cancel_reply(),
        )


@router.message(ResellerStates.user_message)
async def res_user_message_send(
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
    if not has_bot_perm(profile, "dashboard"):
        await state.clear()
        await message.answer("دسترسی ندارید.")
        return
    if kb.is_cancel_text(message.text):
        await state.clear()
        await message.answer("لغو شد.")
        return
    data = await state.get_data()
    uid = int(data.get("msg_user_id") or 0)
    user = await session.get(BotUser, uid) if uid else None
    await state.clear()
    if not user or int(user.reseller_id or 0) != int(owner_id):
        await message.answer("دسترسی ندارید.")
        return
    from app.services.users_quick import send_staff_dm

    try:
        await send_staff_dm(
            session,
            user,
            message.text or "",
            actor=str(db_user.telegram_id or owner_id),
        )
        await session.commit()
        await message.answer("پیام ارسال شد ✅")
    except ValueError as e:
        await message.answer(user_safe_error(e))
    except Exception as e:
        await message.answer(f"خطا: {user_safe_error(e)}")


@router.callback_query(F.data.startswith("res:userrenew:"))
async def res_user_quick_renew(
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
        await callback.answer("فقط نمایندگان", show_alert=True)
        return
    if not has_bot_perm(profile, "dashboard"):
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    try:
        uid = int((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer("نامعتبر", show_alert=True)
        return
    user = await session.get(BotUser, uid)
    if not user or int(user.reseller_id or 0) != int(owner_id):
        await callback.answer("دسترسی ندارید", show_alert=True)
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


@router.callback_query(F.data.startswith("res:reports"))
async def res_reports(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    """Shop-scoped finance report — same metrics as /finance?tab=reports."""
    owner_id, profile = await _actor(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if not owner_id or not profile:
        await callback.answer("فقط نمایندگان", show_alert=True)
        return
    if not (
        has_bot_perm(profile, "orders") or has_bot_perm(profile, "payments")
    ):
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    raw = (callback.data or "res:reports:week").split(":")
    period = raw[-1] if len(raw) >= 3 else "week"
    if period not in {"day", "week", "month"}:
        period = "week"
    await callback.answer()
    from app.services.finance_reports import (
        PERIOD_LABELS_FA,
        build_finance_report,
        format_finance_report_telegram,
    )

    report = await build_finance_report(
        session, reseller_id=int(owner_id), period=period
    )
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
                callback_data=f"res:reports:{key}",
            )
        )
    rows.append(period_row)
    if callback.message:
        await safe_edit_text(
            callback.message,
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )


@router.callback_query(F.data == "res:stats")
async def res_stats(
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
        await callback.answer("فقط نمایندگان", show_alert=True)
        return
    if not has_bot_perm(profile, "stats"):
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    await callback.answer()
    text = (
        "📊 <b>آمار</b>\n\n"
        f"تأیید رسید: {'بله' if has_bot_perm(profile, 'payments') else 'خیر'}\n"
        f"وب‌پنل: <code>{profile.web_username or '—'}</code>\n"
        f"ربات: <code>{('@' + profile.bot_username) if profile.bot_username else '—'}</code>\n"
        f"ادمین PG: <code>{profile.pg_admin_username or '—'}</code>"
    )
    if callback.message:
        await safe_edit_text(callback.message, text, reply_markup=None)


@router.callback_query(F.data == "res:billing")
async def res_billing(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    """PAYG billing wallet — read-only for reseller (top-up is Super Admin only)."""
    from app.services.billing import (
        is_billing_enabled,
        is_payg,
        list_billing_transactions,
        resolve_price_per_gb,
        rate_context_for_profile,
    )

    owner_id, profile = await _actor(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if not owner_id or not profile:
        await callback.answer("فقط نمایندگان", show_alert=True)
        return
    if not is_payg(profile):
        await callback.answer("این فروشگاه PAYG نیست", show_alert=True)
        return
    await callback.answer()
    enabled = await is_billing_enabled(session)
    rate = await resolve_price_per_gb(session, rate_context_for_profile(profile))
    from app.services.billing import ensure_payg_shop_wallet

    owner_user, bal = await ensure_payg_shop_wallet(session, profile)
    await session.commit()
    txs = await list_billing_transactions(session, int(owner_id), limit=5)
    lines = [
        "💰 <b>کیف پول (PAYG)</b>",
        "",
        f"موجودی: <b>{format_toman(int(bal), get_settings().currency)}</b>",
        f"نرخ پلن: {format_toman(rate, get_settings().currency)} / گیگ",
        f"وضعیت سیستم: {'فعال' if enabled else 'غیرفعال'}",
        "",
        "مصرف ترافیک از همین کیف پول (همان موجودی منوی کیف پول) کسر می‌شود.",
        "برای شارژ از منوی کیف پول یا با ادمین اصلی هماهنگ کنید.",
    ]
    if profile.billing_suspended_at is not None:
        lines.insert(3, "⚠️ <b>حساب به‌خاطر موجودی صفر مسدود است</b>")
    if txs:
        lines.append("")
        lines.append("آخرین تراکنش‌ها:")
        for tx in txs:
            sign = "+" if tx.amount >= 0 else ""
            lines.append(
                f"• {tx.kind}: {sign}{format_toman(tx.amount, get_settings().currency)}"
            )
    if callback.message:
        await safe_edit_text(callback.message, "\n".join(lines), reply_markup=None)


@router.callback_query(F.data == "res:orders")
async def res_orders(
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
        await callback.answer("فقط نمایندگان", show_alert=True)
        return
    if not has_bot_perm(profile, "orders"):
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    await callback.answer()
    result = await session.execute(
        select(Order)
        .where(Order.reseller_id == owner_id)
        .order_by(Order.id.desc())
        .limit(15)
    )
    orders = list(result.scalars().all())
    if not orders:
        if callback.message:
            await safe_edit_text(
                callback.message,
                "سفارشی برای مشتریان شما ثبت نشده.",
                reply_markup=None,
            )
        return
    lines = ["🛒 <b>آخرین سفارش‌های مشتریان</b>\n"]
    for o in orders:
        lines.append(
            f"#{o.id} — {format_toman(o.amount, get_settings().currency)} — "
            f"{order_status_fa(o.status)}"
        )
    if callback.message:
        await safe_edit_text(
            callback.message,
            "\n".join(lines),
            reply_markup=None,
        )


@router.callback_query(F.data == "res:tickets")
async def res_tickets(
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
        await callback.answer("فقط نمایندگان", show_alert=True)
        return
    if not has_bot_perm(profile, "tickets"):
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    await callback.answer()
    result = await session.execute(
        select(Ticket, BotUser)
        .join(BotUser, BotUser.id == Ticket.user_id)
        .where(
            Ticket.status.in_([TicketStatus.OPEN.value, TicketStatus.ANSWERED.value]),
            or_(
                Ticket.reseller_id == owner_id,
                (Ticket.reseller_id.is_(None)) & (BotUser.reseller_id == owner_id),
            ),
        )
        .order_by(Ticket.id.desc())
        .limit(15)
    )
    rows = result.all()
    if not rows:
        if callback.message:
            await safe_edit_text(
                callback.message,
                "تیکت بازی از مشتریان نیست.",
                reply_markup=None,
            )
        return
    lines = ["🎫 <b>تیکت‌های مشتریان</b>\n"]
    rows_kb: list[list[InlineKeyboardButton]] = []
    for t, u in rows:
        who = u.full_name or u.username or str(u.telegram_id)
        lines.append(f"#{t.id} — {t.subject[:40]} — {who}")
        rows_kb.append(
            [
                InlineKeyboardButton(
                    text=f"💬 #{t.id}",
                    callback_data=f"tkt:reply:{t.id}",
                ),
                InlineKeyboardButton(
                    text=f"🗃 بستن #{t.id}",
                    callback_data=f"tkt:close:{t.id}",
                ),
            ]
        )
    lines.append("\nروی دکمه پاسخ بزنید یا تیکت را ببندید.")
    if callback.message:
        await safe_edit_text(
            callback.message,
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows_kb) if rows_kb else None,
        )


@router.callback_query(F.data == "res:payments")
async def res_payments(
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
        await callback.answer("فقط نمایندگان", show_alert=True)
        return
    if not has_bot_perm(profile, "payments"):
        await callback.answer("اجازه تأیید ندارید", show_alert=True)
        return
    await callback.answer()
    result = await session.execute(
        select(Payment, BotUser)
        .join(BotUser, BotUser.id == Payment.user_id)
        .where(
            BotUser.reseller_id == owner_id,
            Payment.status == PaymentStatus.PENDING.value,
            Payment.receipt_file_id.is_not(None),
        )
        .order_by(Payment.id.desc())
        .limit(20)
    )
    rows = result.all()
    if not rows:
        if callback.message:
            await safe_edit_text(callback.message, "رسید معلقی نیست.", reply_markup=None)
        return
    for payment, user in rows:
        caption = (
            f"رسید #{payment.id}\nکاربر: {user.full_name}\n"
            f"مبلغ: {format_toman(payment.amount, get_settings().currency)}"
        )
        try:
            if payment.receipt_file_id:
                await callback.bot.send_photo(
                    db_user.telegram_id,
                    photo=payment.receipt_file_id,
                    caption=caption,
                    reply_markup=kb.payment_review(payment.id),
                )
            else:
                await callback.bot.send_message(
                    db_user.telegram_id,
                    caption,
                    reply_markup=kb.payment_review(payment.id),
                )
        except Exception:
            pass
    if callback.message:
        await safe_edit_text(callback.message, "رسیدهای باز ارسال شد.", reply_markup=None)


@router.callback_query(F.data == "resapply:home")
async def resapply_home(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    ui = await get_all_settings(session)
    order_keys = [p.strip() for p in (ui.get("menu_order") or "").split(",") if p.strip()]
    if "reseller_apply" not in order_keys:
        await callback.answer("درخواست نمایندگی در منو فعال نیست", show_alert=True)
        return
    owner_id, _profile = await _actor(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    deny = _can_start_reseller_apply(db_user, owner_id)
    if deny:
        await callback.answer(deny, show_alert=True)
        return
    from app.services.billing import BILLING_MODE_FIXED, BILLING_MODE_PAYG

    fixed_n = len(await list_active_reseller_plans(session, billing_mode=BILLING_MODE_FIXED))
    payg_n = len(await list_active_reseller_plans(session, billing_mode=BILLING_MODE_PAYG))
    await callback.answer()
    if fixed_n == 0 and payg_n == 0:
        if callback.message:
            await safe_edit_text(
                callback.message,
                format_message(
                    "🤝 نمایندگی",
                    "در حال حاضر پلن نمایندگی فعالی تعریف نشده است.\nبعداً دوباره بررسی کنید.",
                ),
                reply_markup=kb.back_home(ui),
            )
        return
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message(
                "🤝 درخواست نمایندگی",
                "ابتدا <b>نوع پلن</b> را انتخاب کنید:\n"
                "• <b>ثابت</b> — اشتراک با قیمت ثابت\n"
                "• <b>PAYG</b> — پرداخت بر اساس مصرف ترافیک\n\n"
                "بعد از انتخاب نوع، پلن‌های همان دسته نمایش داده می‌شود.",
            ),
            reply_markup=await _resapply_mode_keyboard(session, ui),
        )


@router.callback_query(F.data.startswith("resapply:mode:"))
async def resapply_mode(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    owner_id, _profile = await _actor(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    deny = _can_start_reseller_apply(db_user, owner_id)
    if deny:
        await callback.answer(deny, show_alert=True)
        return
    mode = normalize_reseller_billing_mode(callback.data.split(":")[-1])
    ui = await get_all_settings(session)
    plans = await list_active_reseller_plans(session, billing_mode=mode)
    await callback.answer()
    label = reseller_billing_mode_label(mode)
    wallet_hint = ""
    if mode == "payg":
        from app.services.billing import check_payg_purchase_wallet

        gate = await check_payg_purchase_wallet(session, int(db_user.wallet_balance or 0))
        if not gate.ok:
            wallet_hint = "\n\n⚠️ " + gate.message
    if not plans:
        if callback.message:
            await safe_edit_text(
                callback.message,
                format_message(
                    f"🤝 {label}",
                    "برای این نوع، پلن فعالی تعریف نشده است.\nنوع دیگری را انتخاب کنید."
                    + wallet_hint,
                ),
                reply_markup=await _resapply_mode_keyboard(session, ui),
            )
        return
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message(
                f"🤝 پلن‌های {label}",
                "یکی از پلن‌های این دسته را انتخاب کنید." + wallet_hint,
            ),
            reply_markup=await _resapply_plan_list_keyboard(plans, mode=mode, ui=ui),
        )


@router.callback_query(F.data.startswith("resapply:plan:"))
async def resapply_plan(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    owner_id, _profile = await _actor(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    deny = _can_start_reseller_apply(db_user, owner_id)
    if deny:
        await callback.answer(deny, show_alert=True)
        return
    plan_id = int(callback.data.split(":")[-1])
    plans = {p.id: p for p in await list_active_reseller_plans(session)}
    plan = plans.get(plan_id)
    if not plan:
        await callback.answer("پلن یافت نشد", show_alert=True)
        return
    mode = reseller_plan_mode_of(plan)
    body = format_reseller_plan_apply_detail(plan, currency=get_settings().currency)
    if mode == "payg":
        from app.services.billing import check_payg_purchase_wallet

        gate = await check_payg_purchase_wallet(session, int(db_user.wallet_balance or 0))
        if not gate.ok:
            body = f"{body}\n\n⚠️ {gate.message}"
    rows = [
        [InlineKeyboardButton(text="✅ ثبت درخواست", callback_data=f"resapply:buy:{plan.id}")],
        [InlineKeyboardButton(text="⬅️ بازگشت به لیست", callback_data=f"resapply:mode:{mode}")],
        [InlineKeyboardButton(text="🔀 تغییر نوع پلن", callback_data="resapply:home")],
    ]
    await callback.answer()
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message(f"🤝 {plan.name}", body),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )


@router.callback_query(F.data.startswith("resapply:buy:"))
async def resapply_buy(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    owner_id, _profile = await _actor(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    deny = _can_start_reseller_apply(db_user, owner_id)
    if deny:
        await callback.answer(deny, show_alert=True)
        return
    ui = await get_all_settings(session)
    from app.bot.handlers.terms import prompt_terms_if_needed

    if await prompt_terms_if_needed(
        callback,
        session,
        db_user,
        state,
        ui,
        "buy_reseller",
        reseller_owner_id=reseller_owner_id,
    ):
        return
    plan_id = int(callback.data.split(":")[-1])
    plans = {p.id: p for p in await list_active_reseller_plans(session)}
    plan = plans.get(plan_id)
    if not plan:
        await callback.answer("پلن یافت نشد", show_alert=True)
        return
    try:
        app, order = await create_application(session, user=db_user, plan=plan)
    except ValueError as e:
        await callback.answer(user_safe_error(e), show_alert=True)
        return

    await callback.answer()
    mode_label = reseller_billing_mode_label(reseller_plan_mode_of(plan))
    if order is None:
        if callback.message:
            await safe_edit_text(
                callback.message,
                format_message(
                    "✅ درخواست ثبت شد",
                    f"درخواست #{app.id} برای پلن «{plan.name}» ({mode_label}) ثبت شد.\n"
                    "پس از تأیید ادمین، اطلاعات ورود وب‌پنل برایتان ارسال می‌شود.",
                ),
                reply_markup=kb.back_home(ui),
            )
        notify = (
            f"🤝 درخواست نمایندگی جدید #{app.id}\n"
            f"کاربر: {db_user.full_name or db_user.telegram_id}\n"
            f"نوع: {mode_label}\n"
            f"پلن: {plan.name}"
        )
        for aid in get_settings().admin_ids:
            try:
                await callback.bot.send_message(
                    aid,
                    notify,
                    reply_markup=kb.reseller_app_review(app.id),
                )
            except Exception:
                pass
        return

    if not kb.any_checkout_method_enabled(ui):
        if callback.message:
            await safe_edit_text(
                callback.message,
                format_message("⚠️ پرداخت غیرفعال", "روش پرداختی فعال نیست. با پشتیبانی تماس بگیرید."),
                reply_markup=kb.back_home(ui),
            )
        return

    text = format_message(
        f"🧾 سفارش نمایندگی #{order.id}",
        f"نوع: {mode_label}\nپلن: {plan.name}\n"
        f"مبلغ: <b>{format_toman(order.amount, get_settings().currency)}</b>\n\n"
        "روش پرداخت را انتخاب کنید:",
    )
    if callback.message:
        from app.bot.menu_nav import present_order_pay

        try:
            await safe_edit_text(callback.message, text, reply_markup=None)
        except Exception:
            await callback.message.answer(text)
        await present_order_pay(
            callback.message,
            session,
            db_user,
            order.id,
            state=state,
            text="💳 روش پرداخت را از کیبورد پایین انتخاب کنید:",
        )


async def _capacity_context(
    session: AsyncSession,
    db_user: BotUser,
    *,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    from app.services.reseller_access import load_reseller_capacity_actor
    from app.services.reseller_capacity import load_reseller_plan

    # Capacity (renew / extras / addons) is allowed on the platform bot for the
    # shop owner — unlike the full shop panel which stays dedicated-bot only.
    owner_id, profile = await load_reseller_capacity_actor(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if not owner_id or not profile:
        return None, None, None, None

    plan = await load_reseller_plan(session, profile)
    owner = await session.get(BotUser, int(owner_id))
    return owner_id, profile, plan, owner


@router.callback_query(F.data == "res:renew")
async def res_renew(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    from app.services.reseller_capacity import plan_renew_price, renew_reseller_capacity
    from app.services.pg_admin_subscription import (
        compute_renew_invoice,
        format_renew_invoice_html,
        get_subscription,
    )

    owner_id, profile, plan, owner = await _capacity_context(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if not owner_id or not profile or not plan or not owner:
        await callback.answer("فقط نمایندگان", show_alert=True)
        return
    uname = (profile.pg_admin_username or "").strip()
    sub = await get_subscription(session, uname) if uname else None
    invoice = compute_renew_invoice(plan, sub)
    price = int(invoice.get("total") or plan_renew_price(plan))
    rows = [
        [
            InlineKeyboardButton(
                text=f"✅ تمدید — {format_toman(price, get_settings().currency)}",
                callback_data="res:renew:go",
            )
        ],
        [InlineKeyboardButton(text="انصراف", callback_data="menu:home")],
    ]
    detail = format_renew_invoice_html(invoice, currency=get_settings().currency)
    exp = invoice.get("expires_at")
    exp_line = ""
    if exp is not None:
        try:
            exp_line = f"\nانقضای فعلی: <b>{exp.strftime('%Y-%m-%d %H:%M')}</b> UTC"
        except Exception:
            exp_line = ""
    await callback.answer()
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message(
                "🔄 تمدید سرویس نماینده",
                f"پلن: <b>{plan.name}</b>\n"
                f"{detail}{exp_line}\n\n"
                "با تمدید، دسترسی در صورت انقضا باز می‌شود و مصرف ترافیک کاربران ریست می‌شود.\n"
                "هزینه از کیف پول فروشگاهی کسر می‌شود.",
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )


@router.callback_query(F.data == "res:renew:go")
async def res_renew_go(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    from app.services.reseller_capacity import renew_reseller_capacity

    owner_id, profile, plan, owner = await _capacity_context(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if not owner_id or not profile or not plan or not owner:
        await callback.answer("فقط نمایندگان", show_alert=True)
        return
    try:
        result = await renew_reseller_capacity(
            session, user=owner, profile=profile, plan=plan
        )
    except ValueError as e:
        await callback.answer(user_safe_error(e, limit=180), show_alert=True)
        return
    await callback.answer("تمدید شد")
    if callback.message:
        exp = result.get("expires_at")
        exp_line = ""
        if exp is not None:
            try:
                exp_line = f"\nانقضای جدید: <b>{exp.strftime('%Y-%m-%d %H:%M')}</b> UTC"
            except Exception:
                exp_line = ""
        await safe_edit_text(
            callback.message,
            format_message(
                "✅ تمدید انجام شد",
                f"مبلغ: {format_toman(result['amount'], get_settings().currency)}\n"
                f"ریست مصرف کاربران: {result['reset_ok']}"
                + (f" (خطا: {result['reset_err']})" if result.get("reset_err") else "")
                + exp_line,
            ),
            reply_markup=None,
        )


@router.callback_query(F.data == "res:buy_gb")
async def res_buy_gb(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    from app.services.reseller_capacity import plan_allows_buy_extra, plan_extra_gb_price

    owner_id, profile, plan, owner = await _capacity_context(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if not owner_id or not profile or not plan:
        await callback.answer("فقط نمایندگان", show_alert=True)
        return
    if not plan_allows_buy_extra(plan):
        await callback.answer("این پلن خرید حجم اضافه ندارد", show_alert=True)
        return
    price = plan_extra_gb_price(plan)
    rows = []
    for gb in (1, 5, 10, 50):
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{gb} گیگ — {format_toman(price * gb, get_settings().currency)}",
                    callback_data=f"res:buy_gb:{gb}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="انصراف", callback_data="menu:home")])
    await callback.answer()
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message(
                "📦 خرید حجم اضافه",
                f"نرخ: <b>{format_toman(price, get_settings().currency)}</b> / گیگ\n"
                "سقف حجم ادمین پاسارگارد شما افزایش می‌یابد.\n"
                "هزینه از کیف پول فروشگاهی کسر می‌شود.",
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )


@router.callback_query(F.data.startswith("res:buy_gb:"))
async def res_buy_gb_go(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    from app.services.reseller_capacity import buy_extra_gb

    owner_id, profile, plan, owner = await _capacity_context(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if not owner_id or not profile or not plan or not owner:
        await callback.answer("فقط نمایندگان", show_alert=True)
        return
    from app.services.reseller_capacity import ALLOWED_EXTRA_GB

    try:
        gb = int(callback.data.split(":")[-1])
    except ValueError:
        await callback.answer("مقدار نامعتبر", show_alert=True)
        return
    if gb not in ALLOWED_EXTRA_GB:
        await callback.answer("مقدار مجاز نیست", show_alert=True)
        return
    try:
        result = await buy_extra_gb(
            session, user=owner, profile=profile, plan=plan, gb=gb
        )
    except ValueError as e:
        await callback.answer(user_safe_error(e, limit=180), show_alert=True)
        return
    await callback.answer("خرید شد")
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message(
                "✅ حجم اضافه خریداری شد",
                f"مقدار: {result['gb']} گیگ\n"
                f"مبلغ: {format_toman(result['amount'], get_settings().currency)}",
            ),
            reply_markup=None,
        )


@router.callback_query(F.data == "res:buy_users")
async def res_buy_users(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    from app.services.reseller_capacity import plan_allows_buy_extra, plan_extra_user_price

    owner_id, profile, plan, owner = await _capacity_context(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if not owner_id or not profile or not plan:
        await callback.answer("فقط نمایندگان", show_alert=True)
        return
    if not plan_allows_buy_extra(plan):
        await callback.answer("این پلن خرید کاربر اضافه ندارد", show_alert=True)
        return
    price = plan_extra_user_price(plan)
    rows = []
    for n in (1, 5, 10, 20):
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{n} کاربر — {format_toman(price * n, get_settings().currency)}",
                    callback_data=f"res:buy_users:{n}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="انصراف", callback_data="menu:home")])
    await callback.answer()
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message(
                "👤 خرید کاربر اضافه",
                f"نرخ: <b>{format_toman(price, get_settings().currency)}</b> / کاربر\n"
                "سقف تعداد کاربران ادمین شما افزایش می‌یابد.\n"
                "هزینه از کیف پول فروشگاهی کسر می‌شود.",
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )


@router.callback_query(F.data.startswith("res:buy_users:"))
async def res_buy_users_go(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    from app.services.reseller_capacity import buy_extra_users

    owner_id, profile, plan, owner = await _capacity_context(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if not owner_id or not profile or not plan or not owner:
        await callback.answer("فقط نمایندگان", show_alert=True)
        return
    from app.services.reseller_capacity import ALLOWED_EXTRA_USERS

    try:
        n = int(callback.data.split(":")[-1])
    except ValueError:
        await callback.answer("مقدار نامعتبر", show_alert=True)
        return
    if n not in ALLOWED_EXTRA_USERS:
        await callback.answer("مقدار مجاز نیست", show_alert=True)
        return
    try:
        result = await buy_extra_users(
            session, user=owner, profile=profile, plan=plan, count=n
        )
    except ValueError as e:
        await callback.answer(user_safe_error(e, limit=180), show_alert=True)
        return
    await callback.answer("خرید شد")
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message(
                "✅ کاربر اضافه خریداری شد",
                f"تعداد: {result['users']}\n"
                f"سقف جدید: {result['max_users']}\n"
                f"مبلغ: {format_toman(result['amount'], get_settings().currency)}",
            ),
            reply_markup=None,
        )


@router.callback_query(F.data == "res:addons")
async def res_addons(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    """Unified capacity hub: catalog packs + unit buy-extra when allowed."""
    from app.services.pg_admin_subscription import (
        get_or_create_subscription,
        is_subscription_plan,
        list_addon_plans,
    )
    from app.services.reseller_capacity import (
        plan_allows_buy_extra,
        plan_extra_gb_price,
        plan_extra_user_price,
    )

    owner_id, profile, plan, owner = await _capacity_context(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if not owner_id or not profile or not owner:
        await callback.answer("فقط نمایندگان", show_alert=True)
        return
    if plan is None or not is_subscription_plan(plan):
        await callback.answer(
            "ابتدا باید پلن نمایندگی (اشتراک) خریده باشید",
            show_alert=True,
        )
        return
    uname = (profile.pg_admin_username or "").strip()
    if not uname:
        await callback.answer("ادمین پاسارگارد تنظیم نشده", show_alert=True)
        return
    try:
        sub = await get_or_create_subscription(session, uname)
    except Exception:
        sub = None
    if sub is None or str(getattr(sub, "access_status", "") or "") != "active":
        await callback.answer(
            "اشتراک فعال ندارید — ابتدا تمدید کنید",
            show_alert=True,
        )
        return

    rows: list[list[InlineKeyboardButton]] = []
    addons = await list_addon_plans(session)
    for p in addons:
        kind = str(getattr(p, "plan_kind", "") or "")
        if kind == "addon_volume":
            label = (
                f"{p.name} — +{int(p.addon_gb or 0)} گیگ — "
                f"{format_toman(p.price, get_settings().currency)}"
            )
        else:
            label = (
                f"{p.name} — +{int(p.addon_users or 0)} کاربر — "
                f"{format_toman(p.price, get_settings().currency)}"
            )
        rows.append(
            [
                InlineKeyboardButton(
                    text=label[:64], callback_data=f"res:addon:buy:{p.id}"
                )
            ]
        )

    allow_extra = plan_allows_buy_extra(plan)
    if allow_extra:
        gb_price = plan_extra_gb_price(plan)
        user_price = plan_extra_user_price(plan)
        rows.append(
            [
                InlineKeyboardButton(
                    text=(
                        f"📦 حجم واحدی — "
                        f"{format_toman(gb_price, get_settings().currency)}/گیگ"
                    ),
                    callback_data="res:buy_gb",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text=(
                        f"👤 کاربر واحدی — "
                        f"{format_toman(user_price, get_settings().currency)}/کاربر"
                    ),
                    callback_data="res:buy_users",
                )
            ]
        )

    if not rows:
        await callback.answer("بستهٔ اضافه فعالی تعریف نشده", show_alert=True)
        return

    rows.append([InlineKeyboardButton(text="انصراف", callback_data="menu:home")])
    body = (
        "بستهٔ آماده از کاتالوگ، یا خرید واحدی (اگر پلن اجازه دهد).\n"
        "با خرید بستهٔ آماده فقط ظرفیت اضافه می‌شود و تاریخ انقضا عوض نمی‌شود."
    )
    if allow_extra and not addons:
        body = (
            "هنوز بستهٔ آماده‌ای در کاتالوگ نیست — می‌توانید از خرید واحدی استفاده کنید.\n"
            f"نرخ حجم: <b>{format_toman(plan_extra_gb_price(plan), get_settings().currency)}</b> / گیگ\n"
            f"نرخ کاربر: <b>{format_toman(plan_extra_user_price(plan), get_settings().currency)}</b> / کاربر"
        )
    await callback.answer()
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message("📦 بسته‌های حجم / کاربر", body),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )


@router.callback_query(F.data.startswith("res:addon:buy:"))
async def res_addon_buy(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    from app.db.models import ResellerPlan
    from app.services.pg_admin_subscription import (
        apply_addon_plan,
        get_or_create_subscription,
        is_addon_plan,
        is_subscription_plan,
    )

    owner_id, profile, plan, owner = await _capacity_context(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if not owner_id or not profile or not owner:
        await callback.answer("فقط نمایندگان", show_alert=True)
        return
    if plan is None or not is_subscription_plan(plan):
        await callback.answer(
            "ابتدا باید پلن نمایندگی (اشتراک) خریده باشید",
            show_alert=True,
        )
        return
    try:
        plan_id = int(callback.data.split(":")[-1])
    except ValueError:
        await callback.answer("پلن نامعتبر", show_alert=True)
        return
    addon = await session.get(ResellerPlan, plan_id)
    if not addon or not addon.is_active or not is_addon_plan(addon):
        await callback.answer("بسته یافت نشد", show_alert=True)
        return
    uname = (profile.pg_admin_username or "").strip()
    if not uname:
        await callback.answer("ادمین پاسارگارد تنظیم نشده", show_alert=True)
        return
    try:
        sub = await get_or_create_subscription(session, uname)
        result = await apply_addon_plan(
            session, sub=sub, addon_plan=addon, payer=owner, charge_wallet=True
        )
    except ValueError as e:
        await callback.answer(user_safe_error(e, limit=180), show_alert=True)
        return
    await callback.answer("خرید شد")
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message(
                "✅ بسته اعمال شد",
                f"حجم اضافه کل: {result['extra_gb']} گیگ\n"
                f"کاربر اضافه کل: {result['extra_users']}\n"
                f"مبلغ: {format_toman(result['amount'], get_settings().currency)}\n"
                "تاریخ انقضا تغییر نکرد.",
            ),
            reply_markup=None,
        )
