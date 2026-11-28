"""More than one receipt photo for one payment.

A customer may send two photos for one purchase (an album, a second page of a bank slip). The first
photo opens the review as before (``Payment.receipt_file_id``); every later photo of the same pending
payment is kept here and sent to staff as a reply under the review card they already have, without
review buttons, instead of opening a second review. The web panel shows all photos of a payment.

Forwarding claims ``forwarded_at`` first, so a photo is sent once even when the album's photos are
handled at the same moment: whichever runs after the review cards exist sends it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Payment, PaymentReceipt, PaymentReceiptFingerprint, PaymentReviewMessage, PaymentStatus
from app.services.formatting import format_message

logger = logging.getLogger(__name__)

# More photos than this for one payment are refused: a receipt is one or two pages, not a chat.
MAX_EXTRA_RECEIPTS = 4
# A photo joins the payment's receipt only this long after the first one; later it is a new message.
EXTRA_RECEIPT_WINDOW = timedelta(hours=24)

EXTRA_ADDED = format_message(
    "✅ رسید اضافه شد",
    "این تصویر هم به همان پرداخت اضافه شد و برای بررسی فرستاده شد؛ نیازی به ارسال دوباره نیست.",
)
EXTRA_ALREADY_SENT = format_message("✅ رسید ثبت شده", "این تصویر قبلاً برای همین پرداخت ثبت شده است.")
EXTRA_LIMIT = format_message(
    "⚠️ رسید ثبت نشد",
    f"برای هر پرداخت حداکثر {MAX_EXTRA_RECEIPTS + 1} تصویر رسید پذیرفته می‌شود؛ منتظر بررسی بمانید.",
)


# Telegram photo captions hold at most 1024 characters.
CAPTION_LIMIT = 1024


def forward_caption(payment_id: int, note: str | None = None) -> str:
    """Plain-text caption of a forwarded photo (sent without parse mode)."""
    caption = f"📎 تصویر دیگر رسید پرداخت #{int(payment_id)} — بررسی با همان پیام بالا"
    if note and note.strip():
        caption += f"\n\n{note.strip()}"
    return caption[:CAPTION_LIMIT]


def accepts_extra_receipt(payment: Payment, *, now: datetime) -> bool:
    """A pending payment that already has its first receipt, recently enough to take another photo."""
    if payment.status != PaymentStatus.PENDING.value or not payment.receipt_file_id:
        return False
    first = payment.receipt_at
    return first is None or now - first <= EXTRA_RECEIPT_WINDOW


async def add_extra_receipt(
    session: AsyncSession, payment: Payment, *, file_id: str, file_unique_id: str | None, note: str | None = None
) -> str:
    """Keep a later photo of ``payment``; returns the customer's answer. Commits."""
    payment_id = int(payment.id)
    unique = (file_unique_id or "").strip() or None
    if unique and await _already_has_photo(session, payment_id, unique):
        return EXTRA_ALREADY_SENT
    if await count_extra_receipts(session, payment_id) >= MAX_EXTRA_RECEIPTS:
        return EXTRA_LIMIT
    inserted = await session.execute(
        pg_insert(PaymentReceipt)
        .values(payment_id=payment_id, file_id=file_id, file_unique_id=unique, review_note=note)
        .on_conflict_do_nothing(
            index_elements=["payment_id", "file_unique_id"], index_where=PaymentReceipt.file_unique_id.is_not(None)
        )
        .returning(PaymentReceipt.id)
    )
    added = inserted.scalar_one_or_none() is not None
    await session.commit()
    return EXTRA_ADDED if added else EXTRA_ALREADY_SENT


async def _already_has_photo(session: AsyncSession, payment_id: int, file_unique_id: str) -> bool:
    """The same photo was already the payment's first receipt (its fingerprint) or an extra one."""
    first = select(PaymentReceiptFingerprint.id).where(
        PaymentReceiptFingerprint.payment_id == payment_id, PaymentReceiptFingerprint.file_unique_id == file_unique_id
    )
    extra = select(PaymentReceipt.id).where(
        PaymentReceipt.payment_id == payment_id, PaymentReceipt.file_unique_id == file_unique_id
    )
    return (await session.scalar(first.limit(1))) is not None or (await session.scalar(extra.limit(1))) is not None


async def count_extra_receipts(session: AsyncSession, payment_id: int) -> int:
    query = select(func.count(PaymentReceipt.id)).where(PaymentReceipt.payment_id == int(payment_id))
    return int(await session.scalar(query) or 0)


async def extra_receipt_counts(session: AsyncSession, payment_ids: list[int]) -> dict[int, int]:
    """Extra photos per payment for a page of payments, in one query."""
    if not payment_ids:
        return {}
    rows = await session.execute(
        select(PaymentReceipt.payment_id, func.count(PaymentReceipt.id))
        .where(PaymentReceipt.payment_id.in_([int(i) for i in payment_ids]))
        .group_by(PaymentReceipt.payment_id)
    )
    return {int(pid): int(count) for pid, count in rows.all()}


async def receipt_file_id(session: AsyncSession, payment: Payment, index: int) -> str | None:
    """The payment's ``index``-th photo: 0 is the first receipt, then the extra ones in the order sent."""
    if index == 0:
        return payment.receipt_file_id
    if index < 0 or index > MAX_EXTRA_RECEIPTS:
        return None
    return await session.scalar(
        select(PaymentReceipt.file_id)
        .where(PaymentReceipt.payment_id == int(payment.id))
        .order_by(PaymentReceipt.id)
        .offset(index - 1)
        .limit(1)
    )


async def forward_extra_receipts(session: AsyncSession, payment_id: int, *, bot: Bot) -> int:
    """Send the payment's not yet forwarded photos under its review cards on ``bot``; returns photos sent.

    Nothing is claimed while ``bot`` has no review card for the payment yet: the run that sends the
    cards forwards them afterwards. A photo's file id belongs to the bot that received it, so only
    that bot's cards get it.
    """
    bot_id = getattr(bot, "id", None)
    if not isinstance(bot_id, int):
        return 0
    cards = list((await session.execute(
        select(PaymentReviewMessage.chat_id, PaymentReviewMessage.message_id).where(
            PaymentReviewMessage.payment_id == int(payment_id), PaymentReviewMessage.bot_id == bot_id
        )
    )).all())
    if not cards:
        return 0
    claimed = list((await session.execute(
        update(PaymentReceipt)
        .where(PaymentReceipt.payment_id == int(payment_id), PaymentReceipt.forwarded_at.is_(None))
        .values(forwarded_at=datetime.now(timezone.utc))
        .returning(PaymentReceipt.id, PaymentReceipt.file_id, PaymentReceipt.review_note)
        .execution_options(synchronize_session=False)
    )).all())
    await session.commit()
    sent = 0
    for _receipt_id, file_id, note in sorted(claimed):
        for chat_id, message_id in cards:
            try:
                await bot.send_photo(
                    chat_id=int(chat_id), photo=file_id, caption=forward_caption(payment_id, note), parse_mode=None,
                    reply_to_message_id=int(message_id), allow_sending_without_reply=True,
                )
                sent += 1
            except TelegramAPIError:
                # The photo stays on the payment and in the web panel; one closed chat must not stop the rest.
                logger.warning("extra receipt forward failed payment=%s chat=%s", payment_id, chat_id, exc_info=True)
    return sent
