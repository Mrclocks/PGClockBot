"""Shop staff change a customer's shop wallet by hand, within fixed limits.

Only the shop's own purse (``ShopWallet`` for that shop) ever moves; the platform purse
and other shops' purses are out of reach. One request is one unit of work, committed once:
the daily-cap claim, the wallet change and the staff action record. The request key makes
a repeated submit return the first result instead of moving money again.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, ShopStaffAction, ShopWalletAdjustDay
from app.services.authz import authz_from_staff, can_shop
from app.services.db_safe import rollback_quiet
from app.services.reseller_access import is_bot_admin_id
from app.services.resellers import get_reseller_profile
from app.services.shop_membership import is_shop_customer
from app.services.shop_scope import is_platform_admin, shop_owner_id
from app.services.staff_audit import actor_from_staff, add_staff_action
from app.services.wallet import credit_wallet, debit_wallet, get_wallet_balance

WALLET_ADJUST_PERMISSION = "wallet_adjust"
ADJUST_MIN = 1_000
ADJUST_MAX = 5_000_000
# Credits and debits together, per shop per UTC day.
DAILY_CAP = 20_000_000
REASON_MIN = 3
REASON_MAX = 200
DIRECTIONS = {"credit": "wallet_credit", "debit": "wallet_debit"}
REQUEST_KEY_PATTERN = re.compile(r"[0-9a-f]{16,64}")
SHORTFALL = "موجودی کیف پول مشتری برای این کاهش کافی نیست"
FAILED = "تغییر کیف پول انجام نشد"


class ShopWalletAdjustError(ValueError):
    """A rule of the manual adjustment was broken; the message is safe to show."""


@dataclass(frozen=True, slots=True)
class ShopWalletRequest:
    customer_id: int
    amount: int
    direction: str
    reason: str
    request_key: str


@dataclass(frozen=True, slots=True)
class ShopWalletChange:
    shop_id: int
    customer_id: int
    amount: int
    direction: str
    balance_after: int
    replayed: bool


def wallet_adjust_shop(staff: dict) -> int:
    """The shop whose purses ``staff`` may adjust; raises ``PermissionError``.

    The platform owner adjusts the platform purse on its own route, never a shop's.
    """
    if is_platform_admin(staff):
        raise PermissionError("کیف پول فروشگاه در پنل همان فروشگاه تنظیم می‌شود")
    if not can_shop(authz_from_staff(staff), WALLET_ADJUST_PERMISSION):
        raise PermissionError("دسترسی تنظیم کیف پول مشتری ندارید")
    shop_id = shop_owner_id(staff)
    if shop_id is None:
        raise PermissionError("حساب شما به فروشگاه متصل نیست")
    return shop_id


def shop_wallet_form_context(staff: dict) -> dict[str, object]:
    """Template values for the customer edit modal: the form only for staff who may use it."""
    try:
        wallet_adjust_shop(staff)
    except PermissionError:
        return {"can_adjust_shop_wallet": False}
    return {
        "can_adjust_shop_wallet": True,
        "shop_wallet_nonce": secrets.token_hex(16),
        "shop_wallet_max": ADJUST_MAX,
        "shop_wallet_daily": DAILY_CAP,
    }


def parse_request(*, customer_id: int, raw_amount: object, direction: object, reason: object, request_key: object) -> ShopWalletRequest:
    """Validate the form values; raises ``ShopWalletAdjustError``."""
    text_amount = str(raw_amount or "").strip().replace(",", "").replace("٬", "")
    if not text_amount.isdigit() or len(text_amount) > 12:
        raise ShopWalletAdjustError("مبلغ باید عدد صحیح مثبت باشد")
    amount = int(text_amount)
    if not ADJUST_MIN <= amount <= ADJUST_MAX:
        raise ShopWalletAdjustError(f"مبلغ باید بین {ADJUST_MIN:,} و {ADJUST_MAX:,} تومان باشد")
    if direction not in DIRECTIONS:
        raise ShopWalletAdjustError("نوع تغییر نامعتبر است")
    clean_reason = " ".join(str(reason or "").split())
    if len(clean_reason) < REASON_MIN:
        raise ShopWalletAdjustError(f"علت تغییر الزامی است (حداقل {REASON_MIN} کاراکتر)")
    key = str(request_key or "").strip().lower()
    if not REQUEST_KEY_PATTERN.fullmatch(key):
        raise ShopWalletAdjustError("فرم منقضی شده است؛ صفحه را دوباره باز کنید")
    return ShopWalletRequest(
        customer_id=int(customer_id), amount=amount, direction=str(direction),
        reason=clean_reason[:REASON_MAX], request_key=key,
    )


def _utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def _claim_daily_cap(session: AsyncSession, shop_id: int, amount: int) -> None:
    day = _utc_day()
    try:
        async with session.begin_nested():
            session.add(ShopWalletAdjustDay(shop_id=shop_id, day=day, used=0))
            await session.flush()
    except IntegrityError:
        pass
    claimed = await session.execute(
        update(ShopWalletAdjustDay)
        .where(
            ShopWalletAdjustDay.shop_id == shop_id,
            ShopWalletAdjustDay.day == day,
            ShopWalletAdjustDay.used + amount <= DAILY_CAP,
        )
        .values(used=ShopWalletAdjustDay.used + amount)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        raise ShopWalletAdjustError(f"سقف تغییر دستی کیف پول امروز ({DAILY_CAP:,} تومان) پر شده است")


async def _replayed(session: AsyncSession, shop_id: int, request: ShopWalletRequest) -> ShopWalletChange | None:
    """The first result of a request key already used; refuses a key reused for something else."""
    record = (
        await session.execute(select(ShopStaffAction).where(ShopStaffAction.request_key == request.request_key))
    ).scalar_one_or_none()
    if record is None:
        return None
    same = (
        record.shop_id == shop_id
        and record.target_id == request.customer_id
        and record.action == DIRECTIONS[request.direction]
        and record.amount == request.amount
    )
    if not same:
        raise ShopWalletAdjustError("فرم منقضی شده است؛ صفحه را دوباره باز کنید")
    customer = await session.get(BotUser, request.customer_id)
    balance = await get_wallet_balance(session, customer, shop_id=shop_id) if customer else 0
    return ShopWalletChange(
        shop_id=shop_id, customer_id=request.customer_id, amount=request.amount,
        direction=request.direction, balance_after=int(balance), replayed=True,
    )


async def _check_target(session: AsyncSession, shop_id: int, customer: BotUser | None) -> BotUser:
    if customer is None or not await is_shop_customer(session, customer, shop_id):
        raise ShopWalletAdjustError("این کاربر مشتری فروشگاه شما نیست")
    if int(customer.id) == shop_id:
        raise ShopWalletAdjustError("کیف پول خود فروشگاه را نمی‌توان دستی تغییر داد")
    profile = await get_reseller_profile(session, shop_id)
    if is_bot_admin_id(profile, int(customer.telegram_id or 0)):
        raise ShopWalletAdjustError("کیف پول کارکنان فروشگاه را نمی‌توان دستی تغییر داد")
    return customer


async def adjust_shop_wallet(session: AsyncSession, *, staff: dict, request: ShopWalletRequest) -> ShopWalletChange:
    """Apply one manual change to a customer's wallet in the staff member's own shop."""
    shop_id = wallet_adjust_shop(staff)
    replay = await _replayed(session, shop_id, request)
    if replay is not None:
        return replay
    customer = await _check_target(session, shop_id, await session.get(BotUser, request.customer_id))
    actor = actor_from_staff(staff, shop_id=shop_id)
    reason = f"تنظیم دستی فروشگاه ({actor.label[:64]}): {request.reason}"
    try:
        await _claim_daily_cap(session, shop_id, request.amount)
        move = credit_wallet if request.direction == "credit" else debit_wallet
        await move(session, customer, request.amount, reason, shop_id=shop_id, commit=False)
        add_staff_action(
            session, actor, action=DIRECTIONS[request.direction], target_type="customer",
            target_id=int(customer.id), amount=request.amount, reason=request.reason, request_key=request.request_key,
        )
        await session.commit()
    except IntegrityError:
        # The same request key won a concurrent race: report that first result.
        await rollback_quiet(session)
        replay = await _replayed(session, shop_id, request)
        if replay is None:
            raise
        return replay
    except ShopWalletAdjustError:
        await rollback_quiet(session)
        raise
    except ValueError as exc:
        # After the checks above, the wallet refuses only a debit larger than the balance.
        await rollback_quiet(session)
        raise ShopWalletAdjustError(SHORTFALL if request.direction == "debit" else FAILED) from exc
    balance = await get_wallet_balance(session, customer, shop_id=shop_id)
    return ShopWalletChange(
        shop_id=shop_id, customer_id=int(customer.id), amount=request.amount,
        direction=request.direction, balance_after=int(balance), replayed=False,
    )
