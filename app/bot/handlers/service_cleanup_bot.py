"""Service cleanup in the bot (web «پاکسازی سرویس‌ها»): the rule, the services near deletion, holds and exemptions.

The rule itself (switches, days, warnings) stays on the web form; the bot shows it and links there.
Each service card offers «مهلت بیشتر» and «معاف از پاکسازی». Both ask for a reason and then go through
``service_cleanup_manage``, which re-checks the service belongs to the actor's scope in the same UPDATE
and writes the staff log in that transaction. The platform owner works on platform services from the
main bot; a shop owner or staff with ``service_cleanup`` on their shop's services from the shop bot.
"""

from __future__ import annotations

import html
from dataclasses import dataclass

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.feature_staff import resolve_feature_staff
from app.bot.handlers.action_reason import ask_reason, take_reason
from app.bot.marketing_panels import WEB_BUTTON_LABEL, panel_page_url
from app.bot.nav_inline import with_inline_back
from app.bot.service_cleanup_entries import SERVICE_CLEANUP_CALLBACK
from app.bot.tg_utils import safe_edit_text
from app.db.models import BotUser
from app.services import bot_feedback_texts as feedback
from app.services.formatting import format_message, format_number, info_block, kv_line
from app.services.service_cleanup import (
    CleanupPolicy,
    CleanupPolicyError,
    CleanupPreview,
    PreviewRow,
    build_preview,
    cleanup_scope,
    load_policy,
)
from app.services.service_cleanup_manage import (
    AuditEntry,
    ExemptRow,
    extend_deadline,
    list_exempt,
    parse_hold_days,
    set_exempt,
)
from app.services.shop_scope import is_platform_admin, resolve_shop_scope_id
from app.services.staff_audit import actor_from_staff
from app.services.user_errors import domain_error_text
from app.services.users import get_all_settings

router = Router(name="service_cleanup_bot")

PERMISSION = "service_cleanup"
WEB_PATH = "/service-cleanup"
PLATFORM_BACK = "nv:adm:system"
SHOP_BACK = "nv:res:grp:customers"
EXEMPT_CALLBACK = "scl:x"
HOLD_DAY_CHOICES: tuple[int, ...] = (3, 7, 14, 30)
ID = r"(\d{1,12})"
TITLE = "🗑 پاکسازی سرویس‌ها"
NOT_LISTED_TEXT = "این سرویس در فهرست پاکسازی نیست؛ فهرست را دوباره باز کنید"


@dataclass(frozen=True, slots=True)
class CleanupActor:
    staff: dict
    shop_id: int | None

    @property
    def back(self) -> str:
        return PLATFORM_BACK if self.shop_id is None else SHOP_BACK

    def customer_callback(self, user_id: int) -> str:
        return f"adm:users:view:{user_id}" if is_platform_admin(self.staff) else f"res:user:{user_id}"


async def _actor(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser, *, is_reseller_bot: bool, reseller_owner_id: int | None
) -> CleanupActor | None:
    """The actor and its cleanup scope, or ``None`` after telling the user they may not."""
    staff = await resolve_feature_staff(
        session, db_user, PERMISSION, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    try:
        shop_id = cleanup_scope(staff) if staff else None
    except PermissionError:
        staff = None
    if not staff:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return None
    return CleanupActor(staff=staff, shop_id=shop_id)


async def _edit(callback: CallbackQuery, session: AsyncSession, text: str, rows: list, back: str) -> None:
    ui = await get_all_settings(session)
    markup = with_inline_back(InlineKeyboardMarkup(inline_keyboard=rows), ui, back)
    if callback.message:
        await safe_edit_text(callback.message, text, reply_markup=markup)


def _rule_line(on: bool, days: int, label: str) -> str:
    if not on:
        return kv_line("⏸", label, "خاموش")
    return kv_line("✅", label, f"{format_number(days)} روز بعد حذف می‌شود")


def _summary_text(policy: CleanupPolicy, preview: CleanupPreview) -> str:
    lines = [
        _rule_line(policy.time_on, policy.time_days, "سرویس منقضی"),
        _rule_line(policy.volume_on, policy.volume_days, "حجم تمام‌شده"),
        kv_line("⛔", "منقضی", format_number(preview.expired_total)),
        kv_line("📉", "حجم تمام‌شده", format_number(preview.volume_total)),
        kv_line("🗑", "موعد حذف رسیده", format_number(preview.time_due_now + preview.volume_due_now)),
        kv_line("⏳", "حذف در ۷ روز آینده", format_number(preview.time_due_soon + preview.volume_due_soon)),
    ]
    tail = "سرویس‌های نزدیک حذف در پایین آمده‌اند؛ برای مهلت بیشتر یا معافیت یکی را باز کنید." if preview.rows else ""
    note = html.escape(preview.cache_note)
    return format_message(TITLE, "\n\n".join(part for part in (info_block(lines), tail, f"<i>{note}</i>") if part))


def _row_button(row: PreviewRow) -> list[InlineKeyboardButton]:
    when = "امروز" if row.days_left <= 0 else f"{format_number(row.days_left)} روز"
    return [InlineKeyboardButton(text=f"{row.kind} · {row.username} · {when}"[:64], callback_data=f"scl:v:{row.service_id}")]


@router.callback_query(F.data == SERVICE_CLEANUP_CALLBACK)
async def show_summary(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    actor = await _actor(callback, session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if actor is None:
        return
    await callback.answer()
    await _render_summary(callback, session, actor)


async def _render_summary(callback: CallbackQuery, session: AsyncSession, actor: CleanupActor) -> None:
    policy = await load_policy(session, shop_id=actor.shop_id)
    preview = await build_preview(session, shop_id=actor.shop_id, policy=policy)
    rows = [_row_button(row) for row in preview.rows]
    rows.append([InlineKeyboardButton(text="🛡 سرویس‌های معاف", callback_data=EXEMPT_CALLBACK)])
    web_url = panel_page_url(WEB_PATH)
    if web_url:
        rows.append([InlineKeyboardButton(text=WEB_BUTTON_LABEL, url=web_url)])
    await _edit(callback, session, _summary_text(policy, preview), rows, actor.back)


async def _find_row(session: AsyncSession, actor: CleanupActor, service_id: int) -> PreviewRow | None:
    policy = await load_policy(session, shop_id=actor.shop_id)
    preview = await build_preview(session, shop_id=actor.shop_id, policy=policy)
    return next((row for row in preview.rows if row.service_id == service_id), None)


def _card_text(row: PreviewRow) -> str:
    lines = [
        kv_line("📦", "سرویس", f"<code>{html.escape(row.username)}</code> (#{row.service_id})"),
        kv_line("👤", "مشتری", html.escape(row.customer)),
        kv_line("🔖", "دلیل", html.escape(row.kind)),
        kv_line("📅", "از", f"<code>{html.escape(row.since)}</code>"),
        kv_line("🗑", "موعد حذف", f"<code>{html.escape(row.deadline)}</code>"),
    ]
    if row.retrying:
        lines.append(kv_line("🔁", "وضعیت", "حذف یک بار ناموفق بود و دوباره امتحان می‌شود"))
    return format_message(f"{TITLE} — #{row.service_id}", info_block(lines))


@router.callback_query(F.data.regexp(rf"^scl:v:{ID}$"))
async def show_service(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    actor = await _actor(callback, session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if actor is None:
        return
    row = await _find_row(session, actor, int(callback.data.split(":")[2]))
    if row is None:
        await callback.answer(NOT_LISTED_TEXT, show_alert=True)
        return
    await callback.answer()
    sid = row.service_id
    rows = [
        [InlineKeyboardButton(text="⏳ مهلت بیشتر", callback_data=f"scl:d:{sid}")],
        [InlineKeyboardButton(text="🛡 معاف از پاکسازی", callback_data=f"scl:E:{sid}:1")],
        [InlineKeyboardButton(text="👤 کارت مشتری", callback_data=actor.customer_callback(row.user_id))],
    ]
    await _edit(callback, session, _card_text(row), rows, SERVICE_CLEANUP_CALLBACK)


@router.callback_query(F.data.regexp(rf"^scl:d:{ID}$"))
async def choose_hold_days(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    actor = await _actor(callback, session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if actor is None:
        return
    sid = int(callback.data.split(":")[2])
    await callback.answer()
    rows = [
        [
            InlineKeyboardButton(text=f"{format_number(days)} روز", callback_data=f"scl:H:{sid}:{days}")
            for days in HOLD_DAY_CHOICES
        ]
    ]
    text = format_message(
        "⏳ مهلت بیشتر",
        "این تعداد روز به موعد حذف فعلی اضافه می‌شود و مشتری پیش از حذف دوباره هشدار می‌گیرد.",
    )
    await _edit(callback, session, text, rows, f"scl:v:{sid}")


def _audit(actor: CleanupActor, action: str, reason: str) -> AuditEntry:
    return AuditEntry(actor_from_staff(actor.staff, shop_id=resolve_shop_scope_id(actor.staff)), action, reason)


@router.callback_query(F.data.regexp(rf"^scl:H:{ID}:(\d{{1,3}})$"))
async def run_hold(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser, state: FSMContext | None = None,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    actor = await _actor(callback, session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if actor is None:
        return
    _, _, raw_id, raw_days = callback.data.split(":")
    try:
        days = parse_hold_days(raw_days)
    except CleanupPolicyError as exc:
        await callback.answer(domain_error_text(exc), show_alert=True)
        return
    reason = await take_reason(state, callback)
    if reason is None:
        await ask_reason(callback, state, label="علت تمدید مهلت حذف")
        return
    policy = await load_policy(session, shop_id=actor.shop_id)
    audit = _audit(actor, "service_cleanup_hold", reason)
    try:
        until = await extend_deadline(
            session, shop_id=actor.shop_id, service_id=int(raw_id), days=days, policy=policy, audit=audit
        )
    except ValueError as exc:
        await callback.answer(domain_error_text(exc), show_alert=True)
        return
    await callback.answer(f"مهلت حذف تا {until} تمدید شد ✅", show_alert=True)
    await _render_summary(callback, session, actor)


@router.callback_query(F.data.regexp(rf"^scl:E:{ID}:([01])$"))
async def run_exempt(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser, state: FSMContext | None = None,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    actor = await _actor(callback, session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if actor is None:
        return
    _, _, raw_id, raw_keep = callback.data.split(":")
    keep = raw_keep == "1"
    reason = await take_reason(state, callback)
    if reason is None:
        await ask_reason(callback, state, label="علت معافیت از پاکسازی" if keep else "علت برگرداندن به پاکسازی")
        return
    audit = _audit(actor, "service_cleanup_exempt" if keep else "service_cleanup_unexempt", reason)
    try:
        await set_exempt(session, shop_id=actor.shop_id, service_id=int(raw_id), exempt=keep, audit=audit)
    except ValueError as exc:
        await callback.answer(domain_error_text(exc), show_alert=True)
        return
    await callback.answer(
        "سرویس از پاکسازی معاف شد ✅" if keep else "سرویس دوباره در پاکسازی قرار گرفت ✅", show_alert=True
    )
    await _render_summary(callback, session, actor)


def _exempt_button(row: ExemptRow) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text=f"↩️ {row.username} · {row.customer}"[:64], callback_data=f"scl:E:{row.service_id}:0")]


@router.callback_query(F.data == EXEMPT_CALLBACK)
async def show_exempt(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None,
) -> None:
    actor = await _actor(callback, session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id)
    if actor is None:
        return
    await callback.answer()
    exempt = await list_exempt(session, shop_id=actor.shop_id)
    body = (
        f"{format_number(len(exempt))} سرویس هرگز پاک نمی‌شود. برای برگرداندن یکی به پاکسازی، آن را بزنید."
        if exempt
        else "سرویس معافی ثبت نشده است."
    )
    rows = [_exempt_button(row) for row in exempt]
    await _edit(callback, session, format_message("🛡 سرویس‌های معاف", body), rows, SERVICE_CLEANUP_CALLBACK)
