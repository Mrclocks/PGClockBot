"""Keep all Telegram copies of a reviewed payment in sync with the database."""

from __future__ import annotations

import html
import logging

from aiogram import Bot
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import BotUser, Order, Payment, PaymentReviewMessage, PaymentStatus
from app.services.formatting import format_message, format_user_label
from app.services.notice_links import notice_markup
from app.services.notifications import customer_markup

logger = logging.getLogger(__name__)


async def remember_review_message(
    session: AsyncSession, payment_id: int, bot: Bot, message: Message
) -> None:
    """Stage a message reference; callers commit before review can proceed."""
    if not isinstance(bot.id, int) or not isinstance(message.message_id, int):
        return
    is_photo = bool(getattr(message, "photo", None))
    insert = pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
    # Fan-out and the first callback can register the same message concurrently.
    # Keep the original pending card and avoid poisoning either transaction.
    await session.execute(
        insert(PaymentReviewMessage).values(
            bot_id=bot.id, chat_id=message.chat.id, message_id=message.message_id,
            payment_id=payment_id,
            text=getattr(message, "html_caption" if is_photo else "html_text", "") or "",
            is_photo=is_photo,
        ).on_conflict_do_nothing(index_elements=["bot_id", "chat_id", "message_id"])
    )


def _reviewed_text(message: PaymentReviewMessage, status: str, actor: str, note: str | None = None) -> str:
    approved = status == PaymentStatus.APPROVED.value
    title = "✅ پرداخت تأیید شد" if approved else "❌ پرداخت رد شد"
    outcome = "✅ تأیید شد" if approved else "❌ رد شد"
    text = "\n".join(
        line for line in message.text.splitlines()
        if "از دکمه‌های زیر تأیید یا رد کنید." not in line
    ).rstrip().replace("⏳ نیاز به تأیید", title)
    text = f"{text}\n\n{outcome} توسط {actor}"
    if not approved and note and note.strip():
        text += f"\n📝 <b>یادداشت بررسی:</b> {html.escape(note.strip()[:300])}"
    limit = 1024 if message.is_photo else 4096
    # Never truncate HTML mid-tag when the original card fills a photo caption.
    if len(text) > limit:
        text = format_message(
            title, f"پرداخت #{message.payment_id}\n{outcome} توسط {actor}"
        )
    return text


async def sync_review_messages(
    session: AsyncSession, payment_id: int, *, bot: Bot | None = None
) -> int:
    """Edit all known copies using the committed reviewer, never the last clicker.

    Telegram failures are isolated per recipient and cannot undo a payment.
    Bot IDs prevent editing unrelated messages through another shop's bot.
    """
    messages = list((await session.execute(
        select(PaymentReviewMessage).where(PaymentReviewMessage.payment_id == payment_id)
    )).scalars().all())
    if not messages:
        return 0
    state = (await session.execute(
        select(Payment.status, Payment.reviewed_by, Payment.order_id, Payment.user_id, Payment.review_note,
               Payment.wallet_shop_id)
        .where(Payment.id == payment_id)
    )).one_or_none()
    if state is None or state.status not in {
        PaymentStatus.APPROVED.value, PaymentStatus.REJECTED.value,
    }:
        return 0
    reviewer = None
    if state.reviewed_by:
        reviewer = (await session.execute(
            select(BotUser).where(BotUser.telegram_id == state.reviewed_by)
        )).scalar_one_or_none()
    actor = format_user_label(reviewer, telegram_id=state.reviewed_by) if state.reviewed_by else "مدیریت"

    order_row = (await session.execute(
        select(Order.reseller_id, Order.service_id).where(Order.id == state.order_id)
    )).one_or_none() if state.order_id else None
    rid = (order_row.reseller_id if order_row else None) or state.wallet_shop_id
    # The reviewed card keeps the way to the customer and service, so staff can follow up in one tap.
    follow_up = notice_markup(customer_markup(
        user_id=state.user_id, service_id=order_row.service_id if order_row else None,
        shop_id=int(rid) if rid else None,
    ))

    send_bot = bot
    should_close = False
    if send_bot is None or not any(m.bot_id == send_bot.id for m in messages):
        if rid:
            from app.services.reseller_bots import open_notify_bot_for_reseller

            send_bot, should_close = await open_notify_bot_for_reseller(session, int(rid))
        else:
            token = get_settings().bot_token
            if not token:
                return 0
            from app.bot import telegram_session

            send_bot, should_close = Bot(token=token, session=telegram_session()), True
    if send_bot is None:
        return 0
    updated = 0
    try:
        for message in messages:
            if message.bot_id != send_bot.id:
                continue
            kwargs = dict(
                chat_id=message.chat_id, message_id=message.message_id,
                parse_mode="HTML", reply_markup=follow_up,
            )
            text = _reviewed_text(message, state.status, actor, state.review_note)
            try:
                if message.is_photo:
                    await send_bot.edit_message_caption(caption=text, **kwargs)
                else:
                    await send_bot.edit_message_text(text=text, **kwargs)
                updated += 1
            except Exception as exc:
                if "message is not modified" in str(exc).lower():
                    updated += 1
                else:
                    logger.warning(
                        "payment review edit failed payment=%s chat=%s message=%s",
                        payment_id, message.chat_id, message.message_id, exc_info=True,
                    )
    finally:
        if should_close:
            await send_bot.session.close()
    return updated
