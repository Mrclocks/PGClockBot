from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.auth import is_platform_admin as _is_admin
from app.bot.handlers.admin_broadcast_confirm import preview_broadcast
from app.bot.handlers.admin_states import AdminStates
from app.bot.handlers.admin_plan_editor import register_admin_plan_editor
from app.bot.message_button_picker import ask_for_buttons, register_flow
from app.bot.auth import (
    platform_can_manage_representatives,
    require_bot_owner,
    require_bot_owner_handler,
    require_platform_rep_mgmt,
)
from app.config import get_settings
from app.db.models import BotUser, Order, OrderStatus, Payment, PaymentStatus, Plan, Role, Ticket, UserService
from app.services.color_tags import risk_mark
from app.services.home_overview import bot_dashboard_summary
from app.services.formatting import (
    bot_user_panel_label,
    format_number,
    format_system_stats,
    format_toman,
    order_status_fa,
)
from app.services.staff_cards import (
    format_adjust_prompt,
    SERVICE_BUTTON_LIMIT,
    format_services_list,
    format_services_list_header,
    format_staff_customer_card,
)
from app.services.orders import (
    approve_payment,
    fulfill_paid_order,
    reject_payment,
)
from app.services.automation_review import count_review_queue
from app.services.payment_voids import offers_void
from app.services.chat_records import keep_sent
from app.services.db_safe import rollback_quiet
from app.services.broadcast_audience import describe as describe_audience, make_spec as make_audience
from app.services.redact import user_safe_error
from app.services.subscription_lookup import link_token
from app.services.reseller_wallet import load_reseller_profile
from app.services.pasarguard import get_pg
from app.services.tickets import get_ticket, reply_ticket
from app.services.updates import local_version
from app.services.user_search import clean_handle, find_bot_user
from app.services.users import get_all_settings
from app.services.billing import is_payg
from app.services.bot_user_admin import (
    ServiceSnapshot,
    admin_extend_service,
    get_owned_service,
    service_snapshot,
    snapshot_telegram_lines,
)
from app.services.support_button import support_markup
from app.services.volume_allocation import set_shop_oversell
from app.bot.handlers.action_reason import ask_reason, take_reason
from app.services.action_reasons import bot_actor
from app.services.customer_action_notices import NoticeTarget, log_and_notify
from app.bot.handlers.automation_review import review_button
from app.bot.handlers.cancellation_review import show_card as show_cancellation_card
from app.bot.handlers import stuck_orders
from app.bot.handlers.payment_voids import void_button
from app.bot.handlers.make_reseller_plan import ask_reseller_plan
from app.bot.handlers.subscription_lookup import (
    RESELLER_SEARCH_PROMPT,
    SEARCH_PROMPT,
    delete_typed_link,
    staff_lookup,
    staff_reseller_search,
    staff_user_search,
)
from app.bot.nav_inline import with_inline_back
from app.bot.service_adjust import (
    NOTHING_STAGED,
    UNKNOWN_ADJUST_OUTCOME,
    StagedAdjustment,
    read_staged,
    restore_staged,
    service_adjust_lock,
    take_staged,
)
from app.bot.nav_input import finish_text_step
from app.bot.tg_utils import parse_bot_float, parse_bot_int, safe_edit_text
from app.services import bot_feedback_texts as feedback

logger = logging.getLogger(__name__)

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
        is_reseller_bot=False,
        reseller_owner_id=None,
    )


async def _present_admin_tickets_list(
    message: Message,
    session: AsyncSession,
    *,
    edit: bool = False,
) -> None:
    """Render open platform tickets as an inline panel (list + Back)."""
    from app.bot.nav_inline import present_inline_only, with_inline_back
    from app.services.formatting import bot_user_ticket_who
    from app.services.tickets import list_open_tickets as _list_open

    tickets = await _list_open(session, platform_only=True)
    ui = await get_all_settings(session)
    if not tickets:
        markup = with_inline_back(None, ui, "nv:adm:ops")
        text = "تیکت بازی وجود ندارد."
    else:
        user_ids = {int(t.user_id) for t in tickets[:20]}
        users = {
            int(u.id): u
            for u in (
                await session.execute(select(BotUser).where(BotUser.id.in_(user_ids or [0])))
            ).scalars().all()
        }
        lines = ["🎫 <b>تیکت‌های باز</b>\n"]
        rows = []
        for t in tickets[:20]:
            who = html.escape(bot_user_ticket_who(users.get(int(t.user_id)), fallback_id=int(t.user_id)))
            subj = html.escape((t.subject or "")[:40])
            lines.append(f"#{t.id} — {subj} — {who}")
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"#{t.id} { (t.subject or '')[:20] }",
                        callback_data=f"adm:ticket:{t.id}",
                    )
                ]
            )
        markup = with_inline_back(
            InlineKeyboardMarkup(inline_keyboard=rows), ui, "nv:adm:ops"
        )
        text = "\n".join(lines)
    if edit:
        try:
            await message.edit_text(text, reply_markup=markup)
            return
        except Exception:
            logger.debug("_present_admin_tickets_list: ignored failure", exc_info=True)
    await present_inline_only(message, text=text, inline=markup)


async def _answer_tickets_nav(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    text: str,
    state: FSMContext | None = None,
    *,
    clear_state: bool = False,
) -> None:
    """Cancel/finish ticket reply: heal main KB + resend tickets list at bottom."""
    from app.bot.nav_chrome import answer_staff_nav

    async def _reopen(msg, sess, user, st, **_kw):
        await _present_admin_tickets_list(msg, sess, edit=False)

    await answer_staff_nav(
        message,
        session,
        db_user,
        text=text,
        classic=None,
        state=state,
        reopen_panel=_reopen,
        clear_state=clear_state,
        is_reseller_bot=False,
        reseller_owner_id=None,
    )


def _admin_ticket_actions_markup(
    ticket_id: int,
    ui: dict | None = None,
    *,
    user_id: int | None = None,
) -> InlineKeyboardMarkup:
    from app.bot.keyboards import _ikb
    from app.bot.nav_inline import with_inline_back

    rows: list[list] = []
    if user_id:
        rows.append(
            [
                _ikb(
                    "👤 کاربر",
                    callback_data=f"adm:users:view:{int(user_id)}",
                    ui=ui,
                    style_fallback="primary",
                )
            ]
        )
    rows.append(
        [
            _ikb(
                "💬 پاسخ",
                callback_data=f"adm:ticket:reply:{ticket_id}",
                ui=ui,
                style_id="confirm", style_fallback="success",
            ),
            _ikb(
                "🗃 بستن",
                callback_data=f"adm:ticket:close:{ticket_id}",
                ui=ui,
                style_id="reject", style_fallback="danger",
            ),
        ]
    )
    return with_inline_back(
        InlineKeyboardMarkup(inline_keyboard=rows), ui, "adm:tickets"
    )

# The reseller applications list the review screens back up to.
RESELLER_APPS_LIST = "adm:resapp:list"

router = Router(name="admin")

@router.callback_query(F.data == "adm:home")
@require_bot_owner_handler
async def adm_home(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    await callback.answer()
    if callback.message:
        from app.bot.handlers.reply_nav import open_admin_home

        await open_admin_home(
            callback.message,
            session,
            db_user,
            state,
            push=False,
            is_reseller_bot=False,
        )

@router.callback_query(F.data == "adm:dash")
@require_bot_owner_handler
async def adm_dash(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
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
        f"⏳ در انتظار تأیید: <b>{pending_orders}</b>\n"
        f"🧾 رسید در انتظار بررسی: <b>{pending_pay}</b>\n"
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
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
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
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    await callback.answer()
    from app.services.delivery_failures import list_stuck_paid_orders

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
            await callback.message.edit_text("هنوز سفارشی ثبت نشده است.", reply_markup=markup)
        return
    rows = []
    for o in merged:
        prefix = "⚠ " if int(o.id) in stuck_ids else ""
        label = f"{prefix}#{o.id} · {order_status_fa(o.status)} · {format_toman(o.amount, get_settings().currency)}"
        rows.append([InlineKeyboardButton(text=label[:64], callback_data=f"adm:order:{o.id}")])
    if await count_review_queue(session, shop_id=None):
        rows.append([review_button()])
    if stuck_count := await stuck_orders.queue_count(session, None):
        rows.append(stuck_orders.entry_row(stuck_count))
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
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    order_id = int(callback.data.split(":")[-1])
    order = await session.get(Order, order_id)
    if not order:
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
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
    if pay is not None and offers_void(pay, order):
        rows.append([void_button(pay.id)])
    rows.append([kb.back_button(ui, "adm:orders")])
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
                from app.services.delivery_failures import note_delivery_send_failure

                await note_delivery_send_failure(
                    session,
                    order=delivered or order,
                    payment=pay,
                    error=str(send_exc),
                )
            except Exception:
                logger.debug("_approve_order_bot: ignored failure", exc_info=True)
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
                    from app.services.delivery_failures import note_delivery_send_failure

                    await note_delivery_send_failure(
                        session,
                        order=delivered or order,
                        payment=pay,
                        error=str(send_exc),
                    )
                except Exception:
                    logger.debug("_approve_order_bot: ignored failure", exc_info=True)
        msg = "سفارش تحویل شد"
    else:
        raise ValueError("این سفارش هنوز رسید پرداخت ندارد و قابل تأیید نیست")

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
        logger.debug("_approve_order_bot: ignored failure", exc_info=True)
    return msg

@router.callback_query(F.data.startswith("ordrev:ok:"))
@require_bot_owner_handler
async def order_approve_cb(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    order_id = int(callback.data.split(":")[-1])
    order = await session.get(Order, order_id)
    if not order:
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
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
            review = kb.order_review(order.id) if order.status != OrderStatus.DELIVERED.value else None
            await callback.message.edit_text(text, reply_markup=with_inline_back(review, None, "adm:orders"))
        except Exception:
            logger.debug("order_approve_cb: ignored failure", exc_info=True)

@router.callback_query(F.data.startswith("ordrev:no:"))
@require_bot_owner_handler
async def order_reject_cb(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    order_id = int(callback.data.split(":")[-1])
    order = await session.get(Order, order_id)
    if not order:
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
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

                with keep_sent():  # a notice or review item, never a temporary panel
                    await callback.bot.send_message(
                        user.telegram_id,
                        format_message("❌ سفارش رد شد", f"سفارش #{order_id} رد شد."),
                        parse_mode="HTML",
                        reply_markup=support_markup(),
                    )
            except Exception:
                logger.debug("order_reject_cb: ignored failure", exc_info=True)
    else:
        order.status = OrderStatus.REJECTED.value
        await session.commit()
    await callback.answer(feedback.REJECTED, show_alert=True)
    if callback.message:
        try:
            await callback.message.edit_text(
                f"🛒 سفارش #{order_id}\nوضعیت: <b>ردشده</b>",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[kb.back_button(None, "adm:orders")]]
                ),
            )
        except Exception:
            logger.debug("order_reject_cb: ignored failure", exc_info=True)

@router.callback_query(F.data == "adm:cancellations")
@require_bot_owner_handler
async def adm_cancellations(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    """Platform cancel inbox — list open requests; each card approves or rejects."""
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    await callback.answer()
    from app.bot.nav_inline import with_inline_back
    from app.services.service_cancellations import (
        OPEN_OPERATOR_STATUSES,
        STATUS_LABELS,
        list_cancellation_requests,
    )

    rows, _next = await list_cancellation_requests(session, None, limit=40)
    rows = [(r, u) for r, u in rows if r.status in OPEN_OPERATOR_STATUSES][:20]
    ui = await get_all_settings(session)
    if not rows:
        markup = with_inline_back(None, ui, "nv:adm:home")
        if callback.message:
            await callback.message.edit_text(feedback.NO_PENDING_CANCELLATION, reply_markup=markup)
        return
    lines = ["📝 <b>درخواست‌های لغو</b>\n"]
    kb_rows: list[list[InlineKeyboardButton]] = []
    for row, pg_user in rows:
        st = STATUS_LABELS.get(row.status, row.status)
        lines.append(
            f"#{row.id} — سرویس {row.service_id}"
            f"{f' ({html.escape(pg_user)})' if pg_user else ''} — {st}"
        )
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=f"#{row.id}",
                    callback_data=f"adm:cancel:view:{row.id}",
                )
            ]
        )
    lines.append("\n<i>یکی را برای تأیید یا رد انتخاب کنید.</i>")
    markup = with_inline_back(
        InlineKeyboardMarkup(inline_keyboard=kb_rows), ui, "nv:adm:home"
    )
    if callback.message:
        await callback.message.edit_text("\n".join(lines), reply_markup=markup)


@router.callback_query(F.data.regexp(r"^adm:cancel:view:\d+$"))
@require_bot_owner_handler
async def adm_cancellation_view(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    await show_cancellation_card(callback, session, db_user, int((callback.data or "").rsplit(":", 1)[-1]))


@router.callback_query(F.data == "adm:payments")
@require_bot_owner_handler
async def adm_payments(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    await callback.answer()
    from app.services.action_center import payment_shop_clause

    result = await session.execute(
        select(Payment)
        .outerjoin(Order, Order.id == Payment.order_id)
        .where(
            Payment.status == PaymentStatus.PENDING.value,
            Payment.receipt_file_id.is_not(None),
            payment_shop_clause(None),
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
            await callback.message.edit_text(feedback.NO_PENDING_RECEIPT, reply_markup=back)
        return
    for p in payments:
        caption = f"پرداخت #{p.id} — {format_toman(p.amount, get_settings().currency)}"
        try:
            with keep_sent():  # a notice or review item, never a temporary panel
                await callback.bot.send_photo(
                    db_user.telegram_id,
                    photo=p.receipt_file_id,
                    caption=caption,
                    reply_markup=kb.payment_review(p.id),
                )
        except Exception:
            with keep_sent():  # a notice or review item, never a temporary panel
                await callback.bot.send_message(
                    db_user.telegram_id, caption, reply_markup=kb.payment_review(p.id)
                )
    if callback.message:
        await callback.message.edit_text("رسیدها ارسال شد.", reply_markup=back)

register_admin_plan_editor(router)


@router.callback_query(F.data == "adm:users")
@require_bot_owner_handler
async def adm_users(callback: CallbackQuery, session: AsyncSession, db_user: BotUser, state: FSMContext | None = None):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    await callback.answer()
    if callback.message:
        from app.bot.handlers.reply_nav import open_admin_users_hub

        await open_admin_users_hub(
            callback.message,
            session,
            db_user,
            state,
            push=False,
            is_reseller_bot=False,
        )

USERS_PAGE_SIZE = 10
MAX_SEARCH_LENGTH = 64

STEP_EXPIRED_TEXT = "زمان این مرحله تمام شد. دوباره از کارت کاربر اقدام کنید."
STAFF_DM_UNDELIVERED = "⚠️ پیام به کاربر نرسید؛ احتمالاً ربات را مسدود کرده یا هنوز آن را شروع نکرده است."

# Which card the staff DM prompt returns to (also the callback-data namespace).
MSG_CARD_USER = "users"
ADMIN_DM_FLOW = "admin_dm"
MSG_CARD_RESELLER = "resellers"

def _deny_if_outside_platform_shop(user: BotUser | None) -> str | None:
    """Owner admin tools are platform-shop only (reseller_id IS NULL)."""
    if user is None:
        return feedback.NOT_FOUND
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
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
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
    by_svc = await load_services_by_user_ids(session, [int(u.id) for u in users], shop_id=None)
    rows = []
    for u in users:
        ops = build_user_ops_row(u, by_svc.get(int(u.id), []))
        name = (u.full_name or u.username or str(u.telegram_id))[:16]
        flag = risk_mark(u, shop_id=None) + bot_user_alert_flags(ops)
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
        f"<i>🔔 اعلان · ⏰ انقضا · 📉 حجم کم</i>"
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
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    await callback.answer(
        "وب‌پنل /users: فیلتر نزدیک‌انقضا، حجم‌کم، اعلان‌ها و ویرایش کامل کاربران.",
        show_alert=True,
    )

@router.callback_query(F.data == "adm:users:search")
@require_bot_owner_handler
async def adm_users_search_start(callback: CallbackQuery, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    await callback.answer()
    from app.bot.nav_input import ask_text

    await ask_text(
        callback,
        state,
        prompt=SEARCH_PROMPT,
        cancel_code="adm_usr",
        return_to="adm:users",
        fsm_state=AdminStates.user_search,
        edit=True,
    )

@router.message(AdminStates.user_search)
@require_bot_owner_handler
async def adm_users_search(
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
        await _answer_users_nav(
            message,
            session,
            db_user,
            feedback.CANCELLED,
            state,
            clear_state=True,
        )
        return
    if link_token(message.text):
        await _answer_link_search(message, state, session)
        return
    query = clean_handle(message.text)
    if not query or len(query) > MAX_SEARCH_LENGTH:
        await message.answer("نام، آیدی عددی، یوزرنیم تلگرام یا لینک اشتراک را بفرستید؛ مثلاً <code>123456789</code> یا <code>@username</code>.")
        return
    user = await find_bot_user(session, query, scope=BotUser.reseller_id.is_(None))
    if user is None:
        # No exact id or @username: search names and usernames by part.
        text, markup = await staff_user_search(session, query, shop_id=None)
    else:
        text, markup = await _user_card(session, user)
    await _show_search_result(message, state, text, markup)

async def _show_search_result(message: Message, state: FSMContext, text: str, markup: InlineKeyboardMarkup) -> None:
    """The search prompt turns into the result, like every other inline step; the main keyboard stays."""
    await state.set_state(None)
    await finish_text_step(message, state, text=text, inline=markup)


async def _show_outcome(message: Message, state: FSMContext, text: str, markup: InlineKeyboardMarkup) -> None:
    """A typed step's outcome replaces its prompt: one screen, never an extra message."""
    await finish_text_step(message, state, text=text, inline=markup)
    await state.clear()


async def _outcome_on_card(
    message: Message, state: FSMContext, session: AsyncSession, user: BotUser, status: str, *, card: str = MSG_CARD_USER
) -> None:
    """The outcome line on top of the person's card, in place of the prompt."""
    build = _reseller_card if card == MSG_CARD_RESELLER else _user_card
    text, markup = await build(session, user)
    await _show_outcome(message, state, f"{status}\n\n{text}", markup)


async def _outcome_back_to_users(message: Message, state: FSMContext, session: AsyncSession, status: str) -> None:
    """An outcome with no card to show (the person is gone or out of scope): a way back only."""
    markup = with_inline_back(None, await get_all_settings(session), "adm:users")
    await _show_outcome(message, state, status, markup)

async def _answer_link_search(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """Platform users search given a subscription link: owner summary, scoped to platform services."""
    text, markup = await staff_lookup(session, message.text, shop_id=None)
    await _show_search_result(message, state, text, markup)
    await delete_typed_link(message)

async def _render_user_card(
    message: Message,
    session: AsyncSession,
    user: BotUser,
    *,
    confirm_delete: bool = False,
    edit: bool = False,
) -> None:
    text, markup = await _user_card(session, user, confirm_delete=confirm_delete)
    if edit:
        try:
            await message.edit_text(text, reply_markup=markup)
            return
        except TelegramAPIError:
            # Too old or already gone: show the card as a new message instead.
            logger.debug("user card edit failed; sending it anew", exc_info=True)
    await message.answer(text, reply_markup=markup)

async def _user_card(
    session: AsyncSession, user: BotUser, *, confirm_delete: bool = False
) -> tuple[str, InlineKeyboardMarkup]:
    """The platform customer card from stored data only (no panel call) and its actions."""
    from app.services.users_ops import (
        bot_user_alert_flags,
        build_user_ops_row,
        load_services_by_user_ids,
    )

    by = await load_services_by_user_ids(session, [int(user.id)], shop_id=None)
    ops = build_user_ops_row(user, by.get(int(user.id), []))
    flags = risk_mark(user, shop_id=None) + bot_user_alert_flags(ops)
    alert_line = ""
    if ops.has_alert:
        bits = []
        if ops.expiring:
            bits.append(f"انقضا {ops.expire_text}")
        if ops.low_volume:
            bits.append(f"حجم {ops.volume_text}")
        alert_line = "🔔 اعلان: " + " · ".join(bits) if bits else "🔔 اعلان فعال"
    from app.services.color_tags import bot_tag_button_label, effective_color_tag
    from app.services.loyalty import points_balance

    tag_line = bot_tag_button_label(effective_color_tag(user))
    pts = await points_balance(session, user, None)
    text = format_staff_customer_card(
        flags=flags,
        display_name=user.full_name or user.username or "—",
        telegram_id=int(user.telegram_id),
        username=user.username,
        wallet_text=format_toman(user.wallet_balance, get_settings().currency),
        points_text=format_number(pts),
        service_count=int(ops.service_count),
        is_blocked=bool(user.is_blocked),
        role=user.role,
        risk_tag=tag_line,
        alert_line=alert_line.strip(),
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
    return text, markup

async def _ask_staff_message(
    callback: CallbackQuery,
    state: FSMContext,
    user: BotUser,
    *,
    card: str,
) -> None:
    """Prompt for a DM to ``user``; ``card`` picks the screen shown afterwards."""
    from app.bot.nav_input import ask_text

    await state.update_data(msg_user_id=int(user.id), msg_card=card)
    await ask_text(
        callback,
        state,
        prompt=(
            f"✉️ متن پیام برای <b>{html.escape(user.full_name or user.username or str(user.telegram_id))}</b> را بفرستید:"
        ),
        cancel_code="adm_usr",
        return_to=f"adm:{card}:view:{user.id}",
        fsm_state=AdminStates.user_message,
        edit=True,
    )

async def _staff_message_target(
    session: AsyncSession, user_id: int, card: str
) -> tuple[BotUser | None, str | None]:
    if card == MSG_CARD_RESELLER:
        if not await platform_can_manage_representatives():
            return None, feedback.REPRESENTATIVES_DISABLED
        user = await session.get(BotUser, int(user_id))
        if user is None or user.role != Role.RESELLER.value:
            return user, "نماینده یافت نشد"
        return user, None
    return await _platform_shop_user(session, user_id)

@router.callback_query(F.data.startswith("adm:users:msg:"))
@require_bot_owner_handler
async def adm_users_message_start(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    user, deny = await _platform_shop_user(session, int(callback.data.split(":")[-1]))
    if deny:
        await callback.answer(deny, show_alert=True)
        return
    await callback.answer()
    await _ask_staff_message(callback, state, user, card=MSG_CARD_USER)

@router.callback_query(F.data.regexp(r"^adm:resellers:msg:\d+$"))
@require_bot_owner_handler
@require_platform_rep_mgmt
async def adm_resellers_message_start(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    user_id = int((callback.data or "").rsplit(":", 1)[-1])
    user, deny = await _staff_message_target(session, user_id, MSG_CARD_RESELLER)
    if deny:
        await callback.answer(deny, show_alert=True)
        return
    await callback.answer()
    await _ask_staff_message(callback, state, user, card=MSG_CARD_RESELLER)

@router.message(AdminStates.user_message)
@require_bot_owner_handler
async def adm_users_message_send(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await state.clear()
        return
    if kb.is_cancel_text(message.text):
        from app.bot.nav_input import try_legacy_cancel

        if await try_legacy_cancel(message, state, session, db_user):
            return
        await _answer_users_nav(
            message,
            session,
            db_user,
            feedback.CANCELLED,
            state,
            clear_state=True,
        )
        return
    data = await state.get_data()
    uid = int(data.get("msg_user_id") or 0)
    card = str(data.get("msg_card") or MSG_CARD_USER)
    user, deny = await _staff_message_target(session, uid, card) if uid else (None, feedback.NOT_FOUND)
    if deny or user is None:
        await state.set_state(None)
        await _outcome_back_to_users(message, state, session, deny or feedback.NOT_FOUND)
        return
    await state.update_data(msg_text=message.text or "")
    await ask_for_buttons(message, state, flow=ADMIN_DM_FLOW, shop_id=None, renew=True)


async def _send_admin_dm(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser, buttons: list, **_: object
) -> None:
    """After the buttons step: send the owner's message, then show the person's card with the outcome."""
    message = callback.message
    if not isinstance(message, Message) or not await require_bot_owner(session, db_user, notify=False):
        await state.clear()
        return
    data = await state.get_data()
    card = str(data.get("msg_card") or MSG_CARD_USER)
    uid = int(data.get("msg_user_id") or 0)
    user, deny = await _staff_message_target(session, uid, card) if uid else (None, feedback.NOT_FOUND)
    if deny or user is None:
        await _outcome_back_to_users(message, state, session, deny or feedback.NOT_FOUND)
        return
    from app.services.users_quick import send_staff_dm

    try:
        await send_staff_dm(
            session,
            user,
            str(data.get("msg_text") or ""),
            shop_id=None,
            actor=bot_user_panel_label(db_user),
            sender_tg=int(db_user.telegram_id or 0),
            buttons=buttons,
        )
        await session.commit()
        status = feedback.MESSAGE_SENT
    except ValueError as e:
        status = f"⚠️ {user_safe_error(e)}"
    except TelegramAPIError:
        logger.warning("staff DM not delivered user=%s", user.id, exc_info=True)
        status = STAFF_DM_UNDELIVERED
    # The question sat in the card's panel: the card comes back there, with the outcome on top.
    build = _reseller_card if card == MSG_CARD_RESELLER else _user_card
    text, markup = await build(session, user)
    await state.clear()
    await safe_edit_text(message, f"{status}\n\n{text}", reply_markup=markup)


register_flow(ADMIN_DM_FLOW, _send_admin_dm)

@router.callback_query(F.data.startswith("adm:users:view:"))
@require_bot_owner_handler
async def adm_users_view(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
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
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    try:
        uid = int((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer(feedback.INVALID, show_alert=True)
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
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    parts = (callback.data or "").split(":")
    # adm:users:tagset:{uid}:{key}
    if len(parts) != 5:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    try:
        uid = int(parts[3])
    except ValueError:
        await callback.answer(feedback.INVALID, show_alert=True)
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
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    try:
        uid = int((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    user = await session.get(BotUser, uid)
    deny = _deny_if_outside_platform_shop(user)
    if deny:
        await callback.answer(deny, show_alert=True)
        return
    from app.services.users_quick import quick_renew_user

    try:
        _svc, label = await quick_renew_user(session, user, shop_id=None)
        await session.commit()
        await callback.answer(f"تمدید شد: {label}"[:180], show_alert=True)
    except ValueError as e:
        await callback.answer(user_safe_error(e, limit=160), show_alert=True)
    except Exception as e:
        await callback.answer(user_safe_error(e, limit=160), show_alert=True)
    if callback.message and user:
        await _render_user_card(callback.message, session, user, edit=True)

@router.callback_query(F.data.startswith("adm:users:svcs:"))
@require_bot_owner_handler
async def adm_users_services(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    user_id = int(callback.data.split(":")[-1])
    _user, deny = await _platform_shop_user(session, user_id)
    if deny:
        await callback.answer(deny, show_alert=True)
        return
    from app.services.bot_user_admin import list_service_snapshots, snapshot_telegram_lines

    # Answer before the panel reads so the button stops spinning at once.
    await callback.answer()
    snaps = await list_service_snapshots(session, user_id, shop_id=None)
    if not snaps:
        if callback.message:
            await safe_edit_text(
                callback.message,
                "این کاربر هنوز سرویسی ندارد.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [kb.service_import_button(user_id)],
                        [
                            kb.back_button(None, f"adm:users:view:{user_id}")
                        ],
                    ]
                ),
            )
        return
    text = format_services_list(
        format_services_list_header(user_id, subject="کاربر"),
        [snapshot_telegram_lines(snap, with_link=False) for snap in snaps[:SERVICE_BUTTON_LIMIT]],
        without_button=max(0, len(snaps) - SERVICE_BUTTON_LIMIT),
    )
    if callback.message:
        await safe_edit_text(
            callback.message,
            text,
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
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
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
            session, bot_user_id=user_id, service_id=service_id, shop_id=None
        )
    except ValueError as e:
        await callback.answer(user_safe_error(e, limit=160), show_alert=True)
        return
    # Answer before the panel read so the button stops spinning at once.
    await callback.answer()
    snap = await service_snapshot(session, svc)
    await session.commit()
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
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
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
            session, bot_user_id=user_id, service_id=service_id, shop_id=None
        )
        snap = await service_snapshot(session, svc)
        await session.commit()
    except ValueError as e:
        await callback.answer(user_safe_error(e, limit=160), show_alert=True)
        return
    url = snap.subscription_url
    if not url:
        await callback.answer("لینک اشتراک برای این سرویس موجود نیست", show_alert=True)
        return
    await callback.answer()
    if callback.message:
        with keep_sent():  # the link is copied from here later
            await callback.message.answer(
                f"🔗 لینک سرویس #{service_id}:\n<code>{html.escape(url)}</code>"
            )

@router.callback_query(F.data.startswith("adm:users:svcrenew:"))
@require_bot_owner_handler
async def adm_users_service_renew(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser, state: FSMContext | None = None
):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
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

    reason = await take_reason(state, callback)
    if reason is None:
        await ask_reason(callback, state, label="علت تمدید")
        return
    try:
        svc = await get_owned_service(
            session, bot_user_id=user_id, service_id=service_id, shop_id=None
        )
        plan = await session.get(Plan, int(svc.plan_id)) if svc.plan_id else None
        if plan is None:
            await callback.answer("پلن این سرویس مشخص نیست؛ از وب‌پنل تمدید کنید", show_alert=True)
            return
        await admin_renew_service(session, svc, plan=plan, reset_traffic=True)
        snap = await service_snapshot(session, svc)
    except ValueError as e:
        await callback.answer(user_safe_error(e, limit=160), show_alert=True)
        return
    except Exception as e:
        await callback.answer(user_safe_error(e, limit=160), show_alert=True)
        return
    await log_and_notify(
        session,
        bot_actor(db_user, shop_id=None),
        action="service_renew",
        target_type="service",
        target_id=service_id,
        reason=reason,
        targets=[NoticeTarget(user_id, int(_user.telegram_id or 0), None, svc.pg_username or "")],
    )
    await callback.answer(feedback.RENEWED)
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
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
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
            session, bot_user_id=user_id, service_id=service_id, shop_id=None
        )
    except ValueError as e:
        await callback.answer(user_safe_error(e, limit=160), show_alert=True)
        return
    await callback.answer()
    label = svc.pg_username or f"#{service_id}"
    from app.services.service_import import is_external_service

    external = is_external_service(svc)
    if callback.message:
        text = (
            f"⚠️ <b>حذف سرویس #{service_id}</b>\n\n"
            f"سرویس <code>{html.escape(str(label))}</code> از بیرون ربات اضافه شده است.\n"
            "«جدا شود» فقط آن را از لیست مشتری برمی‌دارد و حساب VPN در پاسارگارد می‌ماند. "
            "«حذف همراه حساب» حساب پاسارگارد را هم برای همیشه حذف می‌کند و برگشت‌ناپذیر است."
            if external
            else (
                f"⚠️ <b>حذف سرویس #{service_id}</b>\n\n"
                f"سرویس <code>{html.escape(str(label))}</code> و کاربر پاسارگارد مرتبط "
                f"برای همیشه حذف شوند؟\nاین عمل برگشت‌ناپذیر است."
            )
        )
        await safe_edit_text(
            callback.message,
            text,
            reply_markup=kb.admin_user_service_delete_confirm(user_id, service_id, external=external),
        )

@router.callback_query(F.data.startswith("adm:users:svcdel:"))
@require_bot_owner_handler
async def adm_users_service_delete(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser, state: FSMContext | None = None
):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    parts = callback.data.split(":")
    # adm:users:svcdel:{uid}:{sid} — not svcdelask
    if len(parts) < 5 or parts[2] != "svcdel":
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    user_id = int(parts[3])
    service_id = int(parts[4])
    _user, deny = await _platform_shop_user(session, user_id)
    if deny:
        await callback.answer(deny, show_alert=True)
        return
    from app.services.bot_user_admin import admin_delete_service, get_owned_service
    from app.services.service_import import admin_delete_pg_flag, is_external_service

    explicit_pg = len(parts) > 5 and parts[5] == "pg"
    reason = await take_reason(state, callback)
    if reason is None:
        await ask_reason(callback, state, label="علت حذف سرویس")
        return
    try:
        svc = await get_owned_service(
            session, bot_user_id=user_id, service_id=service_id, shop_id=None
        )
        detach_only = is_external_service(svc) and not explicit_pg
        notice_target = NoticeTarget(user_id, int(_user.telegram_id or 0), None, svc.pg_username or "")
        await admin_delete_service(
            session, svc, delete_pg=admin_delete_pg_flag(svc, explicit=explicit_pg)
        )
    except ValueError as e:
        await callback.answer(user_safe_error(e, limit=160), show_alert=True)
        return
    except Exception as e:
        await callback.answer(user_safe_error(e, limit=160), show_alert=True)
        return
    done = f"سرویس #{service_id} از لیست مشتری جدا شد؛ حساب پاسارگارد حفظ شد." if detach_only else f"سرویس #{service_id} حذف شد."
    await log_and_notify(
        session,
        bot_actor(db_user, shop_id=None),
        action="service_delete",
        target_type="service",
        target_id=service_id,
        reason=reason,
        targets=[notice_target],
        notice_action="service_detach" if detach_only else "service_delete",
    )
    await callback.answer(done, show_alert=True)
    if callback.message:
        await safe_edit_text(
            callback.message,
            f"✅ {done}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        kb.back_button(None, f"adm:users:svcs:{user_id}")
                    ]
                ]
            ),
        )


async def _service_name(session: AsyncSession, service_id: int) -> str:
    service = await session.get(UserService, int(service_id))
    return (service.pg_username or "") if service is not None else ""


async def _adm_apply_staged_adjust(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    *,
    user_id: int,
    service_id: int,
    adj_key: str,
    staged: StagedAdjustment,
) -> ServiceSnapshot | None:
    """Apply a staged change; a refused change is staged again, an unknown outcome is not."""
    try:
        svc = await get_owned_service(session, bot_user_id=user_id, service_id=service_id, shop_id=None)
        await admin_extend_service(session, svc, extra_days=staged.days, extra_gb=staged.gb)
        return await service_snapshot(session, svc)
    except ValueError as exc:
        await rollback_quiet(session)
        await restore_staged(state, adj_key, staged)
        await callback.answer(user_safe_error(exc, limit=160), show_alert=True)
    except Exception:
        await rollback_quiet(session)
        logger.warning("service extend outcome unknown sid=%s", service_id, exc_info=True)
        await callback.answer(UNKNOWN_ADJUST_OUTCOME, show_alert=True)
    return None


async def _adm_confirm_service_adjust(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    *,
    user_id: int,
    service_id: int,
    adj_key: str,
) -> bool:
    """Apply the staged change; True only when the panel accepted it."""
    async with service_adjust_lock(service_id):
        staged = await take_staged(state, adj_key)
        if staged.is_empty:
            await callback.answer(NOTHING_STAGED, show_alert=True)
            return False
        snap = await _adm_apply_staged_adjust(
            callback, state, session, user_id=user_id, service_id=service_id, adj_key=adj_key, staged=staged
        )
    if snap is None:
        return False
    await callback.answer("مانده به‌روز شد")
    if callback.message:
        await safe_edit_text(
            callback.message,
            "✅ مانده سرویس به‌روز شد\n\n" + snapshot_telegram_lines(snap),
            reply_markup=kb.admin_user_service_actions(user_id, service_id),
        )
    return True


async def _adm_confirm_with_reason(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    *,
    customer: BotUser,
    service_id: int,
    adj_key: str,
) -> None:
    """Ask for the reason, apply the staged change, then log it and tell the customer."""
    if read_staged(await state.get_data(), adj_key).is_empty:
        await callback.answer(NOTHING_STAGED, show_alert=True)
        return
    reason = await take_reason(state, callback)
    if reason is None:
        await ask_reason(callback, state, label="علت تغییر مانده سرویس")
        return
    user_id = int(customer.id)
    if not await _adm_confirm_service_adjust(
        callback, state, session, user_id=user_id, service_id=service_id, adj_key=adj_key
    ):
        return
    await log_and_notify(
        session,
        bot_actor(db_user, shop_id=None),
        action="service_extend",
        target_type="service",
        target_id=service_id,
        reason=reason,
        targets=[NoticeTarget(user_id, int(customer.telegram_id or 0), None, await _service_name(session, service_id))],
    )


@router.callback_query(F.data.startswith("adm:users:svcadj:"))
@require_bot_owner_handler
async def adm_users_service_adjust(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    parts = (callback.data or "").split(":")
    # adm:users:svcadj:{uid}:{sid}[ :days|gb|confirm : +/-|input ]
    if len(parts) < 5:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    try:
        user_id = int(parts[3])
        service_id = int(parts[4])
    except ValueError:
        await callback.answer(feedback.INVALID, show_alert=True)
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
        await callback.answer()
        if callback.message:
            from app.bot.nav_input import ask_text

            await ask_text(
                callback,
                state,
                prompt=f"تعداد روز تغییر را وارد کنید (±{MAX_EXTEND_DAYS}، منفی = کاهش):",
                cancel_code="adm_usr",
                return_to=f"adm:users:svcadj:{user_id}:{service_id}",
                fsm_state=AdminStates.svc_adjust_days_input,
                edit=False,
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
        await callback.answer()
        if callback.message:
            from app.bot.nav_input import ask_text

            await ask_text(
                callback,
                state,
                prompt=f"مقدار گیگ تغییر را وارد کنید (±{MAX_EXTEND_GB}، منفی = کاهش):",
                cancel_code="adm_usr",
                return_to=f"adm:users:svcadj:{user_id}:{service_id}",
                fsm_state=AdminStates.svc_adjust_gb_input,
                edit=False,
            )
        return
    elif action == "confirm":
        await _adm_confirm_with_reason(
            callback, state, session, db_user, customer=_user, service_id=service_id, adj_key=adj_key
        )
        return
    else:
        # Open adjust screen
        await state.update_data(**{adj_key: {"days": days, "gb": gb}})
        await callback.answer()

    if callback.message:
        await safe_edit_text(
            callback.message,
            format_adjust_prompt(service_id, days=days, gb=gb),
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
        from app.bot.nav_input import try_legacy_cancel

        if await try_legacy_cancel(message, state, session, db_user):
            return
        await _answer_users_nav(
            message,
            session,
            db_user,
            feedback.CANCELLED,
            state,
            clear_state=True,
        )
        return
    from app.services.bot_user_admin import MAX_EXTEND_DAYS

    try:
        days = parse_bot_int(message.text)
    except ValueError:
        await message.answer(feedback.ENTER_VALID_NUMBER_SHORT)
        return
    if abs(days) > MAX_EXTEND_DAYS:
        await message.answer(
            f"روز باید بین ±{MAX_EXTEND_DAYS} باشد",
        )
        return
    data = await state.get_data()
    user_id = int(data.get("svcadj_uid") or 0)
    service_id = int(data.get("svcadj_sid") or 0)
    if not user_id or not service_id:
        await state.set_state(None)
        await _outcome_back_to_users(message, state, session, STEP_EXPIRED_TEXT)
        return
    adj_key = f"svcadj:{user_id}:{service_id}"
    stored = data.get(adj_key) if isinstance(data.get(adj_key), dict) else {}
    gb = float(stored.get("gb") or 0)
    await state.set_state(None)
    await state.update_data(**{adj_key: {"days": days, "gb": gb}})
    # The adjuster replaces the prompt in place; the menu keyboard never changed.
    await finish_text_step(
        message,
        state,
        text=format_adjust_prompt(service_id, days=days, gb=gb),
        inline=kb.admin_user_service_adjust_keyboard(user_id, service_id, days=days, gb=gb),
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
        from app.bot.nav_input import try_legacy_cancel

        if await try_legacy_cancel(message, state, session, db_user):
            return
        await _answer_users_nav(
            message,
            session,
            db_user,
            feedback.CANCELLED,
            state,
            clear_state=True,
        )
        return
    from app.services.bot_user_admin import MAX_EXTEND_GB

    try:
        gb = parse_bot_float(message.text)
    except ValueError:
        await message.answer(feedback.ENTER_VALID_NUMBER_SHORT)
        return
    if abs(gb) > MAX_EXTEND_GB:
        await message.answer(
            f"گیگ باید بین ±{MAX_EXTEND_GB} باشد",
        )
        return
    data = await state.get_data()
    user_id = int(data.get("svcadj_uid") or 0)
    service_id = int(data.get("svcadj_sid") or 0)
    if not user_id or not service_id:
        await state.set_state(None)
        await _outcome_back_to_users(message, state, session, STEP_EXPIRED_TEXT)
        return
    adj_key = f"svcadj:{user_id}:{service_id}"
    stored = data.get(adj_key) if isinstance(data.get(adj_key), dict) else {}
    days = int(stored.get("days") or 0)
    await state.set_state(None)
    await state.update_data(**{adj_key: {"days": days, "gb": gb}})
    # The adjuster replaces the prompt in place; the menu keyboard never changed.
    await finish_text_step(
        message,
        state,
        text=format_adjust_prompt(service_id, days=days, gb=gb),
        inline=kb.admin_user_service_adjust_keyboard(user_id, service_id, days=days, gb=gb),
    )

@router.callback_query(F.data.startswith("adm:users:block:"))
@require_bot_owner_handler
async def adm_users_block(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
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

    await state.update_data(block_user_id=user_id)
    await callback.answer()
    if callback.message:
        from app.bot.nav_input import ask_text

        await ask_text(
            callback,
            state,
            prompt=(
                f"علت مسدودسازی کاربر <code>{user.telegram_id}</code> را بنویسید "
                "(حداقل ۳ کاراکتر — برای کاربر ارسال می‌شود):"
            ),
            cancel_code="adm_usr",
            return_to=f"adm:users:view:{user_id}",
            fsm_state=AdminStates.block_user_reason,
            edit=False,
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
        from app.bot.nav_input import try_legacy_cancel

        if await try_legacy_cancel(message, state, session, db_user):
            return
        await _answer_users_nav(
            message,
            session,
            db_user,
            feedback.CANCELLED,
            state,
            clear_state=True,
        )
        return
    data = await state.get_data()
    user_id = int(data.get("block_user_id") or 0)
    reason = (message.text or "").strip()
    if len(reason) < 3:
        await message.answer("علت خیلی کوتاه است؛ دست‌کم ۳ حرف بنویسید.")
        return
    user, deny = await _platform_shop_user(session, user_id) if user_id else (None, feedback.NOT_FOUND)
    if deny:
        await _outcome_back_to_users(message, state, session, deny)
        return
    from app.services.notifications import notify_account_edit
    from app.services.users import is_protected_admin

    if is_protected_admin(user):
        await _outcome_on_card(message, state, session, user, "⚠️ مسدود کردن ادمین مجاز نیست.")
        return
    await state.set_state(None)
    user.is_blocked = True
    await session.commit()
    await notify_account_edit(
        session,
        user=user,
        event="block",
        reason=reason,
        actor=db_user.username or db_user.full_name or "ادمین ربات",
    )
    await _outcome_on_card(message, state, session, user, f"🚫 کاربر مسدود شد.\nعلت: {html.escape(reason)}")

@router.callback_query(F.data.startswith("adm:users:delask:"))
@require_bot_owner_handler
async def adm_users_delask(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
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
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
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
    await state.update_data(delete_user_id=user_id)
    if callback.message:
        from app.bot.nav_input import ask_text

        await ask_text(
            callback,
            state,
            prompt=(
                f"علت حذف کامل کاربر <code>{user.telegram_id}</code> را بنویسید "
                "(حداقل ۳ کاراکتر — برای خود کاربر ارسال می‌شود):\n\nبرای لغو: انصراف"
            ),
            cancel_code="adm_usr",
            return_to=f"adm:users:view:{user_id}",
            fsm_state=AdminStates.delete_user_reason,
            edit=False,
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
        from app.bot.nav_input import try_legacy_cancel

        if await try_legacy_cancel(message, state, session, db_user):
            return
        await _answer_users_nav(
            message,
            session,
            db_user,
            feedback.CANCELLED,
            state,
            clear_state=True,
        )
        return
    reason = (message.text or "").strip()
    if len(reason) < 3:
        await message.answer("علت حداقل ۳ کاراکتر باشد. دوباره بنویسید یا انصراف بزنید:")
        return
    data = await state.get_data()
    user_id = int(data.get("delete_user_id") or 0)
    await state.set_state(None)
    if not user_id:
        await _outcome_back_to_users(message, state, session, STEP_EXPIRED_TEXT)
        return

    from app.services.notifications import notify_account_edit
    from app.services.users import delete_bot_user, is_protected_admin

    user, deny = await _platform_shop_user(session, user_id)
    if deny:
        await _outcome_back_to_users(message, state, session, deny)
        return
    if is_protected_admin(user):
        await _outcome_on_card(message, state, session, user, "⚠️ حذف ادمین مجاز نیست.")
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
        await _outcome_on_card(message, state, session, user, f"⚠️ {user_safe_error(e)}")
        return
    except Exception as e:
        logger.exception("bot user delete failed user=%s", user_id)
        await rollback_quiet(session)
        await _outcome_back_to_users(message, state, session, f"⚠️ {user_safe_error(e)}")
        return
    await _outcome_back_to_users(
        message, state, session,
        f"🗑 کاربر <code>{info.get('telegram_id')}</code> ({html.escape(str(info.get('name') or '—'))}) "
        f"حذف شد.\nعلت: {html.escape(reason)}",
    )

@router.callback_query(F.data.startswith("adm:users:unres:"))
@require_bot_owner_handler
async def adm_users_unreseller_ask(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
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
    await state.update_data(revoke_user_id=user_id)
    if callback.message:
        from app.bot.nav_input import ask_text

        await ask_text(
            callback,
            state,
            prompt=(
                f"علت حذف نمایندگی کاربر <code>{user.telegram_id}</code> را بنویسید "
                "(برای خود کاربر ارسال می‌شود):\n\nبرای لغو: انصراف"
            ),
            cancel_code="adm_usr",
            return_to=f"adm:users:view:{user_id}",
            fsm_state=AdminStates.revoke_reseller_reason,
            edit=False,
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
        from app.bot.nav_input import try_legacy_cancel

        if await try_legacy_cancel(message, state, session, db_user):
            return
        await _answer_users_nav(
            message,
            session,
            db_user,
            feedback.CANCELLED,
            state,
            clear_state=True,
        )
        return
    reason = (message.text or "").strip()
    if len(reason) < 3:
        await message.answer("علت حداقل ۳ کاراکتر باشد. دوباره بنویسید یا انصراف بزنید:")
        return
    data = await state.get_data()
    user_id = int(data.get("revoke_user_id") or 0)
    await state.set_state(None)
    if not user_id:
        await _outcome_back_to_users(message, state, session, STEP_EXPIRED_TEXT)
        return
    target, deny = await _platform_shop_user(session, user_id)
    if deny:
        await _outcome_back_to_users(message, state, session, deny)
        return

    from app.services.resellers import notify_reseller_revoked, revoke_reseller

    try:
        info = await revoke_reseller(
            session, user_id, delete_pg_admin=True, reason=reason
        )
    except ValueError as e:
        await _outcome_on_card(message, state, session, target, f"⚠️ {user_safe_error(e)}")
        return
    except Exception as e:
        logger.exception("reseller revoke failed user=%s", user_id)
        await rollback_quiet(session)
        await _outcome_back_to_users(message, state, session, f"⚠️ {user_safe_error(e)}")
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
    status = f"🤝 نمایندگی حذف شد.\nعلت: {html.escape(reason)}\n{note}"
    if user:
        await _outcome_on_card(message, state, session, user, status)
    else:
        await _outcome_back_to_users(message, state, session, status)

@router.callback_query(F.data == "adm:resellers")
@require_bot_owner_handler
@require_platform_rep_mgmt
async def adm_resellers(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    await callback.answer()
    if callback.message:
        from app.bot.handlers.reply_nav import open_admin_resellers_hub

        await open_admin_resellers_hub(
            callback.message,
            session,
            db_user,
            state,
            push=False,
            is_reseller_bot=False,
        )

RESELLERS_PAGE_SIZE = 10

@router.callback_query(F.data.startswith("adm:resellers:list:"))
@require_bot_owner_handler
@require_platform_rep_mgmt
async def adm_resellers_list(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
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
        text += "\n\nهنوز نماینده‌ای ثبت نشده است."
    if callback.message:
        from app.bot.nav_inline import with_inline_back

        ui = await get_all_settings(session)
        markup = kb.admin_resellers_list_keyboard(
            page=page, has_prev=has_prev, has_next=has_next, rows=rows
        )
        markup = with_inline_back(markup, ui, "nv:adm:resellers")
        await callback.message.edit_text(text, reply_markup=markup)

@router.callback_query(F.data == "adm:resellers:search")
@require_bot_owner_handler
@require_platform_rep_mgmt
async def adm_resellers_search_start(callback: CallbackQuery, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    await callback.answer()
    from app.bot.nav_input import ask_text

    await ask_text(
        callback,
        state,
        prompt=RESELLER_SEARCH_PROMPT,
        cancel_code="adm_res",
        return_to="nv:adm:resellers",
        fsm_state=AdminStates.reseller_search,
        edit=True,
    )


@router.message(AdminStates.reseller_search)
@require_bot_owner_handler
@require_platform_rep_mgmt
async def adm_resellers_search(
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
        await message.answer(feedback.CANCELLED)
        return
    query = clean_handle(message.text)
    if not query or len(query) > MAX_SEARCH_LENGTH:
        await message.answer("نام، آیدی عددی یا یوزرنیم تلگرام نماینده را بفرستید؛ مثلاً <code>123456789</code> یا <code>@username</code>.")
        return
    reseller = await find_bot_user(session, query, scope=BotUser.role == Role.RESELLER.value)
    if reseller is not None:
        text, markup = await _reseller_card(session, reseller)
    else:
        # No exact id or @username: search names and usernames by part.
        text, markup = await staff_reseller_search(session, query)
    await _show_search_result(message, state, text, markup)


RESELLER_SVCS_PAGE_SIZE = 10


def _oversell_button_state(profile) -> bool | None:
    return None if profile is None or is_payg(profile) else bool(profile.allow_volume_oversell)


def _oversell_line(profile) -> str:
    state = _oversell_button_state(profile)
    if state is None:
        return ""
    return "فروش بیش از سقف حجم: " + ("روشن (بدون محدودیت)" if state else "خاموش (تا سقف اشتراک)") + "\n"

async def _render_reseller_card(
    message: Message,
    session: AsyncSession,
    user: BotUser,
    *,
    edit: bool = False,
) -> None:
    text, markup = await _reseller_card(session, user)
    if edit:
        try:
            await message.edit_text(text, reply_markup=markup)
            return
        except TelegramAPIError:
            logger.debug("reseller card edit failed; sending it anew", exc_info=True)
    await message.answer(text, reply_markup=markup)


async def _reseller_card(session: AsyncSession, user: BotUser) -> tuple[str, InlineKeyboardMarkup]:
    """The platform's card of one reseller account and its actions."""
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
        f"سرویس‌های فروشگاه: {shop_svc_count}\n"
        f"{_oversell_line(profile)}\n"
        f"<i>سرویس‌های پاسارگارد = کاربران VPN زیر ادمین نماینده</i>"
    )
    markup = kb.admin_reseller_actions(
        user.id,
        has_shop_services=int(shop_svc_count) > 0,
        color_tag=effective_color_tag(user),
        oversell=_oversell_button_state(profile),
    )
    return text, markup

@router.callback_query(F.data.startswith("adm:resellers:view:"))
@require_bot_owner_handler
@require_platform_rep_mgmt
async def adm_resellers_view(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
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
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    try:
        uid = int((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer(feedback.INVALID, show_alert=True)
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
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    parts = (callback.data or "").split(":")
    # adm:resellers:tagset:{uid}:{key}
    if len(parts) != 5:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    try:
        uid = int(parts[3])
    except ValueError:
        await callback.answer(feedback.INVALID, show_alert=True)
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

@router.callback_query(F.data.regexp(r"^adm:resellers:osell:\d+$"))
@require_bot_owner_handler
@require_platform_rep_mgmt
async def adm_resellers_oversell_toggle(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser
):
    """Flip a reseller's volume-oversell switch from the reseller card."""
    try:
        uid = int((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    user = await session.get(BotUser, uid)
    profile = await load_reseller_profile(session, uid) if user and user.role == Role.RESELLER.value else None
    if profile is None:
        await callback.answer("نماینده یافت نشد", show_alert=True)
        return
    try:
        # The decorator already admitted only the Owner.
        allow = await set_shop_oversell(
            session, uid, allow=not bool(profile.allow_volume_oversell), actor_is_owner=True
        )
    except ValueError as e:
        await callback.answer(user_safe_error(e, limit=160), show_alert=True)
        return
    await callback.answer("فروش بیش از سقف روشن شد" if allow else "فروش تا سقف اشتراک محدود شد")
    if callback.message:
        await _render_reseller_card(callback.message, session, user, edit=True)

@router.callback_query(F.data.startswith("adm:resellers:capadj:"))
@require_bot_owner_handler
@require_platform_rep_mgmt
async def adm_resellers_capacity_adjust(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    parts = (callback.data or "").split(":")
    # adm:resellers:capadj:{uid}[ :days|gb|confirm : +/-|input ]
    if len(parts) < 4:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    try:
        user_id = int(parts[3])
    except ValueError:
        await callback.answer(feedback.INVALID, show_alert=True)
        return

    from app.services.pg_admin_subscription import MAX_ADJUST_DAYS, MAX_ADJUST_GB
    from app.services.resellers import get_reseller_profile

    profile = await get_reseller_profile(session, user_id)
    if not profile or not (profile.pg_admin_username or "").strip():
        await callback.answer(feedback.PG_ADMIN_NOT_SET, show_alert=True)
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
        await callback.answer()
        if callback.message:
            from app.bot.nav_input import ask_text

            await ask_text(
                callback,
                state,
                prompt=f"تعداد روز تغییر را وارد کنید (±{MAX_ADJUST_DAYS}، منفی = کاهش):",
                cancel_code="adm_cap",
                fsm_state=AdminStates.reseller_cap_days_input,
                edit=False,
            )
        return
    elif action == "gb" and detail == "input":
        await state.update_data(
            **{adj_key: {"days": days, "gb": gb}, "capadj_uid": user_id}
        )
        await callback.answer()
        if callback.message:
            from app.bot.nav_input import ask_text

            await ask_text(
                callback,
                state,
                prompt=f"مقدار گیگ تغییر را وارد کنید (±{MAX_ADJUST_GB}، منفی = کاهش):",
                cancel_code="adm_cap",
                fsm_state=AdminStates.reseller_cap_gb_input,
                edit=False,
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
        reseller_profile = await load_reseller_profile(session, user_id)
        if callback.message:
            await safe_edit_text(
                callback.message,
                f"✅ ظرفیت اشتراک به‌روز شد\n\nحجم کل: <b>{total}</b> گیگ\nانقضا: {html.escape(exp_txt)}",
                reply_markup=kb.admin_reseller_actions(
                    user_id,
                    has_shop_services=int(shop_count) > 0,
                    color_tag=effective_color_tag(reseller_user) if reseller_user else None,
                    oversell=_oversell_button_state(reseller_profile),
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
        from app.bot.nav_input import try_legacy_cancel

        if await try_legacy_cancel(message, state, session, db_user):
            return
        data = await state.get_data()
        user_id = int(data.get("capadj_uid") or 0)
        await state.set_state(None)
        if not user_id:
            await message.answer(feedback.CANCELLED)
        if user_id and message:
            # Resend capacity adjust panel at bottom.
            adj_key = f"capadj:{user_id}"
            stored = data.get(adj_key) if isinstance(data.get(adj_key), dict) else {}
            await message.answer(
                f"⏱ <b>تغییر ظرفیت نماینده #{user_id}</b>\n\n"
                f"روز: <b>{int(stored.get('days') or 0)}</b> · "
                f"گیگ: <b>{int(stored.get('gb') or 0)}</b>",
                reply_markup=kb.admin_reseller_capacity_adjust_keyboard(
                    user_id,
                    days=int(stored.get("days") or 0),
                    gb=int(stored.get("gb") or 0),
                ),
            )
        return
    from app.services.pg_admin_subscription import MAX_ADJUST_DAYS

    try:
        days = parse_bot_int(message.text)
    except ValueError:
        await message.answer(feedback.ENTER_VALID_NUMBER_SHORT)
        return
    if abs(days) > MAX_ADJUST_DAYS:
        await message.answer(f"روز باید بین ±{MAX_ADJUST_DAYS} باشد")
        return
    data = await state.get_data()
    user_id = int(data.get("capadj_uid") or 0)
    if not user_id:
        await state.set_state(None)
        await message.answer("زمان این مرحله تمام شد. دوباره شروع کنید.")
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
        from app.bot.nav_input import try_legacy_cancel

        if await try_legacy_cancel(message, state, session, db_user):
            return
        data = await state.get_data()
        user_id = int(data.get("capadj_uid") or 0)
        await state.set_state(None)
        if not user_id:
            await message.answer(feedback.CANCELLED)
        if user_id and message:
            adj_key = f"capadj:{user_id}"
            stored = data.get(adj_key) if isinstance(data.get(adj_key), dict) else {}
            await message.answer(
                f"⏱ <b>تغییر ظرفیت نماینده #{user_id}</b>\n\n"
                f"روز: <b>{int(stored.get('days') or 0)}</b> · "
                f"گیگ: <b>{int(stored.get('gb') or 0)}</b>",
                reply_markup=kb.admin_reseller_capacity_adjust_keyboard(
                    user_id,
                    days=int(stored.get("days") or 0),
                    gb=int(stored.get("gb") or 0),
                ),
            )
        return
    from app.services.pg_admin_subscription import MAX_ADJUST_GB

    try:
        gb = parse_bot_int(message.text)
    except ValueError:
        await message.answer(feedback.ENTER_VALID_NUMBER_SHORT)
        return
    if abs(gb) > MAX_ADJUST_GB:
        await message.answer(f"گیگ باید بین ±{MAX_ADJUST_GB} باشد")
        return
    data = await state.get_data()
    user_id = int(data.get("capadj_uid") or 0)
    if not user_id:
        await state.set_state(None)
        await message.answer("زمان این مرحله تمام شد. دوباره شروع کنید.")
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
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    parts = (callback.data or "").split(":")
    # adm:resellers:svcs:{id} or adm:resellers:svcs:{id}:{page}
    try:
        user_id = int(parts[3])
    except (IndexError, ValueError):
        await callback.answer(feedback.INVALID, show_alert=True)
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
                kb.back_button(None, f"adm:resellers:view:{user_id}")
            ]
        ]
    )
    if not uname:
        if callback.message:
            await safe_edit_text(
                callback.message,
                "حساب ادمین پاسارگارد برای این نماینده تنظیم نشده است.",
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
        text += "\n\nاین ادمین هنوز سرویسی ندارد."
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
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    await callback.answer()
    from app.bot.nav_input import ask_text

    await ask_text(
        callback,
        state,
        prompt=(
            "آیدی عددی تلگرام کاربر را برای نماینده‌شدن بفرستید.\n\n"
            "مثال:\n"
            "<code>123456789 1</code>\n\n"
            "• عدد اول: آیدی تلگرام\n"
            "• عدد دوم: ۱ = اجازه تأیید رسید، ۰ یا خالی = بدون تأیید"
        ),
        cancel_code="adm_home",
        return_to="adm:resellers",
        fsm_state=AdminStates.make_reseller,
        edit=True,
    )

@router.callback_query(F.data == "adm:resapp:list")
@require_bot_owner_handler
@require_platform_rep_mgmt
async def adm_resapp_list(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
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
                "درخواست بازی وجود ندارد.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[kb.back_button(None, "nv:adm:resellers")]]
                ),
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
    rows.append([kb.back_button(None, "nv:adm:resellers")])
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
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    from app.services.resellers import get_application

    app = await get_application(session, int(callback.data.split(":")[-1]))
    if not app:
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
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
        ResellerApplicationStatus.PENDING_PAYMENT.value: "در انتظار پرداخت",
        ResellerApplicationStatus.AWAITING_APPROVAL.value: "در انتظار تأیید",
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
            reply_markup=with_inline_back(
                kb.reseller_app_review(app.id, allow_approve=not pending_pay), None, RESELLER_APPS_LIST
            ),
        )

@router.callback_query(F.data.startswith("adm:resapp:ok:"))
@require_bot_owner_handler
@require_platform_rep_mgmt
async def adm_resapp_ok(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    from app.services.resellers import (
        approve_application,
        format_credentials_message,
        get_application,
        get_reseller_panel_base_url,
    )

    app = await get_application(session, int(callback.data.split(":")[-1]))
    if not app:
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
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
            with keep_sent():  # a notice or review item, never a temporary panel
                await callback.bot.send_message(
                    user.telegram_id,
                    format_credentials_message(creds),
                    parse_mode="HTML",
                )
        except Exception:
            logger.debug("adm_resapp_ok: ignored failure", exc_info=True)
    await callback.answer("تأیید شد ✅", show_alert=True)
    if callback.message:
        await callback.message.edit_text(
            f"✅ درخواست #{app.id} تأیید شد — اطلاعات ورود ارسال شد.",
            reply_markup=with_inline_back(None, None, RESELLER_APPS_LIST),
        )

@router.callback_query(F.data.startswith("adm:resapp:no:"))
@require_bot_owner_handler
@require_platform_rep_mgmt
async def adm_resapp_no(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    from app.services.resellers import get_application, reject_application

    app = await get_application(session, int(callback.data.split(":")[-1]))
    if not app:
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
        return
    try:
        await reject_application(session, app, reviewer_tg=db_user.telegram_id)
    except Exception as e:
        await callback.answer(user_safe_error(e), show_alert=True)
        return
    user = await session.get(BotUser, app.user_id)
    if user:
        try:
            with keep_sent():  # a notice or review item, never a temporary panel
                await callback.bot.send_message(
                    user.telegram_id,
                    "❌ درخواست نمایندگی شما رد شد.\nدر صورت نیاز با پشتیبانی در ارتباط باشید.",
                    reply_markup=support_markup(),
                )
        except Exception:
            logger.debug("adm_resapp_no: ignored failure", exc_info=True)
    await callback.answer(feedback.REJECTED, show_alert=True)
    if callback.message:
        await callback.message.edit_text(
            f"❌ درخواست #{app.id} رد شد.",
            reply_markup=with_inline_back(None, None, RESELLER_APPS_LIST),
        )

@router.message(AdminStates.make_reseller)
@require_bot_owner_handler
@require_platform_rep_mgmt
async def make_res(
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
        from app.bot.handlers.reply_nav import open_admin_home
        from app.bot.nav_chrome import answer_staff_nav

        await answer_staff_nav(
            message,
            session,
            db_user,
            text=feedback.CANCELLED,
            classic=None,
            state=state,
            reopen_panel=open_admin_home,
            clear_state=True,
            is_reseller_bot=False,
            reseller_owner_id=None,
        )
        return
    parts = (message.text or "").split()
    try:
        tg_id = int(parts[0])
        can_approve = len(parts) > 1 and parts[1] in {"1", "approve=1", "yes", "بله"}
    except ValueError:
        await message.answer(
            "پیام را به همین شکل بفرستید:\n<code>123456789 1</code>"
        )
        return
    result = await session.execute(select(BotUser).where(BotUser.telegram_id == tg_id))
    user = result.scalar_one_or_none()
    if not user:
        await message.answer("کاربر باید حداقل یک بار ربات را استارت کرده باشد.")
        return
    await ask_reseller_plan(
        message, state, session, user_id=int(user.id), can_approve=can_approve
    )

@router.callback_query(F.data == "adm:tickets")
@require_bot_owner_handler
async def adm_tickets(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await _present_admin_tickets_list(callback.message, session, edit=True)


async def _load_platform_ticket(
    session: AsyncSession, ticket_id: int
) -> tuple[Ticket | None, str | None]:
    ticket = await get_ticket(session, int(ticket_id))
    if not ticket:
        return None, feedback.NOT_FOUND
    if ticket.reseller_id:
        return None, "این تیکت متعلق به فروشگاه نماینده است"
    return ticket, None


def _ticket_thread_text(ticket: Ticket, opener: BotUser | None = None) -> str:
    from app.services.formatting import info_block
    from app.services.notifications import _buyer_lines

    lines = [
        f"🎫 #{ticket.id} — {html.escape(ticket.subject or '')}",
        "",
        info_block(_buyer_lines(opener)),
        "",
    ]
    for m in (ticket.messages or [])[-12:]:
        who = "پشتیبانی" if m.is_staff else "کاربر"
        lines.append(f"<b>{who}:</b> {html.escape(m.body or '')}")
    return "\n".join(lines)


@router.callback_query(F.data.regexp(r"^adm:ticket:\d+$"))
@require_bot_owner_handler
async def adm_ticket_view(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser, state: FSMContext
):
    """Show ticket thread with Reply / Close — do not enter reply FSM yet."""
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    try:
        tid = int((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    ticket, deny = await _load_platform_ticket(session, tid)
    if deny:
        await callback.answer(deny, show_alert=True)
        return
    await callback.answer()
    await state.set_state(None)
    ui = await get_all_settings(session)
    opener = await session.get(BotUser, int(ticket.user_id)) if ticket.user_id else None
    if callback.message:
        await callback.message.edit_text(
            _ticket_thread_text(ticket, opener),
            reply_markup=_admin_ticket_actions_markup(
                ticket.id, ui, user_id=int(ticket.user_id) if ticket.user_id else None
            ),
        )


@router.callback_query(F.data.regexp(r"^adm:ticket:reply:\d+$"))
@require_bot_owner_handler
async def adm_ticket_reply_start(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser, state: FSMContext
):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    try:
        tid = int((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    ticket, deny = await _load_platform_ticket(session, tid)
    if deny:
        await callback.answer(deny, show_alert=True)
        return
    from app.db.models import TicketStatus

    if ticket.status == TicketStatus.CLOSED.value:
        await callback.answer(feedback.TICKET_CLOSED, show_alert=True)
        return
    await callback.answer()
    await state.update_data(ticket_id=ticket.id)
    from app.bot.nav_input import ask_text

    await ask_text(
        callback,
        state,
        prompt="پاسخ را بنویسید:",
        cancel_code="adm_tkt",
        return_to=f"adm:ticket:{tid}",
        fsm_state=AdminStates.ticket_reply,
        edit=False,
    )


@router.callback_query(F.data.regexp(r"^adm:ticket:close:\d+$"))
@require_bot_owner_handler
async def adm_ticket_close(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser, state: FSMContext
):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    try:
        tid = int((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    ticket, deny = await _load_platform_ticket(session, tid)
    if deny:
        await callback.answer(deny, show_alert=True)
        return
    from app.db.models import TicketStatus
    from app.services.tickets import close_ticket

    if ticket.status == TicketStatus.CLOSED.value:
        await callback.answer(feedback.ALREADY_CLOSED, show_alert=True)
    else:
        await close_ticket(session, ticket)
        await callback.answer(feedback.TICKET_CLOSED_DONE, show_alert=True)
    await state.set_state(None)
    if callback.message:
        await _present_admin_tickets_list(callback.message, session, edit=True)


@router.message(AdminStates.ticket_reply)
async def adm_ticket_reply(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if kb.is_cancel_text(message.text):
        from app.bot.nav_input import try_legacy_cancel

        if await try_legacy_cancel(message, state, session, db_user):
            return
        await _answer_tickets_nav(
            message,
            session,
            db_user,
            feedback.CANCELLED,
            state,
            clear_state=True,
        )
        return
    data = await state.get_data()
    ticket = await session.get(Ticket, data.get("ticket_id"))
    if not ticket:
        await state.clear()
        return
    if ticket.reseller_id:
        await _answer_tickets_nav(
            message,
            session,
            db_user,
            "دسترسی به تیکت فروشگاه ندارید.",
            state,
            clear_state=True,
        )
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
        logger.debug("adm_ticket_reply: ignored failure", exc_info=True)
    await _answer_tickets_nav(
        message,
        session,
        db_user,
        feedback.SENT,
        state,
        clear_state=False,
    )

@router.callback_query(F.data == "adm:broadcast")
@require_bot_owner_handler
async def adm_broadcast_start(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    await callback.answer()
    await state.clear()
    if callback.message:
        from app.bot.handlers.reply_nav import open_admin_broadcast_hub

        await open_admin_broadcast_hub(
            callback.message,
            session,
            db_user,
            state,
            push=False,
            is_reseller_bot=False,
        )

@router.callback_query(F.data.startswith("adm:broadcast:aud:"))
@require_bot_owner_handler
async def adm_broadcast_audience(callback: CallbackQuery, db_user: BotUser, state: FSMContext):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    audience = callback.data.rsplit(":", 1)[-1]
    try:
        audience_fa = html.escape(describe_audience(make_audience(audience, shop_id=None)))
    except ValueError:
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    await callback.answer()
    await state.update_data(broadcast_audience=audience)
    if callback.message:
        await callback.message.edit_text(
            f"📢 مخاطب: <b>{audience_fa}</b>\nمتن پیام را بفرستید؛ بولد، لینک و قالب‌بندی تلگرام حفظ می‌شود.\n"
            "بعد از متن می‌پرسیم دکمه زیر پیام باشد یا نه، و پیش از ارسال پیش‌نمایش را می‌بینید.\nبرای لغو: انصراف"
        )
        from app.bot.nav_input import ask_text

        await ask_text(
            callback,
            state,
            prompt="متن پیام:",
            cancel_code="adm_bc",
            fsm_state=AdminStates.broadcast_text,
            edit=False,
        )

@router.message(AdminStates.broadcast_text)
@require_bot_owner_handler
async def adm_broadcast_send(message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await state.clear()
        return
    if kb.is_cancel_text(message.text):
        from app.bot.nav_input import try_legacy_cancel

        if await try_legacy_cancel(message, state, session, db_user):
            return
        from app.bot.handlers.reply_nav import open_admin_broadcast_hub
        from app.bot.nav_chrome import answer_staff_nav

        await answer_staff_nav(
            message,
            session,
            db_user,
            text=feedback.CANCELLED,
            classic=None,
            state=state,
            reopen_panel=open_admin_broadcast_hub,
            clear_state=True,
            is_reseller_bot=False,
            reseller_owner_id=None,
        )
        return
    data = await state.get_data()
    audience = data.get("broadcast_audience") or "all"
    await preview_broadcast(message, state, session, audience=audience)

@router.callback_query(F.data == "adm:pg:stats")
@require_bot_owner_handler
async def pg_stats(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer(feedback.NOT_ADMIN, show_alert=True)
        return
    from app.bot.auth import can_platform_pg_page
    from app.bot.nav_inline import with_inline_back

    if not await can_platform_pg_page(db_user, "pg_overview"):
        await callback.answer("به نمای کلی دسترسی ندارید", show_alert=True)
        return
    await callback.answer()
    ui = await get_all_settings(session)
    markup = with_inline_back(None, ui, "nv:adm:pg")
    try:
        stats = await get_pg().get_system_stats()
    except Exception as e:
        if callback.message:
            await callback.message.edit_text(
                f"خطا: {user_safe_error(e)}", reply_markup=markup
            )
        return
    text = "🏠 <b>نمای کلی پاسارگارد</b>\n\n" + format_system_stats(stats)
    if callback.message:
        await callback.message.edit_text(text[:3500], reply_markup=markup)

# Node ops: app.bot.handlers.admin_pg_nodes (web /pg/nodes parity)

# PG hub / catalog hints live on admin_pg_users (no Owner middleware).
from app.bot.handlers.admin_pg_users import (  # noqa: E402,F401
    adm_pg,
    adm_pg_group_hint,
    adm_pg_template_hint,
)
