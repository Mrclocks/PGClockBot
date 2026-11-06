"""Tell the customer how their service cancellation request was decided.

Sent once, at the single moment the status moves to approved or rejected (the atomic claims in
``service_cancellations`` guarantee one such moment); a repeated call or tap never reaches here.
Delivery is best effort: ``notify_payer`` swallows Telegram failures, and a lost message never
changes the decision.
"""
from __future__ import annotations

import html

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import ServiceCancellation, UserService
from app.services.formatting import format_toman
from app.services.payer_notify import notify_payer

APPROVED_TITLE = "✅ لغو سرویس انجام شد"
REJECTED_TITLE = "❌ درخواست لغو پذیرفته نشد"
NOTE_LIMIT = 300


def decision_notice(
    status: str, service_name: str, *, refund_amount: int | None, note: str, currency: str,
) -> tuple[str, str] | None:
    """(title, body) for a decided request; ``None`` for any other status."""
    name = html.escape(service_name)
    if status == "approved":
        body = f"درخواست لغو سرویس <code>{name}</code> تأیید شد و سرویس غیرفعال شد."
        if refund_amount:
            body += f"\n💰 {format_toman(int(refund_amount), currency)} به کیف پول شما برگشت."
        return APPROVED_TITLE, body
    if status == "rejected":
        body = f"درخواست لغو سرویس <code>{name}</code> پذیرفته نشد؛ سرویس شما همچنان فعال است."
        if note.strip():
            body += f"\n📝 {html.escape(note.strip()[:NOTE_LIMIT])}"
        return REJECTED_TITLE, body
    return None


async def notify_cancellation_decision(session: AsyncSession, request_id: int) -> bool:
    """Send the decision notice of ``request_id`` through the bot of the request's own shop."""
    found = (await session.execute(
        select(ServiceCancellation, UserService.pg_username)
        .outerjoin(UserService, UserService.id == ServiceCancellation.service_id)
        .where(ServiceCancellation.id == int(request_id))
    )).one_or_none()
    if found is None:
        return False
    row, service_name = found
    notice = decision_notice(
        row.status, service_name or f"#{row.service_id}", refund_amount=row.refund_amount,
        note=row.operator_note or "", currency=get_settings().currency,
    )
    if notice is None:
        return False
    title, body = notice
    return await notify_payer(session, int(row.user_id), title=title, body=body, shop_id=row.reseller_id)
