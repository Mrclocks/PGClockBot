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
from app.services.chat_records import keep_sent
from app.services.automation_review import count_review_queue
from app.services.color_tags import tag_mark
from app.services.customer_marks import marks_for, marks_of
from app.bot.handlers.reseller_customer_tools import customer_tool_rows
from app.services.customer_service_lines import service_lines
from app.services.home_overview import reseller_shop_summary
from app.services.formatting import (
    bot_user_panel_label,
    format_message,
    format_number,
    format_toman,
    order_status_fa,
)
from app.services.reseller_access import load_reseller_actor
from app.services.shop_membership import is_shop_customer
from app.services.resellers import (
    create_application,
    format_reseller_plan_apply_detail,
    has_bot_perm,
    list_active_reseller_plans,
    normalize_reseller_billing_mode,
    reseller_billing_mode_label,
    reseller_plan_mode_of,
)
from app.bot.handlers.automation_review import review_button
from app.bot.handlers.cancellation_review import show_card as show_cancellation_card
from app.bot.handlers import stuck_orders
from app.bot.handlers.reseller_package import change_button, renew_button
from app.bot.handlers.subscription_lookup import ASK_CALLBACK
from app.bot.reseller_hub_groups import back_callback_for
from app.bot.tg_utils import safe_edit_text
from app.services.users import get_all_settings
from app.services.redact import user_safe_error
from app.services import bot_feedback_texts as feedback
import logging

logger = logging.getLogger(__name__)

def _user_card_back(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[kb.back_button(None, f"res:user:{int(user_id)}")]])


def _group_back(action: str) -> InlineKeyboardMarkup:
    """«بازگشت» from a shop panel screen to the group it was opened from."""
    return InlineKeyboardMarkup(inline_keyboard=[[kb.back_button(None, back_callback_for(action))]])

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


async def _resapply_home_keyboard(session: AsyncSession, ui: dict) -> InlineKeyboardMarkup:
    """Category hub (like user shop) when linked; else legacy fixed/PAYG modes."""
    from app.services.billing import BILLING_MODE_FIXED, BILLING_MODE_PAYG
    from app.services.button_styles import resolve_category_button_style
    from app.services.plan_categories import (
        reseller_categories_for_menu,
        uncategorized_reseller_plans,
    )

    all_plans = await list_active_reseller_plans(session)
    cats = await reseller_categories_for_menu(session, reseller_plans=all_plans)
    rows: list[list[InlineKeyboardButton]] = []
    if cats:
        for cat in cats:
            rows.append(
                [
                    kb._ikb(
                        kb._category_button_label(cat),
                        callback_data=f"resapply:cat:{int(cat.id)}",
                        style=resolve_category_button_style(ui, cat),
                        icon_custom_emoji_id=getattr(cat, "icon_custom_emoji_id", None),
                    )
                ]
            )
        if uncategorized_reseller_plans(all_plans):
            rows.append(
                [
                    kb._ikb(
                        "سایر",
                        callback_data="resapply:cat:none",
                        style=kb._style(ui, "plan_res_fixed", fallback="primary"),
                    )
                ]
            )
    else:
        fixed_n = len(
            await list_active_reseller_plans(session, billing_mode=BILLING_MODE_FIXED)
        )
        payg_n = len(
            await list_active_reseller_plans(session, billing_mode=BILLING_MODE_PAYG)
        )
        if fixed_n:
            rows.append(
                [
                    kb._ikb(
                        f"ثابت — {fixed_n} پلن",
                        callback_data="resapply:mode:fixed",
                        style=kb._style(ui, "plan_res_fixed", fallback="primary"),
                    )
                ]
            )
        if payg_n:
            rows.append(
                [
                    kb._ikb(
                        f"PAYG — {payg_n} پلن",
                        callback_data="resapply:mode:payg",
                        style=kb._style(ui, "plan_res_payg", fallback="primary"),
                    )
                ]
            )
    rows.append(
        [
            kb._ikb(
                kb._label_or(ui, "btn_back", "بازگشت"),
                callback_data="menu:home",
                ui=ui,
                label_key="btn_back",
                style_id="back",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


# Back-compat alias for tests / older call sites.
async def _resapply_mode_keyboard(session: AsyncSession, ui: dict) -> InlineKeyboardMarkup:
    return await _resapply_home_keyboard(session, ui)


async def _resapply_plan_list_keyboard(
    plans: list,
    *,
    back_callback: str,
    ui: dict,
    categories: dict[int, object] | None = None,
) -> InlineKeyboardMarkup:
    """Plan rows for one category/mode — single Back climbs to category hub."""
    cat_objs = categories or {}
    rows: list[list[InlineKeyboardButton]] = []
    for p in plans:
        price = format_toman(p.price, get_settings().currency) if p.price else "رایگان"
        if reseller_plan_mode_of(p) == "payg":
            rate = int(getattr(p, "price_per_gb", 0) or 0)
            extra = f" · {format_toman(rate, get_settings().currency)}/گیگابایت" if rate else ""
        else:
            extra = ""
        cid = getattr(p, "category_id", None)
        cat_obj = cat_objs.get(int(cid)) if cid is not None else None
        rows.append(
            [
                kb._ikb(
                    f"{p.name} — {price}{extra}",
                    callback_data=f"resapply:plan:{p.id}",
                    style=kb._plan_row_style(
                        ui, p, kind=reseller_plan_mode_of(p), audience="resellers",
                        category=cat_obj,
                    ),
                    icon_custom_emoji_id=kb.category_icon_for(p, cat_objs),
                )
            ]
        )
    # One Back only — same action as the old «بازگشت به پلن‌ها» (hub).
    rows.append(
        [
            kb._ikb(
                kb._label_or(ui, "btn_back", "بازگشت"),
                callback_data=back_callback,
                ui=ui,
                label_key="btn_back",
                style_id="back",
            )
        ]
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
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[kb.back_button(None, "nv:res:home")]]),
                )
            return
    if db_user.role != Role.RESELLER.value:
        await callback.answer(feedback.RESELLERS_ONLY, show_alert=True)
        return
    profile = await get_reseller_profile(session, db_user.id)
    if not profile or not profile.is_active:
        await callback.answer("حساب نمایندگی شما پیدا نشد؛ با پشتیبانی تماس بگیرید", show_alert=True)
        return
    await callback.answer()
    from app.bot.nav_inline import reseller_access_card_keyboard

    text = await format_reseller_access_card(session, profile)
    if callback.message:
        await safe_edit_text(
            callback.message,
            text,
            reply_markup=reseller_access_card_keyboard(
                profile, await get_all_settings(session)
            ),
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
    """Option B: open inline manage hub (same as ``nv:res:home``).

    Full manage panel only on the dedicated shop bot — main bot redirects to
    credentials (shop isolation).
    """
    from app.bot.handlers.reply_nav import open_reseller_home

    # Main bot: never open the shop manage hub — credentials card only.
    if not is_reseller_bot:
        await res_creds(
            callback,
            session,
            db_user,
            is_reseller_bot=False,
            reseller_owner_id=None,
        )
        return
    await callback.answer()
    if not callback.message:
        return
    await open_reseller_home(
        callback.message,
        session,
        db_user,
        state,
        is_reseller_bot=True,
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
        await callback.answer(feedback.RESELLERS_ONLY, show_alert=True)
        return
    if not has_bot_perm(profile, "dashboard"):
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
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
        f"🧾 رسید در انتظار: {pending_pay}\n"
        f"🎫 تیکت باز: {open_tickets}"
    )
    if callback.message:
        await safe_edit_text(callback.message, text, reply_markup=_group_back("res_dash"))


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
        await callback.answer(feedback.RESELLERS_ONLY, show_alert=True)
        return
    if not has_bot_perm(profile, "dashboard"):
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
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
    by_svc = await load_services_by_user_ids(session, [int(u.id) for u in users], shop_id=int(owner_id))
    marks = await marks_for(session, int(owner_id), [int(u.id) for u in users])
    buttons: list[InlineKeyboardButton] = []
    for u in users:
        ops = build_user_ops_row(u, by_svc.get(int(u.id), []))
        name = (u.full_name or u.username or str(u.telegram_id))[:16]
        flag = tag_mark(marks[int(u.id)].tag) + bot_user_alert_flags(ops)
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
    rows.append([InlineKeyboardButton(text="🔎 جستجو", callback_data=ASK_CALLBACK)])
    rows.extend(_group_back("res_users").inline_keyboard)
    text = (
        f"👥 <b>مشتریان من</b>\n"
        f"صفحه {page + 1} از {max(1, (total + RES_USERS_PAGE - 1) // RES_USERS_PAGE)}"
        f" · {total} نفر\n"
        f"<i>🔔 اعلان · ⏰ انقضا · 📉 حجم کم</i>"
    )
    if not users:
        text += "\n\nهنوز مشتری ثبت‌شده‌ای ندارید. لینک ربات فروشگاه را برای مشتری‌ها بفرستید."
    if callback.message:
        await safe_edit_text(
            callback.message,
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
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
        await callback.answer(feedback.RESELLERS_ONLY, show_alert=True)
        return
    if not has_bot_perm(profile, "dashboard"):
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    try:
        uid = int((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    user = await session.get(BotUser, uid)
    if not user or not await is_shop_customer(session, user, int(owner_id)):
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    await callback.answer()
    from app.bot.handlers.reseller_customer_services import (
        may_manage_customer_services,
        services_button,
    )
    from app.services.loyalty import points_balance
    from app.services.staff_cards import format_staff_customer_card
    from app.services.users_ops import (
        bot_user_alert_flags,
        build_user_ops_row,
        load_services_by_user_ids,
    )
    from app.services.wallet import get_wallet_balance

    by = await load_services_by_user_ids(session, [int(user.id)], shop_id=int(owner_id))
    ops = build_user_ops_row(user, by.get(int(user.id), []))
    flags = tag_mark((await marks_of(session, int(owner_id), user)).tag) + bot_user_alert_flags(ops)
    alert_line = ""
    if ops.has_alert:
        bits = []
        if ops.expiring:
            bits.append(f"انقضا {ops.expire_text}")
        if ops.low_volume:
            bits.append(f"حجم {ops.volume_text}")
        if bits:
            alert_line = "🔔 " + " · ".join(bits)
    shop_bal = await get_wallet_balance(session, user, shop_id=owner_id)
    shop_pts = await points_balance(session, user, int(owner_id))
    text = format_staff_customer_card(
        flags=flags,
        display_name=user.full_name or user.username or "—",
        telegram_id=int(user.telegram_id),
        username=user.username,
        wallet_text=format_toman(shop_bal, get_settings().currency),
        points_text=format_number(shop_pts),
        service_count=int(ops.service_count),
        is_blocked=bool(user.is_blocked),
        wallet_label="کیف پول فروشگاه",
        alert_line=alert_line,
        service_summary=service_lines(by.get(int(user.id), [])),
    )
    rows_kb: list[list[InlineKeyboardButton]] = []
    if not user.is_blocked:
        rows_kb.append(
            [InlineKeyboardButton(text="✉️ پیام", callback_data=f"res:usermsg:{user.id}")]
        )
    if ops.service_count > 0 and may_manage_customer_services(profile):
        rows_kb.append([services_button(int(user.id))])
    if ops.service_count > 0:
        rows_kb.append(
            [
                InlineKeyboardButton(
                    text="🔄 تمدید سریع", callback_data=f"res:userrenew:{user.id}"
                )
            ]
        )
    rows_kb.extend(customer_tool_rows(profile, db_user, int(user.id)))
    rows_kb.append([kb.back_button(None, "res:users:0")])
    if callback.message:
        await safe_edit_text(callback.message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows_kb))


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
        await callback.answer(feedback.RESELLERS_ONLY, show_alert=True)
        return
    if not has_bot_perm(profile, "dashboard"):
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    try:
        uid = int((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    user = await session.get(BotUser, uid)
    if not user or not await is_shop_customer(session, user, int(owner_id)):
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    await callback.answer()
    await state.update_data(msg_user_id=int(user.id))
    from app.bot.nav_input import ask_text

    await ask_text(
        callback,
        state,
        prompt=(
            f"✉️ متن پیام برای <b>{html.escape(user.full_name or user.username or str(user.telegram_id))}</b>:"
        ),
        cancel_code="rs_msg",
        return_to=f"res:user:{uid}",
        fsm_state=ResellerStates.user_message,
        edit=True,
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
        await message.answer(feedback.NO_ACCESS_REPLY)
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
    data = await state.get_data()
    uid = int(data.get("msg_user_id") or 0)
    user = await session.get(BotUser, uid) if uid else None
    await state.clear()
    if not user or not await is_shop_customer(session, user, int(owner_id)):
        await message.answer(feedback.NO_ACCESS_REPLY)
        return
    from app.services.users_quick import send_staff_dm

    try:
        await send_staff_dm(
            session,
            user,
            message.text or "",
            shop_id=int(owner_id),
            actor=bot_user_panel_label(db_user),
            sender_tg=int(db_user.telegram_id or 0),
        )
        await session.commit()
        await message.answer(feedback.MESSAGE_SENT, reply_markup=_user_card_back(uid))
    except ValueError as e:
        await message.answer(user_safe_error(e), reply_markup=_user_card_back(uid))
    except Exception as e:
        await message.answer(f"خطا: {user_safe_error(e)}", reply_markup=_user_card_back(uid))


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
        await callback.answer(feedback.RESELLERS_ONLY, show_alert=True)
        return
    if not has_bot_perm(profile, "dashboard"):
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    try:
        uid = int((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    user = await session.get(BotUser, uid)
    if not user or not await is_shop_customer(session, user, int(owner_id)):
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    from app.services.users_quick import quick_renew_user

    try:
        _svc, label = await quick_renew_user(session, user, shop_id=int(owner_id))
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
        await callback.answer(feedback.RESELLERS_ONLY, show_alert=True)
        return
    if not (
        has_bot_perm(profile, "orders") or has_bot_perm(profile, "payments")
    ):
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
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
    rows.extend(_group_back("res_reports").inline_keyboard)
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
        await callback.answer(feedback.RESELLERS_ONLY, show_alert=True)
        return
    if not has_bot_perm(profile, "stats"):
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    await callback.answer()
    text = (
        "📊 <b>آمار</b>\n\n"
        f"تأیید رسید: {'بله' if has_bot_perm(profile, 'payments') else 'خیر'}\n"
        f"وب‌پنل: <code>{profile.web_username or '—'}</code>\n"
        f"ربات: <code>{('@' + profile.bot_username) if profile.bot_username else '—'}</code>\n"
        f"ادمین پاسارگارد: <code>{profile.pg_admin_username or '—'}</code>"
    )
    if callback.message:
        await safe_edit_text(callback.message, text, reply_markup=_group_back("res_stats"))


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
        await callback.answer(feedback.RESELLERS_ONLY, show_alert=True)
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
    currency = get_settings().currency
    lines = [
        "💰 <b>کیف پول (PAYG)</b>",
        "",
        f"موجودی: <b>{format_toman(int(bal), currency)}</b>",
        f"نرخ پلن: {format_toman(rate, currency)} / گیگابایت",
        f"وضعیت سیستم: {'فعال' if enabled else 'غیرفعال'}",
        "",
        "مصرف ترافیک از همین کیف پول (همان موجودی منوی کیف پول) کسر می‌شود.",
        "برای افزایش موجودی از منوی «کیف پول» اقدام کنید یا با ادمین هماهنگ کنید.",
    ]
    if profile.billing_suspended_at is not None:
        lines.insert(3, "⚠️ <b>حساب به‌خاطر موجودی صفر مسدود است</b>")
    try:
        from app.bot.handlers.shop_credit_bot import credit_summary_lines
        from app.services.shop_credit import credit_overview

        overview = await credit_overview(session, int(owner_id), balance=int(bal))
        if overview.usage.limit > 0 or overview.account is not None:
            lines.append("")
            lines.extend(credit_summary_lines(overview, currency=currency))
    except Exception:
        logger.warning("reseller billing credit summary failed shop=%s", owner_id, exc_info=True)
    if txs:
        lines.append("")
        lines.append("آخرین تراکنش‌ها:")
        for tx in txs:
            sign = "+" if tx.amount >= 0 else ""
            lines.append(
                f"• {tx.kind}: {sign}{format_toman(tx.amount, currency)}"
            )
    if callback.message:
        await safe_edit_text(
            callback.message, "\n".join(lines), reply_markup=_group_back("res_billing")
        )


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
        await callback.answer(feedback.RESELLERS_ONLY, show_alert=True)
        return
    if not has_bot_perm(profile, "orders"):
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
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
                "هنوز سفارشی از مشتریان شما ثبت نشده است.",
                reply_markup=_group_back("res_orders"),
            )
        return
    lines = ["🛒 <b>آخرین سفارش‌های مشتریان</b>\n"]
    for o in orders:
        lines.append(
            f"#{o.id} — {format_toman(o.amount, get_settings().currency)} — "
            f"{order_status_fa(o.status)}"
        )
    markup = _group_back("res_orders")
    if has_bot_perm(profile, "payment_void"):
        void_row = [InlineKeyboardButton(text="↩️ ابطال پرداخت تأییدشده", callback_data="payvoid:list")]
        markup = InlineKeyboardMarkup(inline_keyboard=[void_row, *markup.inline_keyboard])
    if await count_review_queue(session, shop_id=int(owner_id)):
        markup = InlineKeyboardMarkup(inline_keyboard=[[review_button()], *markup.inline_keyboard])
    if stuck_count := await stuck_orders.queue_count(session, int(owner_id)):
        markup = InlineKeyboardMarkup(inline_keyboard=[stuck_orders.entry_row(stuck_count), *markup.inline_keyboard])
    if callback.message:
        await safe_edit_text(callback.message, "\n".join(lines), reply_markup=markup)


async def present_reseller_tickets_list(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    *,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
    edit: bool = False,
) -> bool:
    """Render shop ticket list; returns False when actor/ACL fails."""
    from app.bot.nav_inline import with_inline_back
    from app.services.users import get_all_settings

    owner_id, profile = await _actor(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if not owner_id or not profile or not has_bot_perm(profile, "tickets"):
        return False
    result = await session.execute(
        select(Ticket, BotUser)
        .join(BotUser, BotUser.id == Ticket.user_id)
        .where(
            Ticket.status.in_([TicketStatus.OPEN.value, TicketStatus.ANSWERED.value]),
            Ticket.reseller_id == owner_id,
        )
        .order_by(Ticket.id.desc())
        .limit(15)
    )
    rows = result.all()
    ui = await get_all_settings(session)
    if not rows:
        markup = with_inline_back(None, ui, back_callback_for("res_tickets"))
        text = "تیکت بازی از مشتریان ندارید."
    else:
        lines = ["🎫 <b>تیکت‌های مشتریان</b>\n"]
        rows_kb: list[list[InlineKeyboardButton]] = []
        from app.services.formatting import bot_user_ticket_who

        for t, u in rows:
            who = html.escape(bot_user_ticket_who(u, fallback_id=int(t.user_id)))
            lines.append(f"#{t.id} — {html.escape((t.subject or '')[:40])} — {who}")
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
        text = "\n".join(lines)
        markup = with_inline_back(
            InlineKeyboardMarkup(inline_keyboard=rows_kb), ui, back_callback_for("res_tickets")
        )
    if edit:
        await safe_edit_text(message, text, reply_markup=markup)
    else:
        from app.bot.nav_inline import present_inline_only

        await present_inline_only(message, text=text, inline=markup)
    return True


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
        await callback.answer(feedback.RESELLERS_ONLY, show_alert=True)
        return
    if not has_bot_perm(profile, "tickets"):
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    await callback.answer()
    if callback.message:
        ok = await present_reseller_tickets_list(
            callback.message,
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
            edit=True,
        )
        if not ok:
            await callback.answer(feedback.NO_ACCESS, show_alert=True)


@router.callback_query(F.data == "res:cancellations")
async def res_cancellations(
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
        await callback.answer(feedback.RESELLERS_ONLY, show_alert=True)
        return
    if not has_bot_perm(profile, "orders"):
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    await callback.answer()
    from app.bot.nav_inline import with_inline_back
    from app.services.service_cancellations import (
        OPEN_OPERATOR_STATUSES,
        STATUS_LABELS,
        list_cancellation_requests,
    )

    rows, _next = await list_cancellation_requests(session, int(owner_id), limit=40)
    rows = [(r, u) for r, u in rows if r.status in OPEN_OPERATOR_STATUSES][:20]
    ui = await get_all_settings(session)
    if not rows:
        markup = with_inline_back(None, ui, back_callback_for("res_cancellations"))
        if callback.message:
            await safe_edit_text(
                callback.message, feedback.NO_PENDING_CANCELLATION, reply_markup=markup
            )
        return
    lines = ["📝 <b>درخواست‌های لغو</b>\n"]
    kb_rows: list[list[InlineKeyboardButton]] = []
    for row, pg_user in rows:
        st = STATUS_LABELS.get(row.status, row.status)
        who = f" ({html.escape(str(pg_user))})" if pg_user else ""
        lines.append(f"#{row.id} — سرویس {row.service_id}{who} — {st}")
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=f"#{row.id}",
                    callback_data=f"res:cancel:view:{row.id}",
                )
            ]
        )
    lines.append("\n<i>یکی را برای تأیید یا رد انتخاب کنید.</i>")
    markup = with_inline_back(
        InlineKeyboardMarkup(inline_keyboard=kb_rows), ui, back_callback_for("res_cancellations")
    )
    if callback.message:
        await safe_edit_text(callback.message, "\n".join(lines), reply_markup=markup)


@router.callback_query(F.data.regexp(r"^res:cancel:view:\d+$"))
async def res_cancellation_view(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    await show_cancellation_card(
        callback, session, db_user, int((callback.data or "").rsplit(":", 1)[-1]),
        is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id,
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
        await callback.answer(feedback.RESELLERS_ONLY, show_alert=True)
        return
    if not has_bot_perm(profile, "payments"):
        await callback.answer("اجازهٔ تأیید رسید را ندارید", show_alert=True)
        return
    await callback.answer()
    result = await session.execute(
        select(Payment, BotUser)
        .join(BotUser, BotUser.id == Payment.user_id)
        .outerjoin(Order, Order.id == Payment.order_id)
        .where(
            # The payment's own shop: a wallet top-up's purse or the order's shop, not the payer's attribution.
            or_(
                (Payment.is_wallet_topup.is_(True)) & (Payment.wallet_shop_id == owner_id),
                (Payment.is_wallet_topup.is_(False)) & (Order.reseller_id == owner_id),
            ),
            Payment.status == PaymentStatus.PENDING.value,
            Payment.receipt_file_id.is_not(None),
        )
        .order_by(Payment.id.desc())
        .limit(20)
    )
    rows = result.all()
    if not rows:
        if callback.message:
            await safe_edit_text(callback.message, feedback.NO_PENDING_RECEIPT, reply_markup=_group_back("res_payments"))
        return
    for payment, user in rows:
        caption = (
            f"رسید #{payment.id}\nکاربر: {user.full_name}\n"
            f"مبلغ: {format_toman(payment.amount, get_settings().currency)}"
        )
        try:
            if payment.receipt_file_id:
                with keep_sent():  # a notice or review item, never a temporary panel
                    await callback.bot.send_photo(
                        db_user.telegram_id,
                        photo=payment.receipt_file_id,
                        caption=caption,
                        reply_markup=kb.payment_review(payment.id),
                    )
            else:
                with keep_sent():  # a notice or review item, never a temporary panel
                    await callback.bot.send_message(
                        db_user.telegram_id,
                        caption,
                        reply_markup=kb.payment_review(payment.id),
                    )
        except Exception:
            logger.debug("res_payments: ignored failure", exc_info=True)
    if callback.message:
        await safe_edit_text(callback.message, "✅ رسیدهای در انتظار برایتان فرستاده شد. هر کدام را تأیید یا رد کنید.", reply_markup=_group_back("res_payments"))


def _resapply_list_back_for_plan(plan) -> str:
    """Back from plan detail → category list when linked, else billing-mode list."""
    cid = getattr(plan, "category_id", None)
    if cid is not None:
        return f"resapply:cat:{int(cid)}"
    mode = reseller_plan_mode_of(plan)
    return f"resapply:mode:{mode}"


async def _resapply_picker_copy(session: AsyncSession) -> tuple[str, str, bool]:
    """(title, body_html, use_categories) for the apply home step."""
    from app.services.plan_categories import reseller_categories_for_menu

    plans = await list_active_reseller_plans(session)
    cats = await reseller_categories_for_menu(session, reseller_plans=plans)
    if cats:
        return (
            "درخواست نمایندگی",
            feedback.PICK_CATEGORY_BELOW,
            True,
        )
    return (
        "درخواست نمایندگی",
        "ابتدا <b>نوع پلن</b> را انتخاب کنید:\n"
        "• <b>ثابت</b> — اشتراک با قیمت ثابت\n"
        "• <b>PAYG</b> — پرداخت بر اساس مصرف ترافیک",
        False,
    )


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
    all_plans = await list_active_reseller_plans(session)
    await callback.answer()
    if not all_plans:
        if callback.message:
            await safe_edit_text(
                callback.message,
                format_message(
                    "نمایندگی",
                    feedback.NO_RESELLER_PLANS,
                ),
                reply_markup=kb.back_home(ui),
            )
        return
    title, body, _cats = await _resapply_picker_copy(session)
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message(title, body),
            reply_markup=await _resapply_home_keyboard(session, ui),
        )


@router.callback_query(F.data.startswith("resapply:cat:"))
async def resapply_category(
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
    raw = (callback.data or "").split(":")[-1]
    ui = await get_all_settings(session)
    all_plans = await list_active_reseller_plans(session)
    from app.services.plan_categories import (
        list_shop_categories,
        reseller_plans_in_category,
    )
    from app.services.plan_categories import AUDIENCE_RESELLERS

    if raw == "none":
        cat_id = None
        title = "سایر"
        plans = reseller_plans_in_category(all_plans, None)
    else:
        try:
            cat_id = int(raw)
        except ValueError:
            await callback.answer(feedback.CATEGORY_INVALID, show_alert=True)
            return
        cats = await list_shop_categories(
            session, reseller_id=None, active_only=True, audience=AUDIENCE_RESELLERS
        )
        cat = next((c for c in cats if int(c.id) == cat_id), None)
        if not cat:
            await callback.answer(feedback.CATEGORY_NOT_FOUND, show_alert=True)
            return
        title = kb._category_button_label(cat)
        plans = reseller_plans_in_category(all_plans, cat_id)
    if not plans:
        await callback.answer(feedback.NO_PLANS_IN_CATEGORY, show_alert=True)
        return
    cats_all = await list_shop_categories(
        session, reseller_id=None, active_only=True, audience=AUDIENCE_RESELLERS
    )
    cat_map = {int(c.id): c for c in cats_all}
    await callback.answer()
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message(title, "یکی از پلن‌های این دسته را انتخاب کنید."),
            reply_markup=await _resapply_plan_list_keyboard(
                plans,
                back_callback="resapply:home",
                ui=ui,
                categories=cat_map,
            ),
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
                    label,
                    "برای این نوع، پلن فعالی تعریف نشده است.\nنوع دیگری را انتخاب کنید."
                    + wallet_hint,
                ),
                reply_markup=await _resapply_home_keyboard(session, ui),
            )
        return
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message(
                f"پلن‌های {label}",
                "یکی از پلن‌های این دسته را انتخاب کنید." + wallet_hint,
            ),
            reply_markup=await _resapply_plan_list_keyboard(
                plans, back_callback="resapply:home", ui=ui
            ),
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
        await callback.answer("این پلن پیدا نشد", show_alert=True)
        return
    mode = reseller_plan_mode_of(plan)
    body = format_reseller_plan_apply_detail(plan, currency=get_settings().currency)
    if mode == "payg":
        from app.services.billing import check_payg_purchase_wallet

        gate = await check_payg_purchase_wallet(session, int(db_user.wallet_balance or 0))
        if not gate.ok:
            body = f"{body}\n\n⚠️ {gate.message}"
    ui = await get_all_settings(session)
    # Single Back → plan list (category or mode). No redundant «بازگشت به پلن‌ها».
    back_cb = _resapply_list_back_for_plan(plan)
    rows = [
        [
            InlineKeyboardButton(
                text="✅ ثبت درخواست",
                callback_data=f"resapply:buy:{plan.id}",
            )
        ],
        [
            kb._ikb(
                kb._label_or(ui, "btn_back", "بازگشت"),
                callback_data=back_cb,
                ui=ui,
                label_key="btn_back",
                style_id="back",
            )
        ],
    ]
    await callback.answer()
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message(html.escape(plan.name), body),
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
        await callback.answer("این پلن پیدا نشد", show_alert=True)
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
                    f"درخواست #{app.id} برای پلن «{html.escape(plan.name)}» ({mode_label}) ثبت شد.\n"
                    "پس از تأیید ادمین، اطلاعات ورود وب‌پنل برایتان ارسال می‌شود.",
                ),
                reply_markup=kb.back_home(ui),
            )
        notify = (
            f"🤝 درخواست نمایندگی جدید #{app.id}\n"
            f"کاربر: {html.escape(db_user.full_name or str(db_user.telegram_id))}\n"
            f"نوع: {mode_label}\n"
            f"پلن: {html.escape(plan.name)}"
        )
        for aid in get_settings().admin_ids:
            try:
                with keep_sent():  # a notice or review item, never a temporary panel
                    await callback.bot.send_message(
                        aid,
                        notify,
                        reply_markup=kb.reseller_app_review(app.id, user_id=db_user.id),
                    )
            except Exception:
                logger.debug("resapply_buy: ignored failure", exc_info=True)
        return

    if not kb.any_checkout_method_enabled(ui):
        if callback.message:
            await safe_edit_text(
                callback.message,
                format_message("⚠️ پرداخت غیرفعال", "روش پرداختی فعال نیست. با پشتیبانی تماس بگیرید."),
                reply_markup=kb.back_home(ui),
            )
        return

    summary = format_message(
        f"🧾 سفارش نمایندگی #{order.id}",
        f"نوع: {mode_label}\nپلن: {html.escape(plan.name)}\n"
        f"مبلغ: <b>{format_toman(order.amount, get_settings().currency)}</b>",
    )
    if callback.message:
        from app.bot.menu_nav import present_order_pay

        await present_order_pay(
            callback.message,
            session,
            db_user,
            order.id,
            state=state,
            summary=summary,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
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
    from app.services.reseller_capacity import plan_renew_price
    from app.services.pg_admin_subscription import (
        NO_TIME_RENEWAL,
        compute_renew_invoice,
        format_renew_invoice_html,
        get_subscription,
        plan_has_time,
    )
    from app.bot.handlers.reseller_package import order_callback_for
    from app.services.reseller_package import RENEW_EXPLAIN, WALLET_PAY_HINT

    owner_id, profile, plan, owner = await _capacity_context(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if not owner_id or not profile or not plan or not owner:
        await callback.answer(feedback.RESELLERS_ONLY, show_alert=True)
        return
    uname = (profile.pg_admin_username or "").strip()
    sub = await get_subscription(session, uname) if uname else None
    invoice = compute_renew_invoice(plan, sub)
    price = int(invoice.get("total") or plan_renew_price(plan))
    generation = int(sub.renew_generation or 0) if sub else 0
    timed = plan_has_time(plan)
    rows: list[list] = []
    if timed:
        rows.append([renew_button(price, int(plan.id), generation)])
        other = order_callback_for(int(plan.id), generation, is_reseller_bot=is_reseller_bot)
        if other:
            rows.append([InlineKeyboardButton(text="💳 سایر روش‌های پرداخت", callback_data=other)])
    rows.append([change_button()])
    rows.append([kb.back_button(None, back_callback_for("res_renew"))])
    detail = format_renew_invoice_html(invoice, currency=get_settings().currency)
    exp = invoice.get("expires_at")
    exp_line = ""
    if exp is not None:
        try:
            exp_line = f"\nانقضای فعلی: <b>{exp.strftime('%Y-%m-%d %H:%M')}</b> UTC"
        except Exception:
            exp_line = ""
    explain = (RENEW_EXPLAIN + f"\n{WALLET_PAY_HINT}") if timed else NO_TIME_RENEWAL
    await callback.answer()
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message(
                "🔄 تمدید سرویس نماینده",
                f"پلن: <b>{html.escape(plan.name)}</b>\n"
                f"{detail}{exp_line}\n\n"
                + explain,
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
    """A renew button from before pay buttons carried a generation: show the current invoice again."""
    await res_renew(
        callback, session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id,
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
        await callback.answer(feedback.RESELLERS_ONLY, show_alert=True)
        return
    if not plan_allows_buy_extra(plan):
        await callback.answer("این پلن خرید حجم اضافه ندارد", show_alert=True)
        return
    from app.services.reseller_plan_extra_packs import (
        KIND_GB,
        active_amounts_for_plan,
        buyable_packs_for_plan,
        resolve_extra_pack_button_style,
        stair_purchase_button_text,
        telegram_style_kwargs,
    )

    price = plan_extra_gb_price(plan)
    if price <= 0:
        await callback.answer("قیمت هر گیگ برای این پلن صفر است", show_alert=True)
        return
    packs = await buyable_packs_for_plan(session, int(plan.id), KIND_GB)
    currency = get_settings().currency
    rows = []
    if packs:
        for pack in packs:
            gb = int(pack.amount)
            style = resolve_extra_pack_button_style(pack.button_style, plan)
            rows.append(
                [
                    InlineKeyboardButton(
                        text=stair_purchase_button_text(
                            kind=KIND_GB,
                            amount=gb,
                            price_text=format_toman(price * gb, currency),
                        ),
                        callback_data=f"res:buy_gb:id:{int(pack.id)}",
                        **telegram_style_kwargs(style),
                    )
                ]
            )
    else:
        amounts = sorted(await active_amounts_for_plan(session, int(plan.id), KIND_GB))
        if not amounts:
            await callback.answer("پلهٔ حجم فعالی برای این پلن نیست", show_alert=True)
            return
        for gb in amounts:
            style = resolve_extra_pack_button_style(None, plan)
            rows.append(
                [
                    InlineKeyboardButton(
                        text=stair_purchase_button_text(
                            kind=KIND_GB,
                            amount=gb,
                            price_text=format_toman(price * gb, currency),
                        ),
                        callback_data=f"res:buy_gb:{gb}",
                        **telegram_style_kwargs(style),
                    )
                ]
            )
    rows.append([kb.back_button(None, "res:addons")])
    await callback.answer()
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message(
                "📦 حجم اضافه — یک پله انتخاب کنید",
                f"نرخ: <b>{format_toman(price, currency)}</b> / گیگابایت\n"
                "سقف حجم ادمین پاسارگارد شما افزایش می‌یابد.\n"
                "هزینه از کیف پول فروشگاه کسر می‌شود.",
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
    owner_id, profile, plan, owner = await _capacity_context(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if not owner_id or not profile or not plan or not owner:
        await callback.answer(feedback.RESELLERS_ONLY, show_alert=True)
        return
    from app.services.reseller_plan_extra_packs import (
        KIND_GB,
        active_amounts_for_plan,
        get_pack_for_plan,
    )

    parts = (callback.data or "").split(":")
    gb = None
    pack_id = None
    try:
        if len(parts) >= 4 and parts[2] == "id":
            pack_id = int(parts[3])
            pack = await get_pack_for_plan(session, plan_id=int(plan.id), pack_id=pack_id)
            if pack is None or pack.kind != KIND_GB or not pack.is_active:
                raise ValueError("pack")
            gb = int(pack.amount)
        else:
            gb = int(parts[-1])
            if gb not in await active_amounts_for_plan(session, int(plan.id), KIND_GB):
                raise ValueError("amount")
    except (TypeError, ValueError):
        await callback.answer("مقدار مجاز نیست", show_alert=True)
        return
    from app.bot.handlers.reseller_capacity_buy import GB_OK, confirm_view, current_generation, order_callback_for
    from app.services.reseller_capacity_orders import KIND_GB as ORDER_KIND_GB
    from app.services.reseller_capacity import plan_allows_buy_extra, plan_extra_gb_price

    price = plan_extra_gb_price(plan) * gb
    if not plan_allows_buy_extra(plan) or price <= 0:
        await callback.answer("این پلن خرید حجم اضافه ندارد", show_alert=True)
        return
    gen = await current_generation(session, profile)
    ok_cb = f"{GB_OK}id:{pack_id}:{gen}" if pack_id else f"{GB_OK}{gb}:{gen}"
    text, markup = confirm_view(
        title="📦 تأیید خرید حجم اضافه", lines=[f"حجم اضافه: <b>{gb}</b> گیگابایت"], price=price,
        wallet=int(owner.wallet_balance or 0),
        ok_callback=ok_cb, back_callback="res:buy_gb",
        order_callback=order_callback_for(ORDER_KIND_GB, gb, is_reseller_bot=is_reseller_bot),
    )
    await callback.answer()
    if callback.message:
        await safe_edit_text(callback.message, text, reply_markup=markup)


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
        await callback.answer(feedback.RESELLERS_ONLY, show_alert=True)
        return
    if not plan_allows_buy_extra(plan):
        await callback.answer("این پلن خرید کاربر اضافه ندارد", show_alert=True)
        return
    from app.services.reseller_plan_extra_packs import (
        KIND_USERS,
        active_amounts_for_plan,
        buyable_packs_for_plan,
        resolve_extra_pack_button_style,
        stair_purchase_button_text,
        telegram_style_kwargs,
    )

    price = plan_extra_user_price(plan)
    if price <= 0:
        await callback.answer("قیمت هر کاربر برای این پلن صفر است", show_alert=True)
        return
    packs = await buyable_packs_for_plan(session, int(plan.id), KIND_USERS)
    currency = get_settings().currency
    rows = []
    if packs:
        for pack in packs:
            n = int(pack.amount)
            style = resolve_extra_pack_button_style(pack.button_style, plan)
            rows.append(
                [
                    InlineKeyboardButton(
                        text=stair_purchase_button_text(
                            kind=KIND_USERS,
                            amount=n,
                            price_text=format_toman(price * n, currency),
                        ),
                        callback_data=f"res:buy_users:id:{int(pack.id)}",
                        **telegram_style_kwargs(style),
                    )
                ]
            )
    else:
        amounts = sorted(await active_amounts_for_plan(session, int(plan.id), KIND_USERS))
        if not amounts:
            await callback.answer("پلهٔ کاربر فعالی برای این پلن نیست", show_alert=True)
            return
        for n in amounts:
            style = resolve_extra_pack_button_style(None, plan)
            rows.append(
                [
                    InlineKeyboardButton(
                        text=stair_purchase_button_text(
                            kind=KIND_USERS,
                            amount=n,
                            price_text=format_toman(price * n, currency),
                        ),
                        callback_data=f"res:buy_users:{n}",
                        **telegram_style_kwargs(style),
                    )
                ]
            )
    rows.append([kb.back_button(None, "res:addons")])
    await callback.answer()
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message(
                "👥 کاربر اضافه — یک پله انتخاب کنید",
                f"نرخ: <b>{format_toman(price, currency)}</b> / کاربر\n"
                "سقف تعداد کاربران ادمین شما افزایش می‌یابد.\n"
                "هزینه از کیف پول فروشگاه کسر می‌شود.",
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
    owner_id, profile, plan, owner = await _capacity_context(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if not owner_id or not profile or not plan or not owner:
        await callback.answer(feedback.RESELLERS_ONLY, show_alert=True)
        return
    from app.services.reseller_plan_extra_packs import (
        KIND_USERS,
        active_amounts_for_plan,
        get_pack_for_plan,
    )

    parts = (callback.data or "").split(":")
    n = None
    pack_id = None
    try:
        if len(parts) >= 4 and parts[2] == "id":
            pack_id = int(parts[3])
            pack = await get_pack_for_plan(session, plan_id=int(plan.id), pack_id=pack_id)
            if pack is None or pack.kind != KIND_USERS or not pack.is_active:
                raise ValueError("pack")
            n = int(pack.amount)
        else:
            n = int(parts[-1])
            if n not in await active_amounts_for_plan(session, int(plan.id), KIND_USERS):
                raise ValueError("amount")
    except (TypeError, ValueError):
        await callback.answer("مقدار مجاز نیست", show_alert=True)
        return
    from app.bot.handlers.reseller_capacity_buy import USERS_OK, confirm_view, current_generation, order_callback_for
    from app.services.reseller_capacity_orders import KIND_USERS as ORDER_KIND_USERS
    from app.services.reseller_capacity import plan_allows_buy_extra, plan_extra_user_price

    price = plan_extra_user_price(plan) * n
    if not plan_allows_buy_extra(plan) or price <= 0:
        await callback.answer("این پلن خرید کاربر اضافه ندارد", show_alert=True)
        return
    gen = await current_generation(session, profile)
    ok_cb = f"{USERS_OK}id:{pack_id}:{gen}" if pack_id else f"{USERS_OK}{n}:{gen}"
    text, markup = confirm_view(
        title="👤 تأیید خرید کاربر اضافه", lines=[f"کاربر اضافه: <b>{n}</b>"], price=price,
        wallet=int(owner.wallet_balance or 0),
        ok_callback=ok_cb, back_callback="res:buy_users",
        order_callback=order_callback_for(ORDER_KIND_USERS, n, is_reseller_bot=is_reseller_bot),
    )
    await callback.answer()
    if callback.message:
        await safe_edit_text(callback.message, text, reply_markup=markup)


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
        await callback.answer(feedback.RESELLERS_ONLY, show_alert=True)
        return
    if plan is None or not is_subscription_plan(plan):
        await callback.answer(
            "اول یک پلن نمایندگی (اشتراک) بخرید",
            show_alert=True,
        )
        return
    uname = (profile.pg_admin_username or "").strip()
    if not uname:
        await callback.answer(feedback.PG_ADMIN_NOT_SET, show_alert=True)
        return
    try:
        sub = await get_or_create_subscription(session, uname)
    except Exception:
        sub = None
    if sub is None or str(getattr(sub, "access_status", "") or "") != "active":
        await callback.answer(
            "اشتراک فعال ندارید؛ اول آن را تمدید کنید",
            show_alert=True,
        )
        return

    rows: list[list[InlineKeyboardButton]] = []
    currency = get_settings().currency
    # Price packs by the reseller's subscription plan category (strict match).
    sub_cat = int(plan.category_id) if getattr(plan, "category_id", None) else None
    addons = await list_addon_plans(session, category_id=sub_cat)
    for p in addons:
        kind = str(getattr(p, "plan_kind", "") or "")
        if kind == "addon_volume":
            label = (
                f"📦 {p.name} · +{int(p.addon_gb or 0)} گیگ — "
                f"{format_toman(p.price, currency)}"
            )
        else:
            label = (
                f"👥 {p.name} · +{int(p.addon_users or 0)} کاربر — "
                f"{format_toman(p.price, currency)}"
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
        from app.services.reseller_plan_extra_packs import (
            KIND_GB,
            KIND_USERS,
            active_amounts_for_plan,
        )

        gb_price = plan_extra_gb_price(plan)
        user_price = plan_extra_user_price(plan)
        # Category picker: only kinds with a positive unit price and active stairs.
        if gb_price > 0 and await active_amounts_for_plan(session, int(plan.id), KIND_GB):
            rows.append(
                [
                    InlineKeyboardButton(
                        text="📦 حجم اضافه",
                        callback_data="res:buy_gb",
                    )
                ]
            )
        if user_price > 0 and await active_amounts_for_plan(
            session, int(plan.id), KIND_USERS
        ):
            rows.append(
                [
                    InlineKeyboardButton(
                        text="👥 کاربر اضافه",
                        callback_data="res:buy_users",
                    )
                ]
            )

    if not rows:
        await callback.answer("هنوز بستهٔ فعالی تعریف نشده است", show_alert=True)
        return

    rows.append([kb.back_button(None, back_callback_for("res_addon_packs"))])
    body = (
        "نوع ظرفیت را انتخاب کنید.\n"
        "بستهٔ آماده از کاتالوگ فقط ظرفیت را زیاد می‌کند و تاریخ انقضا را عوض نمی‌کند."
    )
    if allow_extra and not addons:
        body = (
            "نوع ظرفیت اضافه را انتخاب کنید؛ سپس پله و قیمت همان نوع را می‌بینید.\n"
            f"نرخ حجم: <b>{format_toman(plan_extra_gb_price(plan), currency)}</b> / گیگابایت\n"
            f"نرخ کاربر: <b>{format_toman(plan_extra_user_price(plan), currency)}</b> / کاربر"
        )
    await callback.answer()
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message("📦 افزایش ظرفیت نمایندگی", body),
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
        await callback.answer(feedback.RESELLERS_ONLY, show_alert=True)
        return
    if plan is None or not is_subscription_plan(plan):
        await callback.answer(
            "اول یک پلن نمایندگی (اشتراک) بخرید",
            show_alert=True,
        )
        return
    try:
        plan_id = int(callback.data.split(":")[-1])
    except ValueError:
        await callback.answer("این پلن پیدا نشد", show_alert=True)
        return
    addon = await session.get(ResellerPlan, plan_id)
    if not addon or not addon.is_active or not is_addon_plan(addon):
        await callback.answer("این بسته پیدا نشد", show_alert=True)
        return
    uname = (profile.pg_admin_username or "").strip()
    if not uname:
        await callback.answer(feedback.PG_ADMIN_NOT_SET, show_alert=True)
        return
    from app.bot.handlers.reseller_capacity_buy import PACK_OK, confirm_view, order_callback_for
    from app.services.reseller_capacity_orders import KIND_PACK
    from app.services.reseller_packs import pack_fits_plan

    if not pack_fits_plan(addon, plan):
        await callback.answer("این بسته برای پلن شما در دسترس نیست", show_alert=True)
        return
    sub = await get_or_create_subscription(session, uname)
    lines = [f"<b>{html.escape(str(addon.name or ''))}</b>"]
    if int(addon.addon_gb or 0):
        lines.append(f"حجم اضافه: {int(addon.addon_gb)} گیگابایت")
    if int(addon.addon_users or 0):
        lines.append(f"کاربر اضافه: {int(addon.addon_users)}")
    lines.append("تاریخ انقضا تغییر نمی‌کند.")
    text, markup = confirm_view(
        title="📦 تأیید خرید بسته", lines=lines, price=int(addon.price or 0),
        wallet=int(owner.wallet_balance or 0),
        ok_callback=f"{PACK_OK}{int(addon.id)}:{int(sub.renew_generation or 0)}", back_callback="res:addons",
        order_callback=order_callback_for(KIND_PACK, int(addon.id), is_reseller_bot=is_reseller_bot),
    )
    await callback.answer()
    if callback.message:
        await safe_edit_text(callback.message, text, reply_markup=markup)
