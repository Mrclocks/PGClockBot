"""Customer and service details shown with a cancellation request in staff messages."""
from __future__ import annotations

import html
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, Plan, ServiceCancellation, UserService
from app.services.numbers import to_fa_digits
from app.services.service_cancellation_settings import AUTO_ACTOR
from app.services.formatting import (
    format_bytes,
    format_expire,
    format_toman,
    format_user_label,
    info_block,
    kv_line,
)

REASON_PREVIEW_LIMIT = 800
UNLIMITED_LABEL = "نامحدود"
UNDECIDED_STATUSES = ("pending", "processing", "review")


def _volume_label(service: UserService) -> str:
    if service.quota_synced_at is None or service.quota_data_limit_bytes is None:
        return "—"
    if int(service.quota_data_limit_bytes) == 0:
        return UNLIMITED_LABEL
    return format_bytes(int(service.quota_data_limit_bytes))


def _expire_label(service: UserService) -> str:
    if service.quota_synced_at is None:
        return "—"
    if service.quota_expire_at is None:
        return UNLIMITED_LABEL
    expire = service.quota_expire_at
    if expire.tzinfo is None:
        expire = expire.replace(tzinfo=timezone.utc)
    return format_expire(expire)


def _created_label(created_at: datetime | None) -> str:
    if created_at is None:
        return "—"
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return created_at.astimezone().strftime("%Y/%m/%d %H:%M")


def cancellation_detail_lines(
    row: ServiceCancellation,
    user: BotUser | None,
    service: UserService | None,
    plan_name: str | None,
    *,
    currency: str,
) -> str:
    """Request, customer and service facts as one RTL info block (HTML-safe)."""
    lines = [kv_line("🧾", "درخواست", f"#{row.id}")]
    if user is not None:
        lines.append(kv_line("👤", "مشتری", format_user_label(user)))
    if service is not None:
        lines.append(kv_line("📦", "سرویس", f"<code>{html.escape(service.pg_username)}</code> (#{service.id})"))
        if plan_name:
            lines.append(kv_line("🏷", "پلن", html.escape(plan_name)))
        lines.append(kv_line("📊", "حجم", _volume_label(service)))
        lines.append(kv_line("⏳", "انقضا", _expire_label(service)))
    lines.append(kv_line("🕒", "زمان ثبت", _created_label(row.created_at)))
    if row.refund_amount is not None and row.status != "pending":
        lines.append(kv_line("💰", "اعتبار تعیین‌شده", format_toman(int(row.refund_amount), currency)))
    elif row.status in UNDECIDED_STATUSES and row.suggested_refund is not None:
        lines.append(kv_line("💡", "مبلغ پیشنهادی", suggestion_label(row, currency)))
    if row.processed_by == AUTO_ACTOR:
        lines.append(kv_line("🤖", "تأیید", "خودکار، طبق تنظیمات «لغو سرویس»"))
    return info_block(lines)


def suggestion_label(row: ServiceCancellation, currency: str) -> str:
    """The amount the shop's refund rule gave when the request was made, with what it was based on."""
    label = format_toman(int(row.suggested_refund or 0), currency)
    if row.remaining_percent is not None and row.paid_amount is not None:
        label += (
            f" ({to_fa_digits(int(row.remaining_percent))}٪ باقی‌مانده از "
            f"{format_toman(int(row.paid_amount), currency)})"
        )
    return label


def reason_block(reason: str | None) -> str:
    return f"💬 <b>دلیل مشتری:</b>\n{html.escape((reason or '')[:REASON_PREVIEW_LIMIT])}"


async def load_cancellation_parts(
    session: AsyncSession, row: ServiceCancellation
) -> tuple[BotUser | None, UserService | None, str | None]:
    """Customer, service and plan name of ``row`` in a single query."""
    found = (await session.execute(
        select(BotUser, UserService, Plan.name)
        .select_from(ServiceCancellation)
        .join(BotUser, BotUser.id == ServiceCancellation.user_id)
        .outerjoin(UserService, UserService.id == ServiceCancellation.service_id)
        .outerjoin(Plan, Plan.id == UserService.plan_id)
        .where(ServiceCancellation.id == row.id)
    )).one_or_none()
    return (None, None, None) if found is None else (found[0], found[1], found[2])
