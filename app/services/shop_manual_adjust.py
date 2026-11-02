"""Rules shared by manual changes that shop staff make to a customer's balances.

Wallet and loyalty-point changes both: take the shop from the session, accept only a
customer of that shop who is not its staff, require a reason, and carry a request key
that makes a repeated submit return the first result.
"""

from __future__ import annotations

import re
import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, ShopStaffAction
from app.services.authz import authz_from_staff, can_shop
from app.services.reseller_access import is_bot_admin_id
from app.services.resellers import get_reseller_profile
from app.services.shop_membership import is_shop_customer
from app.services.shop_scope import is_platform_admin, shop_owner_id

REASON_MIN = 3
REASON_MAX = 200
REQUEST_KEY_PATTERN = re.compile(r"[0-9a-f]{16,64}")
EXPIRED_FORM = "فرم منقضی شده است؛ صفحه را دوباره باز کنید"


class ManualAdjustError(ValueError):
    """A rule of a manual change was broken; the message is safe to show staff."""


def new_request_key() -> str:
    return secrets.token_hex(16)


def manual_adjust_shop(staff: dict, permission: str, *, denied: str) -> int:
    """The shop whose customers ``staff`` may change with ``permission``; raises ``PermissionError``.

    The platform owner works on platform customers through its own routes, never a shop's.
    """
    if is_platform_admin(staff):
        raise PermissionError("این تغییر در پنل همان فروشگاه انجام می‌شود")
    if not can_shop(authz_from_staff(staff), permission):
        raise PermissionError(denied)
    shop_id = shop_owner_id(staff)
    if shop_id is None:
        raise PermissionError("حساب شما به فروشگاه متصل نیست")
    return shop_id


def parse_whole_number(raw: object, *, low: int, high: int, label: str) -> int:
    text = str(raw or "").strip().replace(",", "").replace("٬", "")
    if not text.isdigit() or len(text) > 12:
        raise ManualAdjustError(f"{label} باید عدد صحیح مثبت باشد")
    value = int(text)
    if not low <= value <= high:
        raise ManualAdjustError(f"{label} باید بین {low:,} و {high:,} باشد")
    return value


def parse_reason(raw: object) -> str:
    reason = " ".join(str(raw or "").split())
    if len(reason) < REASON_MIN:
        raise ManualAdjustError(f"علت تغییر الزامی است (حداقل {REASON_MIN} کاراکتر)")
    return reason[:REASON_MAX]


def parse_request_key(raw: object) -> str:
    key = str(raw or "").strip().lower()
    if not REQUEST_KEY_PATTERN.fullmatch(key):
        raise ManualAdjustError(EXPIRED_FORM)
    return key


async def adjustable_customer(session: AsyncSession, shop_id: int, customer_id: int) -> BotUser:
    """The customer, when it belongs to the shop and is not the shop or one of its bot admins."""
    customer = await session.get(BotUser, int(customer_id))
    if customer is None or not await is_shop_customer(session, customer, shop_id):
        raise ManualAdjustError("این کاربر مشتری فروشگاه شما نیست")
    if int(customer.id) == shop_id:
        raise ManualAdjustError("حساب خود فروشگاه را نمی‌توان دستی تغییر داد")
    profile = await get_reseller_profile(session, shop_id)
    if is_bot_admin_id(profile, int(customer.telegram_id or 0)):
        raise ManualAdjustError("حساب کارکنان فروشگاه را نمی‌توان دستی تغییر داد")
    return customer


async def earlier_record(
    session: AsyncSession, *, request_key: str, shop_id: int, customer_id: int, action: str, amount: int
) -> ShopStaffAction | None:
    """The record of a request key already used; refuses a key reused for a different change."""
    record = (
        await session.execute(select(ShopStaffAction).where(ShopStaffAction.request_key == request_key))
    ).scalar_one_or_none()
    if record is None:
        return None
    if (record.shop_id, record.target_id, record.action, record.amount) != (shop_id, customer_id, action, amount):
        raise ManualAdjustError(EXPIRED_FORM)
    return record
