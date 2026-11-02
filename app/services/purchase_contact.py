"""Verify Telegram contacts and enforce each shop's subscription purchase gate."""
from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import BotUser, PurchaseContact
from app.services.trial_contact import hash_trial_phone, normalize_phone_digits
from app.services.users import current_shop_reseller_id, get_all_settings, on

CONTACT_REQUIRED_MESSAGE = "برای خرید اشتراک، ابتدا در ربات با دستور /verify_phone شماره تماس خودتان را تأیید کنید."


def purchase_shop_key() -> int:
    return current_shop_reseller_id() or 0


async def purchase_contact_needed(
    session: AsyncSession, user_id: int, *, settings: Mapping[str, str] | None = None,
    shop_key: int | None = None,
) -> bool:
    shop_key = purchase_shop_key() if shop_key is None else shop_key
    settings = settings if settings is not None else await get_all_settings(session, reseller_id=shop_key)
    if not on(settings.get("purchase_require_contact")):
        return False
    verified = await session.scalar(select(PurchaseContact.user_id).join(
        BotUser, BotUser.id == PurchaseContact.user_id,
    ).where(
        PurchaseContact.user_id == user_id,
        PurchaseContact.shop_key == shop_key,
        PurchaseContact.telegram_id == BotUser.telegram_id,
        BotUser.is_blocked.is_(False),
    ))
    return verified is None


async def verified_contact_phone_hash(
    session: AsyncSession, user_id: int, *, shop_key: int | None = None,
) -> str | None:
    """The phone hash the customer already verified in this shop, if it is still theirs."""
    shop_key = purchase_shop_key() if shop_key is None else shop_key
    return await session.scalar(select(PurchaseContact.phone_hash).join(
        BotUser, BotUser.id == PurchaseContact.user_id,
    ).where(
        PurchaseContact.user_id == user_id,
        PurchaseContact.shop_key == shop_key,
        PurchaseContact.telegram_id == BotUser.telegram_id,
        BotUser.is_blocked.is_(False),
    ))


async def require_purchase_contact(session: AsyncSession, user_id: int) -> None:
    if await purchase_contact_needed(session, user_id):
        raise ValueError(CONTACT_REQUIRED_MESSAGE)


async def verify_purchase_contact(
    session: AsyncSession, user: BotUser, *, contact_user_id: int | None, phone_number: str,
) -> None:
    if user.is_blocked or type(contact_user_id) is not int or contact_user_id != user.telegram_id:
        raise ValueError("فقط شماره متعلق به همین اکانت تلگرام پذیرفته می‌شود.")
    if not isinstance(phone_number, str) or len(phone_number) > 32 or not 8 <= len(normalize_phone_digits(phone_number)) <= 15:
        raise ValueError("شماره تماس نامعتبر است")
    phone_hash = hash_trial_phone(phone_number, secret=get_settings().web_secret, require_iran=False)
    user_id, shop_key = user.id, purchase_shop_key()
    row = await session.get(PurchaseContact, (user_id, shop_key))
    if row is not None and row.telegram_id == user.telegram_id:
        return
    try:
        async with session.begin_nested():
            if row is not None:
                await session.delete(row)
                await session.flush()
            session.add(PurchaseContact(
                user_id=user_id, shop_key=shop_key, telegram_id=user.telegram_id, phone_hash=phone_hash,
            ))
            await session.flush()
    except IntegrityError as exc:
        verified = await session.get(PurchaseContact, (user_id, shop_key), populate_existing=True)
        if verified is None or verified.telegram_id != contact_user_id:
            raise ValueError("تأیید شماره تماس انجام نشد؛ دوباره تلاش کنید.") from exc
    await session.commit()
