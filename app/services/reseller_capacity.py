"""Reseller capacity: renew package / buy extra volume & user slots."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, ResellerPlan, ResellerProfile
from app.services.billing import GB, is_payg
from app.services.db_safe import rollback_quiet
from app.services.formatting import format_toman
from app.services.volume_allocation_cascade import assert_child_growth_fits

logger = logging.getLogger(__name__)

# Whitelist quantities exposed in Telegram buttons — reject forged callback amounts.
ALLOWED_EXTRA_GB = frozenset({1, 5, 10, 50})
ALLOWED_EXTRA_USERS = frozenset({1, 5, 10, 20})


def plan_allows_buy_extra(plan: ResellerPlan | None) -> bool:
    """Quick-buy extras: fixed subscription plans only (not PAYG, not addon packs)."""
    if plan is None:
        return False
    if not bool(getattr(plan, "allow_buy_extra", False)):
        return False
    from app.services.billing import BILLING_MODE_PAYG
    from app.services.pg_admin_subscription import is_subscription_plan

    mode = str(getattr(plan, "billing_mode", "") or "fixed").strip().lower()
    if mode == BILLING_MODE_PAYG:
        return False
    return is_subscription_plan(plan)


def plan_extra_gb_price(plan: ResellerPlan | None) -> int:
    if plan is None:
        return 0
    return max(0, int(getattr(plan, "extra_gb_price", 0) or 0))


def plan_extra_user_price(plan: ResellerPlan | None) -> int:
    if plan is None:
        return 0
    return max(0, int(getattr(plan, "extra_user_price", 0) or 0))


def plan_renew_price(plan: ResellerPlan | None) -> int:
    """Flat renew base (legacy). Prefer compute_renew_invoice for full quote."""
    from app.services.pg_admin_subscription import renew_base_amount

    return renew_base_amount(plan)


async def load_reseller_plan(
    session: AsyncSession, profile: ResellerProfile
) -> ResellerPlan | None:
    plan_id = getattr(profile, "plan_id", None)
    if not plan_id:
        return None
    return await session.get(ResellerPlan, int(plan_id))


async def _charge_wallet(session: AsyncSession, user: BotUser, amount: int) -> None:
    """Atomically debit shop wallet (optimistic lock on balance)."""
    if amount <= 0:
        return
    uid = int(user.id)
    need = int(amount)
    bal = int(user.wallet_balance or 0)
    if bal < need:
        raise ValueError(
            f"موجودی کیف پول کافی نیست.\n"
            f"لازم: {format_toman(need)}\n"
            f"موجودی: {format_toman(bal)}"
        )
    with session.no_autoflush:
        result = await session.execute(
            update(BotUser)
            .where(BotUser.id == uid, BotUser.wallet_balance >= need)
            .values(wallet_balance=BotUser.wallet_balance - need)
            .execution_options(synchronize_session=False)
        )
    if result.rowcount != 1:
        await session.refresh(user)
        raise ValueError(
            f"موجودی کیف پول کافی نیست.\n"
            f"لازم: {format_toman(need)}\n"
            f"موجودی: {format_toman(int(user.wallet_balance or 0))}"
        )
    await session.refresh(user)


async def _refund_wallet(session: AsyncSession, user: BotUser, amount: int) -> None:
    if amount <= 0:
        return
    uid = int(user.id)
    with session.no_autoflush:
        await session.execute(
            update(BotUser)
            .where(BotUser.id == uid)
            .values(wallet_balance=BotUser.wallet_balance + int(amount))
            .execution_options(synchronize_session=False)
        )
    await session.refresh(user)


def _admin_max_users(admin: dict) -> int:
    overrides = admin.get("permission_overrides")
    if isinstance(overrides, dict) and overrides.get("max_users") is not None:
        try:
            return max(0, int(overrides["max_users"]))
        except (TypeError, ValueError):
            pass
    for key in ("max_users", "users_max"):
        if admin.get(key) is not None:
            try:
                return max(0, int(admin[key]))
            except (TypeError, ValueError):
                pass
    role = admin.get("role") if isinstance(admin.get("role"), dict) else {}
    limits = role.get("limits") if isinstance(role, dict) else {}
    if isinstance(limits, dict) and limits.get("max_users") is not None:
        try:
            return max(0, int(limits["max_users"]))
        except (TypeError, ValueError):
            pass
    return 0


def _admin_data_limit(admin: dict) -> int:
    for key in ("data_limit",):
        if admin.get(key) is not None:
            try:
                return max(0, int(admin[key]))
            except (TypeError, ValueError):
                pass
    return 0


EXTRA_FAILED_REFUNDED = "افزایش ظرفیت در پاسارگارد ناموفق بود — مبلغ بازگردانده شد"
EXTRA_OUTCOME_UNKNOWN = (
    "پاسخ پاسارگارد برای افزایش ظرفیت نرسید. خرید ثبت شده و ظرفیت با تمدید بعدی یا بررسی پشتیبانی اعمال می‌شود؛ "
    "دوباره خرید نکنید."
)


@dataclass(slots=True)
class _ExtraPurchase:
    uname: str
    amount: int
    gb: int
    users: int
    reason: str


def _panel_rejected(exc: Exception) -> bool:
    """True only when the panel answered with a refusal, so nothing was changed there."""
    from app.services.pasarguard import PasarGuardError

    status = getattr(exc, "status_code", None) if isinstance(exc, PasarGuardError) else None
    return status is not None and 400 <= int(status) < 500


async def _refund_extra(session: AsyncSession, user: BotUser, purchase: _ExtraPurchase) -> None:
    from app.services.pg_admin_subscription import get_subscription
    from app.services.wallet import credit_wallet

    await rollback_quiet(session)
    sub = await get_subscription(session, purchase.uname)
    if sub is not None:
        sub.extra_gb_purchased = max(0, int(sub.extra_gb_purchased or 0) - purchase.gb)
        sub.extra_users_purchased = max(0, int(sub.extra_users_purchased or 0) - purchase.users)
    await credit_wallet(session, user, purchase.amount, reason=purchase.reason.replace(":", "_refund:", 1), commit=False)
    await session.commit()


async def _apply_paid_extra(
    session: AsyncSession,
    *,
    user: BotUser,
    purchase: _ExtraPurchase,
    apply: Callable[[Any, dict], Awaitable[int]],
) -> int:
    """Charge and record the extra, commit, then raise the panel limit with ``apply``.

    The purchase is committed before the panel call so a crash cannot lose the charge
    or the recorded extra. A refusal from the panel refunds; a timeout or transport
    error keeps the purchase, because the panel may already have applied it, and the
    next capacity sync from the subscription applies it again.
    """
    from app.services.pasarguard import get_pg
    from app.services.pg_admin_subscription import record_unit_extra
    from app.services.wallet import debit_wallet

    await debit_wallet(session, user, purchase.amount, reason=purchase.reason, commit=False)
    await record_unit_extra(session, pg_username=purchase.uname, gb=purchase.gb, users=purchase.users)
    await session.commit()

    pg = get_pg()
    try:
        admin = await pg.get_admin(purchase.uname)
    except Exception as exc:
        logger.warning("extra capacity: reading admin failed admin=%s", purchase.uname, exc_info=True)
        await _refund_extra(session, user, purchase)
        raise ValueError(EXTRA_FAILED_REFUNDED) from exc
    if not isinstance(admin, dict) or not admin:
        await _refund_extra(session, user, purchase)
        raise ValueError("ادمین پاسارگارد یافت نشد — مبلغ بازگردانده شد")
    try:
        return await apply(pg, admin)
    except Exception as exc:
        if _panel_rejected(exc):
            logger.warning("extra capacity refused by panel admin=%s", purchase.uname, exc_info=True)
            await _refund_extra(session, user, purchase)
            raise ValueError(EXTRA_FAILED_REFUNDED) from exc
        logger.error("extra capacity outcome unknown admin=%s reason=%s", purchase.uname, purchase.reason, exc_info=True)
        raise ValueError(EXTRA_OUTCOME_UNKNOWN) from exc


async def buy_extra_gb(
    session: AsyncSession,
    *,
    user: BotUser,
    profile: ResellerProfile,
    plan: ResellerPlan,
    gb: int,
) -> dict[str, Any]:
    if not plan_allows_buy_extra(plan):
        raise ValueError("این پلن امکان خرید حجم اضافه ندارد")
    if is_payg(profile):
        raise ValueError("خرید حجم اضافه برای پلن PAYG فعال نیست")
    try:
        gb_n = int(gb)
    except (TypeError, ValueError) as e:
        raise ValueError("مقدار حجم نامعتبر است") from e
    if gb_n not in ALLOWED_EXTRA_GB:
        raise ValueError("مقدار حجم مجاز نیست")
    price = plan_extra_gb_price(plan)
    if price <= 0:
        raise ValueError("قیمت حجم اضافه برای این پلن تعریف نشده است")

    uname = (profile.pg_admin_username or "").strip()
    if not uname:
        raise ValueError("ادمین پاسارگارد برای این نماینده تنظیم نشده است")

    total = price * gb_n
    # Before the charge and the panel call; the parent stays locked until this purchase commits.
    await assert_child_growth_fits(session, pg_username=uname, add_gb=gb_n)
    purchase = _ExtraPurchase(uname=uname, amount=total, gb=gb_n, users=0, reason=f"reseller_extra_gb:{gb_n}:{uname}")

    async def raise_volume(pg, admin: dict) -> int:
        current = _admin_data_limit(admin)
        # If unlimited (0), start from purchased amount
        new_limit = (current if current > 0 else 0) + gb_n * GB
        await pg.modify_admin(uname, {"data_limit": int(new_limit)})
        return new_limit

    new_limit = await _apply_paid_extra(session, user=user, purchase=purchase, apply=raise_volume)
    return {"gb": gb_n, "amount": total, "data_limit": new_limit}


async def buy_extra_users(
    session: AsyncSession,
    *,
    user: BotUser,
    profile: ResellerProfile,
    plan: ResellerPlan,
    count: int,
) -> dict[str, Any]:
    if not plan_allows_buy_extra(plan):
        raise ValueError("این پلن امکان خرید کاربر اضافه ندارد")
    if is_payg(profile):
        raise ValueError("خرید کاربر اضافه برای پلن PAYG فعال نیست")
    try:
        n = int(count)
    except (TypeError, ValueError) as e:
        raise ValueError("تعداد کاربر نامعتبر است") from e
    if n not in ALLOWED_EXTRA_USERS:
        raise ValueError("تعداد کاربر مجاز نیست")
    price = plan_extra_user_price(plan)
    if price <= 0:
        raise ValueError("قیمت کاربر اضافه برای این پلن تعریف نشده است")

    uname = (profile.pg_admin_username or "").strip()
    if not uname:
        raise ValueError("ادمین پاسارگارد برای این نماینده تنظیم نشده است")

    total = price * n
    purchase = _ExtraPurchase(uname=uname, amount=total, gb=0, users=n, reason=f"reseller_extra_users:{n}:{uname}")

    async def raise_users(pg, admin: dict) -> int:
        new_max = _admin_max_users(admin) + n
        overrides = (
            dict(admin.get("permission_overrides") or {})
            if isinstance(admin.get("permission_overrides"), dict)
            else {}
        )
        overrides["max_users"] = int(new_max)
        await pg.modify_admin(
            uname, {"permission_overrides": overrides, "max_users": int(new_max)}
        )
        return new_max

    new_max = await _apply_paid_extra(session, user=user, purchase=purchase, apply=raise_users)
    return {"users": n, "amount": total, "max_users": new_max}


async def admin_adjust_reseller_subscription(
    session: AsyncSession,
    profile: ResellerProfile,
    *,
    extra_days: int = 0,
    extra_gb: int = 0,
) -> dict[str, Any]:
    """Owner-admin free adjust — thin wrapper over pg_admin_subscription."""
    from app.services.pg_admin_subscription import admin_adjust_reseller_subscription as _adjust

    return await _adjust(
        session, profile, extra_days=extra_days, extra_gb=extra_gb
    )
