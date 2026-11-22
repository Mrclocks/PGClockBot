"""Automatic personal coupons: a shop issues a one-time discount code on an event.

Three programs per shop (``coupon_programs``):

- ``next_purchase``: after a delivered new purchase or renewal, a coupon for the next one;
- ``referral_welcome``: to a customer who joined the shop through a referral link;
- ``referral_reward``: to the referrer after the friend's first delivered purchase, at most
  ``monthly_cap`` per referrer and month.

A coupon is a ``LoyaltyDiscountEntitlement`` of the shop, so the existing checkout step offers
and reserves it. Each event has an ``issue_key`` under a partial unique index: a repeated
delivery hook or registration never issues a second coupon. Issuing commits on its own and
runs after the event committed; a failure is logged and never undoes the sale or the signup.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, CouponProgram, LoyaltyDiscountEntitlement, Order
from app.services.cashback import EVENT_ADDON, order_event
from app.services.db_safe import rollback_and_reload, rollback_quiet
from app.services.formatting import format_toman
from app.services.loyalty import issue_discount_entitlement
from app.services.platform_identity import deliverable_telegram_id
from app.services.reseller_bots import ShopBotUnavailable, open_notify_bot_for_user

logger = logging.getLogger(__name__)

TRIGGER_NEXT_PURCHASE = "next_purchase"
TRIGGER_REFERRAL_WELCOME = "referral_welcome"
TRIGGER_REFERRAL_REWARD = "referral_reward"
TRIGGERS: dict[str, str] = {
    TRIGGER_NEXT_PURCHASE: "کوپن خرید بعدی",
    TRIGGER_REFERRAL_WELCOME: "کوپن خوش‌آمد دوست دعوت‌شده",
    TRIGGER_REFERRAL_REWARD: "کوپن معرف",
}
MAX_PERCENT = 90
MAX_MONEY = 2_000_000_000
MAX_VALID_DAYS = 365
MAX_MONTHLY_CAP = 1_000


@dataclass(frozen=True, slots=True)
class ProgramInput:
    enabled: bool
    percent: int
    max_discount: int | None
    min_purchase: int
    valid_days: int
    monthly_cap: int | None


def shop_key(shop_id: int | None) -> int:
    return int(shop_id or 0)


def validate_program(trigger: str, data: ProgramInput) -> None:
    """Raise ``ValueError`` (safe to show) unless every field is in range."""
    if trigger not in TRIGGERS:
        raise ValueError("نوع کوپن نامعتبر است")
    checks = (
        (data.percent, 1, MAX_PERCENT, "درصد"),
        (data.min_purchase, 0, MAX_MONEY, "حداقل خرید"),
        (data.valid_days, 1, MAX_VALID_DAYS, "مدت اعتبار"),
    )
    for value, lower, upper, label in checks:
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError(f"«{label}» خارج از محدودهٔ مجاز است")
    for value, upper, label in ((data.max_discount, MAX_MONEY, "سقف تخفیف"), (data.monthly_cap, MAX_MONTHLY_CAP, "سقف ماهانه")):
        if value is not None and (type(value) is not int or not 1 <= value <= upper):
            raise ValueError(f"«{label}» خارج از محدودهٔ مجاز است")


async def load_programs(session: AsyncSession, shop_id: int | None) -> dict[str, CouponProgram]:
    rows = (await session.scalars(
        select(CouponProgram).where(CouponProgram.shop_key == shop_key(shop_id)).limit(len(TRIGGERS))
    )).all()
    return {row.trigger: row for row in rows}


async def save_program(session: AsyncSession, shop_id: int | None, trigger: str, data: ProgramInput) -> CouponProgram:
    validate_program(trigger, data)
    program = (await load_programs(session, shop_id)).get(trigger)
    if program is None:
        program = CouponProgram(shop_key=shop_key(shop_id), trigger=trigger)
        session.add(program)
    program.enabled = bool(data.enabled)
    program.percent = data.percent
    program.max_discount = data.max_discount
    program.min_purchase = data.min_purchase
    program.valid_days = data.valid_days
    program.monthly_cap = data.monthly_cap if trigger == TRIGGER_REFERRAL_REWARD else None
    try:
        await session.commit()
    except IntegrityError:
        await rollback_quiet(session)
        raise ValueError("تنظیم هم‌زمان ذخیره شد؛ صفحه را دوباره باز کنید") from None
    return program


async def _enabled_program(session: AsyncSession, shop_id: int | None, trigger: str) -> CouponProgram | None:
    return await session.scalar(select(CouponProgram).where(
        CouponProgram.shop_key == shop_key(shop_id), CouponProgram.trigger == trigger,
        CouponProgram.enabled.is_(True),
    ))


async def _monthly_count(session: AsyncSession, user_id: int, prefix: str, *, now: datetime) -> int:
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    count = await session.scalar(select(func.count()).select_from(LoyaltyDiscountEntitlement).where(
        LoyaltyDiscountEntitlement.user_id == int(user_id),
        LoyaltyDiscountEntitlement.issue_key.startswith(prefix, autoescape=True),
        LoyaltyDiscountEntitlement.created_at >= month_start,
    ))
    return int(count or 0)


async def issue_coupon(
    session: AsyncSession, *, shop_id: int | None, trigger: str, user: BotUser, issue_key: str,
) -> LoyaltyDiscountEntitlement | None:
    """Issue the program's coupon once for ``issue_key`` and commit; None when not issued."""
    program = await _enabled_program(session, shop_id, trigger)
    if program is None:
        return None
    if program.monthly_cap is not None:
        prefix = f"{trigger}:{shop_key(shop_id)}:"
        if await _monthly_count(session, int(user.id), prefix, now=datetime.now(timezone.utc)) >= program.monthly_cap:
            return None
    try:
        async with session.begin_nested():
            coupon = await issue_discount_entitlement(
                session, user, percent=int(program.percent), min_purchase_toman=int(program.min_purchase),
                max_discount_toman=program.max_discount, expires_days=int(program.valid_days), reseller_id=shop_id,
            )
            coupon.issue_key = issue_key
            await session.flush()
    except IntegrityError:
        return None
    await session.commit()
    return coupon


def coupon_text(coupon: LoyaltyDiscountEntitlement, trigger: str) -> str:
    cap = f" (تا {format_toman(int(coupon.max_discount_toman))})" if coupon.max_discount_toman else ""
    lines = [f"🎟 <b>{TRIGGERS[trigger]}</b>: {int(coupon.percent)}٪ تخفیف{cap}"]
    if int(coupon.min_purchase_toman or 0) > 0:
        lines.append(f"برای خرید از {format_toman(int(coupon.min_purchase_toman))}")
    if coupon.expires_at is not None:
        days = max(1, (coupon.expires_at - datetime.now(timezone.utc)).days + 1)
        lines.append(f"تا {days} روز اعتبار دارد")
    lines.append("هنگام پرداخت، از دکمهٔ «کد تخفیف» آن را انتخاب کنید.")
    return "\n".join(lines)


async def _notify(session: AsyncSession, user: BotUser, coupon: LoyaltyDiscountEntitlement, *, trigger: str,
                  shop_id: int | None) -> None:
    chat_id = deliverable_telegram_id(getattr(user, "telegram_id", None))
    if chat_id is None:
        return
    try:
        bot, should_close = await open_notify_bot_for_user(session, user, shop_id=shop_id)
    except ShopBotUnavailable:
        logger.info("coupon notice skipped: shop bot unavailable shop=%s", shop_id)
        return
    try:
        await bot.send_message(chat_id, coupon_text(coupon, trigger), parse_mode="HTML")
    finally:
        if should_close:
            await bot.session.close()


async def _issue_and_notify(session: AsyncSession, *, shop_id: int | None, trigger: str, user: BotUser,
                            issue_key: str, keep: tuple[object, ...]) -> None:
    """Best effort: the event already committed, so a failure is logged and the session rolled back.

    ``keep`` are the caller's objects to reload after a rollback, so it can go on reading them.
    """
    user_id = int(user.id)
    try:
        coupon = await issue_coupon(session, shop_id=shop_id, trigger=trigger, user=user, issue_key=issue_key)
    except Exception:  # noqa: BLE001 - optional reward after a committed event; logged and rolled back
        logger.warning("coupon issue failed trigger=%s user=%s", trigger, user_id, exc_info=True)
        await rollback_and_reload(session, *keep)
        return
    if coupon is None:
        return
    try:
        await _notify(session, user, coupon, trigger=trigger, shop_id=shop_id)
    except Exception:  # noqa: BLE001 - the coupon is stored and shown at checkout; the notice is optional
        logger.warning("coupon notice failed trigger=%s user=%s", trigger, user_id, exc_info=True)


async def issue_after_delivery(session: AsyncSession, order: Order) -> None:
    """Next-purchase coupon for a delivered, paid new purchase or renewal (not an add-on)."""
    if int(order.amount or 0) <= 0 or order_event(order) == EVENT_ADDON:
        return
    shop_id = int(order.reseller_id) if order.reseller_id else None
    try:
        buyer = await session.get(BotUser, int(order.user_id))
    except Exception:  # noqa: BLE001 - optional reward after a committed delivery; logged and rolled back
        logger.warning("coupon buyer lookup failed order=%s", order.id, exc_info=True)
        await rollback_and_reload(session, order)
        return
    if buyer is None:
        return
    await _issue_and_notify(session, shop_id=shop_id, trigger=TRIGGER_NEXT_PURCHASE, user=buyer,
                            issue_key=f"{TRIGGER_NEXT_PURCHASE}:{shop_key(shop_id)}:{int(order.id)}", keep=(order,))


async def issue_referral_welcome(session: AsyncSession, user: BotUser) -> None:
    """Welcome coupon for a customer who joined a shop through a referral link."""
    if not user.referred_by_id or int(user.referred_by_id) == int(user.id):
        return
    shop_id = int(user.referral_shop_id) if user.referral_shop_id else None
    await _issue_and_notify(session, shop_id=shop_id, trigger=TRIGGER_REFERRAL_WELCOME, user=user,
                            issue_key=f"{TRIGGER_REFERRAL_WELCOME}:{shop_key(shop_id)}:{int(user.id)}", keep=(user,))


async def issue_referral_reward(session: AsyncSession, *, referrer: BotUser, buyer: BotUser,
                                shop_id: int | None, keep: tuple[object, ...] = ()) -> None:
    """Coupon for the referrer once the friend's first purchase in the shop is delivered."""
    await _issue_and_notify(session, shop_id=shop_id, trigger=TRIGGER_REFERRAL_REWARD, user=referrer,
                            issue_key=f"{TRIGGER_REFERRAL_REWARD}:{shop_key(shop_id)}:{int(buyer.id)}", keep=keep)

