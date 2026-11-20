"""A customer hands a service to another customer of the same shop through a one-time link.

The owner creates a link (only the token's SHA-256 is stored) that expires after the shop's hours.
A customer who opens it in the same shop bot sees what they receive and accepts. Acceptance is one
transaction: the transfer is claimed (``open`` -> ``accepted`` where it has not expired), the
service row moves only if it still belongs to the sender, the service's automatic purchases are
switched off (they would spend the new owner's wallet without consent), and the sender's notice is
queued. After the commit the subscription link is rotated on the panel when the shop asks for it,
so the sender can no longer connect; a failure there is logged and the new owner still has the
service.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, ServiceAutomation, ServiceTransfer, UserService
from app.services.customer_notice_queue import add_notice
from app.services.pasarguard import (
    PasarGuardError,
    extract_sub_token,
    get_pg,
    get_pg_for_reseller,
    user_subscription_url,
)
from app.services.service_freeze import can_be_frozen as is_self_service, has_open_order, open_freeze
from app.services.service_scope import service_shop_id
from app.services.service_transfer_settings import (
    DONE_ACTION,
    PERIOD,
    STATUS_ACCEPTED,
    STATUS_CANCELLED,
    STATUS_EXPIRED,
    STATUS_OPEN,
    transfer_policy,
)

logger = logging.getLogger(__name__)

TOKEN_BYTES = 16
NOT_FOUND = "سرویس یافت نشد"
LINK_GONE = "این لینک انتقال دیگر معتبر نیست"


@dataclass(frozen=True, slots=True)
class TransferOffer:
    transfer_id: int
    service_id: int
    service_name: str
    expires_at: datetime


BUTTON_START = "start"
BUTTON_CANCEL = "cancel"


async def transfer_button(session: AsyncSession, service: UserService, ui: Mapping[str, Any]) -> str | None:
    """Which transfer button the service's «⚙️ بیشتر» shows, if any."""
    if not is_self_service(service):
        return None
    row = await open_transfer(session, int(service.id))
    if row is not None and row.expires_at > _now():
        return BUTTON_CANCEL
    return BUTTON_START if transfer_policy(ui).allows_plan(service.plan_id) else None


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def open_transfer(session: AsyncSession, service_id: int) -> ServiceTransfer | None:
    return await session.scalar(
        select(ServiceTransfer).where(ServiceTransfer.service_id == int(service_id), ServiceTransfer.status == STATUS_OPEN)
    )


async def _expire_stale(session: AsyncSession, service_id: int, now: datetime) -> None:
    await session.execute(
        update(ServiceTransfer).where(
            ServiceTransfer.service_id == int(service_id), ServiceTransfer.status == STATUS_OPEN,
            ServiceTransfer.expires_at <= now,
        ).values(status=STATUS_EXPIRED).execution_options(synchronize_session=False)
    )


async def _assert_transferable(session: AsyncSession, service: UserService) -> None:
    if not is_self_service(service):
        raise ValueError("این سرویس را نمی‌توان منتقل کرد")
    if await open_freeze(session, int(service.id)) is not None:
        raise ValueError("سرویس متوقف است؛ اول آن را از حالت توقف خارج کنید")
    # An unpaid or undelivered order would later charge the sender or apply to the new owner's service.
    if await has_open_order(session, int(service.id)):
        raise ValueError("یک سفارش باز روی این سرویس هست؛ بعد از پایان یا لغو آن دوباره تلاش کنید")


async def _owned(session: AsyncSession, user: BotUser, service_id: int, shop_id: int | None) -> UserService:
    service = await session.get(UserService, int(service_id))
    if service is None or int(service.bot_user_id) != int(user.id) or user.is_blocked:
        raise ValueError(NOT_FOUND)
    if await service_shop_id(session, service) != shop_id:
        raise ValueError(NOT_FOUND)
    return service


async def _accepted_in_period(session: AsyncSession, service_id: int, now: datetime) -> int:
    count = await session.scalar(
        select(func.count(ServiceTransfer.id)).where(
            ServiceTransfer.service_id == int(service_id), ServiceTransfer.status == STATUS_ACCEPTED,
            ServiceTransfer.accepted_at >= now - PERIOD,
        )
    )
    return int(count or 0)


async def create_transfer(
    session: AsyncSession, user: BotUser, service_id: int, *, shop_id: int | None, ui: Mapping[str, Any]
) -> tuple[ServiceTransfer, str]:
    """A new one-time link for the owner's service: (row, token). The token is shown once."""
    policy = transfer_policy(ui)
    if not policy.enabled:
        raise ValueError("انتقال سرویس در این فروشگاه فعال نیست")
    service = await _owned(session, user, service_id, shop_id)
    if not policy.plans.allows(service.plan_id):
        raise ValueError("انتقال برای پلن این سرویس فعال نیست")
    await _assert_transferable(session, service)
    now = _now()
    if await _accepted_in_period(session, int(service.id), now) >= policy.per_period:
        raise ValueError("سقف انتقال این سرویس در ۳۰ روز گذشته پر شده است")
    await _expire_stale(session, int(service.id), now)
    token = secrets.token_urlsafe(TOKEN_BYTES)
    row = ServiceTransfer(
        service_id=int(service.id), shop_id=shop_id, from_user_id=int(user.id), token_hash=token_hash(token),
        status=STATUS_OPEN, expires_at=now + timedelta(hours=policy.link_hours),
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        raise ValueError("برای این سرویس یک لینک انتقال باز هست؛ اول آن را لغو کنید") from None
    await session.commit()
    return row, token


async def cancel_transfer(session: AsyncSession, user: BotUser, service_id: int, *, shop_id: int | None) -> bool:
    """Cancel the owner's open link; False when there was none."""
    await _owned(session, user, service_id, shop_id)
    result = await session.execute(
        update(ServiceTransfer).where(
            ServiceTransfer.service_id == int(service_id), ServiceTransfer.from_user_id == int(user.id),
            ServiceTransfer.status == STATUS_OPEN,
        ).values(status=STATUS_CANCELLED).execution_options(synchronize_session=False)
    )
    await session.commit()
    return result.rowcount == 1


async def _live_transfer(session: AsyncSession, token: str, shop_id: int | None) -> ServiceTransfer:
    row = await session.scalar(select(ServiceTransfer).where(ServiceTransfer.token_hash == token_hash(token or "")))
    if row is None or row.status != STATUS_OPEN or row.expires_at <= _now() or row.shop_id != shop_id:
        raise ValueError(LINK_GONE)
    return row


async def preview_transfer(
    session: AsyncSession, recipient: BotUser, token: str, *, shop_id: int | None
) -> TransferOffer:
    """What the link gives, for the confirmation screen; raises ValueError for a dead link."""
    row = await _live_transfer(session, token, shop_id)
    if int(row.from_user_id) == int(recipient.id):
        raise ValueError("این لینک را خودتان ساخته‌اید؛ آن را برای کس دیگری بفرستید")
    service = await session.get(UserService, int(row.service_id))
    if service is None:
        raise ValueError(LINK_GONE)
    return TransferOffer(int(row.id), int(service.id), service.pg_username or f"#{service.id}", row.expires_at)


async def _claim(
    session: AsyncSession, row: ServiceTransfer, recipient: BotUser, now: datetime, *, rotate: bool
) -> None:
    claimed = await session.execute(
        update(ServiceTransfer).where(
            ServiceTransfer.id == int(row.id), ServiceTransfer.status == STATUS_OPEN, ServiceTransfer.expires_at > now,
        ).values(status=STATUS_ACCEPTED, to_user_id=int(recipient.id), accepted_at=now, link_rotation_pending=rotate)
        .execution_options(synchronize_session=False)
    )
    moved = await session.execute(
        update(UserService).where(
            UserService.id == int(row.service_id), UserService.bot_user_id == int(row.from_user_id),
            UserService.is_cancelled.is_(False), UserService.cancellation_pending.is_(False),
        ).values(bot_user_id=int(recipient.id)).execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1 or moved.rowcount != 1:
        await session.rollback()
        raise ValueError(LINK_GONE)


async def _stop_automation(session: AsyncSession, service_id: int) -> None:
    await session.execute(
        update(ServiceAutomation).where(ServiceAutomation.service_id == int(service_id)).values(
            renew_enabled=False, duration_enabled=False, volume_enabled=False,
            renew_notice=None, duration_notice=None, volume_notice=None,
        ).execution_options(synchronize_session=False)
    )


async def _queue_sender_notice(session: AsyncSession, row: ServiceTransfer, service_name: str) -> None:
    sender = await session.get(BotUser, int(row.from_user_id))
    if sender is None or sender.is_blocked:
        return
    add_notice(
        session, shop_id=row.shop_id, bot_user_id=int(sender.id), telegram_id=int(sender.telegram_id),
        action=DONE_ACTION, service_name=service_name, reason="", service_id=None,
    )


async def accept_transfer(
    session: AsyncSession, recipient: BotUser, token: str, *, shop_id: int | None, ui: Mapping[str, Any]
) -> UserService:
    """Move the service to ``recipient``; safe against a second accept of the same link."""
    if recipient.is_blocked:
        raise ValueError(LINK_GONE)
    offer = await preview_transfer(session, recipient, token, shop_id=shop_id)
    row = await session.get(ServiceTransfer, offer.transfer_id)
    service = await session.get(UserService, offer.service_id)
    policy = transfer_policy(ui)
    # Re-checked here: the shop may have turned transfers off or removed the plan since the link was made.
    if not policy.allows_plan(service.plan_id) or await service_shop_id(session, service) != row.shop_id:
        raise ValueError(LINK_GONE)
    await _assert_transferable(session, service)
    await _claim(session, row, recipient, _now(), rotate=policy.new_link)
    await _stop_automation(session, offer.service_id)
    await _queue_sender_notice(session, row, offer.service_name)
    await session.commit()
    await session.refresh(service)
    if policy.new_link:
        await rotate_subscription_link(session, service, transfer_id=int(row.id))
    return service


async def rotate_subscription_link(session: AsyncSession, service: UserService, *, transfer_id: int) -> bool:
    """Revoke the old subscription link on the panel and store the new one.

    A failure leaves ``link_rotation_pending`` set and the job tries again, so the sender's old link
    does not keep working. Revoking twice only issues another new link, which the new owner reads
    from «سرویس‌های من».
    """
    try:
        shop_id = await service_shop_id(session, service)
        pg = await get_pg_for_reseller(session, shop_id) if shop_id else get_pg()
        user = await pg.revoke_sub_by_id(int(service.pg_user_id))
    except (PasarGuardError, httpx.HTTPError, TimeoutError, ValueError):
        logger.warning("subscription link not rotated after transfer service=%s; the job retries", service.id, exc_info=True)
        return False
    url = user_subscription_url(user if isinstance(user, dict) else None)
    if url:
        service.subscription_url = url
        service.subscription_token = extract_sub_token(url) or service.subscription_token
    await session.execute(
        update(ServiceTransfer).where(ServiceTransfer.id == int(transfer_id))
        .values(link_rotation_pending=False).execution_options(synchronize_session=False)
    )
    await session.commit()
    return True


ROTATION_BATCH = 50


async def retry_link_rotations(session: AsyncSession) -> int:
    """Rotate the links still pending after an accepted transfer; one bad row never stops the batch."""
    rows = list(await session.scalars(
        select(ServiceTransfer).where(ServiceTransfer.link_rotation_pending.is_(True))
        .order_by(ServiceTransfer.id).limit(ROTATION_BATCH)
    ))
    services = {
        s.id: s for s in await session.scalars(select(UserService).where(UserService.id.in_({r.service_id for r in rows})))
    } if rows else {}
    done = 0
    for row in rows:
        service = services.get(row.service_id)
        if service is None or not service.pg_user_id:
            # Nothing left to rotate: the service was removed after the transfer.
            await session.execute(
                update(ServiceTransfer).where(ServiceTransfer.id == int(row.id)).values(link_rotation_pending=False)
                .execution_options(synchronize_session=False)
            )
            await session.commit()
            continue
        done += int(await rotate_subscription_link(session, service, transfer_id=int(row.id)))
    return done
