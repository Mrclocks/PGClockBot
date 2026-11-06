"""Failed and stuck deliveries in the bot, with the same actions as the web finance tab.

List -> card -> retry / dismiss, or (for a paid order without a service) refund, record a manual
delivery or close it. Every step re-resolves the actor's shop and the ``orders`` permission; the
order id in a callback is only a lookup key inside that scope. Closing actions ask for a second tap.
"""
from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.auth import require_bot_owner, resolve_bot_principal_bridge
from app.bot.nav_inline import with_inline_back
from app.bot.tg_utils import safe_edit_text
from app.config import get_settings
from app.db.models import BotUser, DeliveryFailure, Order, Plan
from app.services import bot_feedback_texts as feedback
from app.services.formatting import format_message, format_toman, format_user_label, info_block, kv_line, order_status_fa
from app.services.notifications import customer_markup
from app.services.reseller_access import load_reseller_actor
from app.services.redact import operator_error, redact
from app.services.resellers import has_bot_perm
from app.services.shop_scope import ShopScopeError, assert_order_retry_in_scope, is_platform_admin
from app.services.stuck_order_resolution import (
    cancel_and_refund,
    close_without_refund,
    count_still_stuck,
    describe_stuck_orders,
    mark_delivered_manually,
    tell_customer_refunded,
)
from app.services.delivery_failure_queue import NOTHING_TO_DISMISS, dismiss_delivery_failures
from app.services.ux20 import DeliveryNotRetryable, list_open_delivery_failures, list_stuck_paid_orders, retry_delivery
from app.services.users import get_all_settings

logger = logging.getLogger(__name__)
router = Router(name="stuck_orders")

PERMISSION = "orders"
PAGE_SIZE = 8
QUEUE_LIMIT = 100
ERROR_PREVIEW_LIMIT = 220
ID = r"(\d{1,12})"
PLATFORM_BACK = "adm:orders"
SHOP_BACK = "res:orders"
LIST_CALLBACK = "stk:l:0"
ENTRY_LABEL = "🚧 تحویل ناموفق و گیرکرده"

# kind -> (confirmation prompt, button label, action)
CLOSING_ACTIONS = {
    "rf": (
        "سفارش لغو و مبلغ پرداخت‌شده به کیف پول مشتری برگردانده شود؟\n"
        "قبلش مطمئن شوید در پاسارگارد چیزی برای این سفارش ساخته یا تمدید نشده است. این کار یک‌بار انجام می‌شود.",
        "✅ لغو و بازگشت وجه", cancel_and_refund,
    ),
    "md": (
        "تحویل را خودتان انجام داده‌اید (مثلاً سرویس را در پنل ساخته یا تمدید کرده‌اید)؟\n"
        "سفارش بدون جابه‌جایی پول بسته می‌شود.",
        "✅ ثبت تحویل دستی", mark_delivered_manually,
    ),
    "cl": (
        "سفارش بسته و از لیست حذف شود، بدون بازگشت وجه و بدون ارسال چیزی؟\n"
        "فقط وقتی که حساب این سفارش جای دیگری تسویه شده است.",
        "✅ بستن و حذف از لیست", close_without_refund,
    ),
}


async def stuck_staff(
    session: AsyncSession, db_user: BotUser, *, is_reseller_bot: bool, reseller_owner_id: int | None,
) -> dict | None:
    """Staff dict scoped to the actor's own shop (or the platform Owner); None when not allowed."""
    owner_id, profile = await load_reseller_actor(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if owner_id and profile:
        if not has_bot_perm(profile, PERMISSION):
            return None
        bridge = await resolve_bot_principal_bridge(
            session, db_user, is_reseller_bot=True, reseller_owner_id=reseller_owner_id
        )
        return bridge.staff if bridge and bridge.staff else {"role": "reseller", "bot_user_id": int(owner_id)}
    if is_reseller_bot or not await require_bot_owner(session, db_user, is_reseller_bot=False, notify=False):
        return None
    bridge = await resolve_bot_principal_bridge(session, db_user, is_reseller_bot=False)
    return bridge.staff if bridge and bridge.staff and is_platform_admin(bridge.staff) else None


def _shop_of(staff: dict) -> int | None:
    return None if is_platform_admin(staff) else int(staff.get("bot_user_id") or 0) or None


def _back(staff: dict) -> str:
    return PLATFORM_BACK if is_platform_admin(staff) else SHOP_BACK


def _actor(db_user: BotUser) -> str:
    return f"tg:{db_user.telegram_id}"


def entry_row(count: int) -> list[InlineKeyboardButton]:
    """The orders screens offer this row when ``count`` deliveries need attention."""
    return [InlineKeyboardButton(text=f"{ENTRY_LABEL} ({count})", callback_data=LIST_CALLBACK)]


async def _gate(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser, ctx: dict,
) -> dict | None:
    staff = await stuck_staff(session, db_user, **ctx)
    if staff is None:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
    return staff


async def _scoped_order(callback: CallbackQuery, session: AsyncSession, staff: dict, order_id: int) -> Order | None:
    order = await session.get(Order, order_id)
    try:
        if order is None:
            raise ValueError
        assert_order_retry_in_scope(staff, order)
    except (ValueError, ShopScopeError):
        await callback.answer(feedback.NOT_FOUND, show_alert=True)
        return None
    return order


async def _edit(callback: CallbackQuery, session: AsyncSession, text: str, rows: list, back: str) -> None:
    ui = await get_all_settings(session)
    markup = with_inline_back(InlineKeyboardMarkup(inline_keyboard=rows), ui, back)
    if callback.message:
        await safe_edit_text(callback.message, text, reply_markup=markup)


async def _queue(session: AsyncSession, shop_id: int | None) -> list[Order]:
    """Orders with an open failure or stuck without a service, newest first, one query per source."""
    failures = await list_open_delivery_failures(session, reseller_id=shop_id, limit=QUEUE_LIMIT)
    stuck = await list_stuck_paid_orders(session, reseller_id=shop_id, limit=QUEUE_LIMIT)
    ids = {int(f.order_id) for f in failures} | {int(o.id) for o in stuck}
    if not ids:
        return []
    rows = await session.scalars(select(Order).where(Order.id.in_(ids)).order_by(Order.id.desc()))
    return list(rows)


async def queue_count(session: AsyncSession, shop_id: int | None) -> int:
    return len(await _queue(session, shop_id))


async def _render_list(callback: CallbackQuery, session: AsyncSession, staff: dict, page: int) -> None:
    orders = await _queue(session, _shop_of(staff))
    if not orders:
        await _edit(callback, session, format_message("🚧 تحویل ناموفق و گیرکرده", "همهٔ تحویل‌ها سالم است؛ موردی نیاز به بررسی ندارد. ✅"), [], _back(staff))
        return
    last_page = (len(orders) - 1) // PAGE_SIZE
    page = min(page, last_page)
    rows = [
        [InlineKeyboardButton(
            text=f"#{o.id} · {order_status_fa(o.status)} · {format_toman(o.amount, get_settings().currency)}"[:64],
            callback_data=f"stk:v:{o.id}",
        )]
        for o in orders[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ قبل", callback_data=f"stk:l:{page - 1}"))
    if page < last_page:
        nav.append(InlineKeyboardButton(text="بعد ▶️", callback_data=f"stk:l:{page + 1}"))
    if nav:
        rows.append(nav)
    text = format_message(
        "🚧 تحویل ناموفق و گیرکرده",
        f"{len(orders)} سفارش نیاز به بررسی دارد. یکی را برای دیدن علت و اقدام انتخاب کنید.",
    )
    await _edit(callback, session, text, rows, _back(staff))


@router.callback_query(F.data.regexp(r"^stk:l:(\d{1,4})$"))
async def show_list(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    staff = await _gate(callback, session, db_user, {"is_reseller_bot": is_reseller_bot, "reseller_owner_id": reseller_owner_id})
    if staff is None:
        return
    await callback.answer()
    await _render_list(callback, session, staff, int(callback.data.split(":")[2]))


async def _card(callback: CallbackQuery, session: AsyncSession, staff: dict, order: Order) -> None:
    user = await session.get(BotUser, order.user_id)
    plan = await session.get(Plan, order.plan_id) if order.plan_id else None
    failure = await session.scalar(
        select(DeliveryFailure).where(DeliveryFailure.order_id == order.id, DeliveryFailure.resolved_at.is_(None))
        .order_by(DeliveryFailure.id.desc()).limit(1)
    )
    reason = (await describe_stuck_orders(session, [order])).get(int(order.id))
    still_stuck = await count_still_stuck(session, [int(order.id)]) > 0
    lines = [
        kv_line("🧾", "سفارش", f"#{order.id}"),
        kv_line("📌", "وضعیت", order_status_fa(order.status)),
        kv_line("👤", "مشتری", format_user_label(user)),
        kv_line("🏷", "پلن", html.escape(plan.name) if plan else "—"),
        kv_line("💰", "مبلغ", format_toman(order.amount, get_settings().currency)),
    ]
    if still_stuck and reason:
        lines += [kv_line("🔖", "نوع", html.escape(reason.kind)), kv_line("⚠️", "علت", html.escape(reason.reason))]
    if failure:
        lines.append(kv_line("🧪", "خطای آخر", html.escape(redact(failure.error, limit=ERROR_PREVIEW_LIMIT)) or "—"))
        lines.append(kv_line("🔁", "تعداد تلاش", str(int(failure.attempts or 1))))
    rows: list[list[InlineKeyboardButton]] = []
    if failure or still_stuck:
        rows.append([InlineKeyboardButton(text="♻️ تلاش مجدد تحویل", callback_data=f"stk:r:{order.id}")])
    if still_stuck:
        rows.append([InlineKeyboardButton(text="🛠 تحویل دستی انجام شد", callback_data=f"stk:c:md:{order.id}")])
        if reason is None or reason.can_refund:
            rows.append([InlineKeyboardButton(text="↩️ لغو و بازگشت وجه", callback_data=f"stk:c:rf:{order.id}")])
        rows.append([InlineKeyboardButton(text="🗑 بستن بدون بازگشت وجه", callback_data=f"stk:c:cl:{order.id}")])
    if failure:
        rows.append([InlineKeyboardButton(text="🙈 حذف از لیست خطاها", callback_data=f"stk:d:{order.id}")])
    links = customer_markup(user_id=order.user_id, service_id=order.service_id, shop_id=_shop_of(staff))
    if links is not None:
        rows.extend(links.inline_keyboard)
    await _edit(callback, session, format_message("🚧 بررسی تحویل", info_block(lines)), rows, LIST_CALLBACK)


@router.callback_query(F.data.regexp(rf"^stk:v:{ID}$"))
async def view_order(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    staff = await _gate(callback, session, db_user, {"is_reseller_bot": is_reseller_bot, "reseller_owner_id": reseller_owner_id})
    if staff is None:
        return
    order = await _scoped_order(callback, session, staff, int(callback.data.split(":")[2]))
    if order is None:
        return
    await callback.answer()
    await _card(callback, session, staff, order)


@router.callback_query(F.data.regexp(rf"^stk:r:{ID}$"))
async def retry_order(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    staff = await _gate(callback, session, db_user, {"is_reseller_bot": is_reseller_bot, "reseller_owner_id": reseller_owner_id})
    if staff is None:
        return
    order = await _scoped_order(callback, session, staff, int(callback.data.split(":")[2]))
    if order is None:
        return
    order_id = int(order.id)
    try:
        await retry_delivery(session, order_id)
    except DeliveryNotRetryable as exc:
        await callback.answer(str(exc)[:190], show_alert=True)
        return
    except ValueError as exc:
        await session.rollback()
        logger.warning("bot delivery retry refused order=%s", order_id, exc_info=True)
        await callback.answer(operator_error(exc, prefix="تلاش مجدد ناموفق", limit=150), show_alert=True)
        return
    await callback.answer("تحویل دوباره انجام شد ✅", show_alert=True)
    order = await session.get(Order, order_id)
    await session.refresh(order)
    await _card(callback, session, staff, order)


@router.callback_query(F.data.regexp(rf"^stk:d:{ID}$"))
async def dismiss_order(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    staff = await _gate(callback, session, db_user, {"is_reseller_bot": is_reseller_bot, "reseller_owner_id": reseller_owner_id})
    if staff is None:
        return
    order_id = int(callback.data.split(":")[2])
    dismissed, _ = await dismiss_delivery_failures(session, staff, [order_id])
    if not dismissed:
        await callback.answer(NOTHING_TO_DISMISS, show_alert=True)
        return
    await callback.answer("از لیست خطاها حذف شد")
    order = await session.get(Order, order_id)
    if order is not None and await count_still_stuck(session, [order_id]):
        await _card(callback, session, staff, order)
        return
    await _render_list(callback, session, staff, 0)


@router.callback_query(F.data.regexp(rf"^stk:c:(rf|md|cl):{ID}$"))
async def confirm_closing(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    staff = await _gate(callback, session, db_user, {"is_reseller_bot": is_reseller_bot, "reseller_owner_id": reseller_owner_id})
    if staff is None:
        return
    _, _, kind, raw_id = callback.data.split(":")
    order = await _scoped_order(callback, session, staff, int(raw_id))
    if order is None:
        return
    await callback.answer()
    prompt, label, _action = CLOSING_ACTIONS[kind]
    rows = [[InlineKeyboardButton(text=label, callback_data=f"stk:x:{kind}:{order.id}")]]
    await _edit(callback, session, format_message(f"⚠️ تأیید برای سفارش #{order.id}", html.escape(prompt)), rows, f"stk:v:{order.id}")


@router.callback_query(F.data.regexp(rf"^stk:x:(rf|md|cl):{ID}$"))
async def run_closing(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    staff = await _gate(callback, session, db_user, {"is_reseller_bot": is_reseller_bot, "reseller_owner_id": reseller_owner_id})
    if staff is None:
        return
    _, _, kind, raw_id = callback.data.split(":")
    action = CLOSING_ACTIONS[kind][2]
    try:
        result = await action(session, staff, int(raw_id), actor=_actor(db_user))
    except (ValueError, ShopScopeError) as exc:
        await session.rollback()
        await callback.answer(str(getattr(exc, "message", None) or exc)[:190], show_alert=True)
        return
    if kind == "rf":
        await tell_customer_refunded(session, result)
    done = {
        "rf": f"سفارش #{result.order_id} لغو شد" + (f" و {result.refunded:,} به کیف پول مشتری برگشت" if result.refunded else ""),
        "md": f"سفارش #{result.order_id} به‌عنوان تحویل دستی ثبت شد",
        "cl": f"سفارش #{result.order_id} بدون بازگشت وجه بسته شد",
    }[kind]
    await callback.answer(done[:190], show_alert=True)
    await _render_list(callback, session, staff, 0)
