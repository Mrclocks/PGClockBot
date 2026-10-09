"""Operator-priced cancellations, serialized with paid service mutations."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.db.models import BotUser, Order, ServiceAutomation, ServiceCancellation, ShopWallet, UserService
from app.services.pasarguard import get_pg, get_pg_for_reseller
from app.services.service_automation import service_shop_id
from app.services.wallet import credit_wallet

STATUS_LABELS = {"pending": "در انتظار بررسی", "processing": "در حال غیرفعال‌سازی", "review": "نیازمند بررسی و تلاش مجدد", "approved": "لغو شد؛ اعتبار به کیف پول همین فروشگاه برگشت", "rejected": "درخواست رد شد", "withdrawn": "درخواست پس گرفته شد"}
# Operator-facing queue — badge + list filter for items needing human action.
OPEN_OPERATOR_STATUSES = ("pending", "review")


async def lock_service_mutation(session, service_id):
    """Lock the service row in the caller's transaction before claiming an order."""
    result = await session.execute(update(UserService).where(
        UserService.id == service_id, UserService.is_cancelled.is_(False),
        UserService.cancellation_pending.is_(False),
    ).values(is_cancelled=False).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise ValueError("این سرویس لغو شده یا لغو آن در حال بررسی است")


async def owned_cancellation_service(session, user, service_id, shop_id):
    service = await session.get(UserService, service_id)
    if not service or service.bot_user_id != user.id or user.is_blocked:
        raise ValueError("سرویس یافت نشد")
    if not service.pg_user_id or (service.remark or "").strip() == "linked":
        raise ValueError("لغو این سرویس از این مسیر ممکن نیست")
    if await service_shop_id(session, service) != shop_id:
        raise ValueError("درخواست را از فروشگاه خود سرویس ثبت کنید")
    return service


async def cancellation_status(session, user, service_id, *, shop_id):
    await owned_cancellation_service(session, user, service_id, shop_id)
    row = await session.scalar(select(ServiceCancellation).where(
        ServiceCancellation.service_id == service_id,
        ServiceCancellation.user_id == user.id,
        ServiceCancellation.reseller_id == shop_id,
    ).order_by(ServiceCancellation.id.desc()).limit(1))
    return {"id": row.id, "status": row.status, "label": STATUS_LABELS[row.status],
            "reason": row.reason, "refund_amount": row.refund_amount if row.status == "approved" else None,
            "operator_note": row.operator_note} if row else None


async def request_cancellation(session, user, service_id, *, shop_id, reason):
    user_id = int(user.id)
    reason = str(reason or "").strip()
    if not reason or len(reason) > 1000:
        raise ValueError("دلیل لغو را بین ۱ تا ۱۰۰۰ نویسه بنویسید")
    service = await owned_cancellation_service(session, user, service_id, shop_id)
    existing = await cancellation_status(session, user, service_id, shop_id=shop_id)
    if existing and existing["status"] in {"pending", "processing", "review", "approved"}:
        return existing
    row = ServiceCancellation(service_id=service.id, user_id=user.id, reseller_id=shop_id,
                              reason=reason, pg_user_id=service.pg_user_id)
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        user = await session.get(BotUser, user_id)
    status = await cancellation_status(session, user, service_id, shop_id=shop_id)
    if status is None:
        raise ValueError("درخواست قبلی این سرویس نیاز به بررسی پشتیبانی دارد")
    return status


def assert_cancellation_scope(row, shop_id):
    if not row or row.reseller_id != shop_id:
        raise ValueError("درخواست یافت نشد")


async def pending_cancellation_count(session, shop_id) -> int:
    """Count open cancellation requests for one shop (platform shop_id is None)."""
    return int(
        await session.scalar(
            select(func.count())
            .select_from(ServiceCancellation)
            .where(
                ServiceCancellation.reseller_id == shop_id,
                ServiceCancellation.status.in_(OPEN_OPERATOR_STATUSES),
            )
        )
        or 0
    )


async def has_pending_cancellations(session, shop_id) -> bool:
    return (await pending_cancellation_count(session, shop_id)) > 0


async def list_cancellation_requests(session, shop_id, *, before: int | None = None, limit: int = 50):
    """Shop-scoped cancellation rows newest-first. Returns (rows, next_before)."""
    limit = max(1, min(int(limit), 100))
    query = (
        select(ServiceCancellation, UserService.pg_username)
        .join(UserService, UserService.id == ServiceCancellation.service_id)
        .where(ServiceCancellation.reseller_id == shop_id)
    )
    if before is not None:
        query = query.where(ServiceCancellation.id < int(before))
    rows = list(
        (
            await session.execute(
                query.order_by(ServiceCancellation.id.desc()).limit(limit + 1)
            )
        ).all()
    )
    next_before = int(rows[limit - 1][0].id) if len(rows) > limit else None
    return rows[:limit], next_before


async def reject_cancellation(session, request_id, *, shop_id, actor, note):
    row = await session.get(ServiceCancellation, request_id)
    assert_cancellation_scope(row, shop_id)
    if len(note) > 1000:
        raise ValueError("توضیح اپراتور طولانی است")
    result = await session.execute(update(ServiceCancellation).where(
        ServiceCancellation.id == request_id, ServiceCancellation.status == "pending",
    ).values(status="rejected", operator_note=note.strip(), processed_by=actor,
             processed_at=datetime.now(timezone.utc)))
    if result.rowcount != 1:
        raise ValueError("فقط درخواست بررسی‌نشده قابل رد است")
    await session.commit()


async def approve_cancellation(session, request_id, *, shop_id, amount, actor, note=""):
    if isinstance(amount, bool) or not isinstance(amount, int) or not 0 <= amount <= 2**31 - 1 or len(note) > 1000:
        raise ValueError("مبلغ یا توضیح اعتبار برگشتی نامعتبر است")
    row = await session.get(ServiceCancellation, request_id)
    assert_cancellation_scope(row, shop_id)
    if row.status == "approved":
        return row
    if row.refund_amount is not None and row.refund_amount != amount:
        raise ValueError("در تلاش مجدد، مبلغ تأییدشده قابل تغییر نیست")
    balance_query = select(ShopWallet.balance).where(ShopWallet.user_id == row.user_id, ShopWallet.reseller_id == shop_id) if shop_id else select(BotUser.wallet_balance).where(BotUser.id == row.user_id)
    if int(await session.scalar(balance_query) or 0) + amount > 2**31 - 1:
        raise ValueError("مجموع اعتبار برگشتی و موجودی از سقف کیف پول بیشتر است")
    service = await session.get(UserService, row.service_id)
    if (not service or service.bot_user_id != row.user_id or service.pg_user_id != row.pg_user_id
            or await service_shop_id(session, service) != shop_id):
        raise ValueError("مالکیت یا شناسهٔ پنل سرویس تغییر کرده است")
    now, token = datetime.now(timezone.utc), secrets.token_hex(16)
    claim = await session.execute(update(ServiceCancellation).where(
        ServiceCancellation.id == request_id,
        ServiceCancellation.status.in_(["pending", "processing", "review"]),
        or_(ServiceCancellation.refund_amount.is_(None), ServiceCancellation.refund_amount == amount),
        or_(ServiceCancellation.locked_until.is_(None), ServiceCancellation.locked_until < now),
    ).values(status="processing", refund_amount=amount, operator_note=note.strip(),
             processed_by=actor, lock_token=token, locked_until=now + timedelta(minutes=5))
         .execution_options(synchronize_session=False))
    if claim.rowcount != 1:
        await session.rollback()
        raise ValueError("این درخواست در حال پردازش است یا بسته شده است")
    # Both cancellation and quota delivery take this row lock before checking orders.
    await session.execute(update(UserService).where(UserService.id == service.id).values(cancellation_pending=True))
    busy = await session.scalar(select(Order.id).where(
        Order.service_id == service.id,
        or_(Order.service_mutation_pending.is_(True), Order.status.not_in(["delivered", "rejected", "cancelled"])),
    ).limit(1))
    if busy:
        await session.rollback()
        raise ValueError("سفارش باز سرویس باید پیش از لغو تعیین تکلیف شود")
    await session.commit()
    try:
        pg = await get_pg_for_reseller(session, shop_id) if shop_id else get_pg()
        info = await pg.get_user_by_id(row.pg_user_id)
        if int(info.get("id") or 0) != row.pg_user_id:
            raise ValueError("پاسخ پنل با سرویس همخوان نیست")
        if str(info.get("status") or "").lower() != "disabled":
            await pg.set_disabled_by_id(row.pg_user_id, True)
            info = await pg.get_user_by_id(row.pg_user_id)
            if int(info.get("id") or 0) != row.pg_user_id or str(info.get("status") or "").lower() != "disabled":
                raise ValueError("غیرفعال‌شدن سرویس در پنل تأیید نشد")
        sealed = await session.execute(update(ServiceCancellation).where(
            ServiceCancellation.id == request_id, ServiceCancellation.lock_token == token,
            ServiceCancellation.status == "processing",
        ).values(status="approved", processed_at=datetime.now(timezone.utc), lock_token=None, locked_until=None)
             .execution_options(synchronize_session=False))
        if sealed.rowcount != 1:
            await session.rollback()
            raise ValueError("نتیجه نیاز به بررسی اپراتور دارد")
        user = await session.get(BotUser, row.user_id)
        if amount:
            await credit_wallet(session, user, amount, f"اعتبار لغو سرویس #{service.id} (درخواست #{request_id})", shop_id=shop_id, commit=False)
        await session.execute(update(UserService).where(UserService.id == service.id).values(is_cancelled=True, cancellation_pending=False))
        await session.execute(update(ServiceAutomation).where(ServiceAutomation.service_id == service.id).values(
            renew_enabled=False, duration_enabled=False, volume_enabled=False))
        await session.commit()
        await session.refresh(row)
        return row
    except Exception:
        await session.rollback()
        await session.execute(update(ServiceCancellation).where(
            ServiceCancellation.id == request_id, ServiceCancellation.lock_token == token,
        ).values(status="review", lock_token=None, locked_until=None).execution_options(synchronize_session=False))
        await session.commit()
        raise ValueError("نتیجهٔ پنل نامشخص است؛ اعتبار واریز نشده و درخواست برای تلاش مجدد محفوظ است") from None
