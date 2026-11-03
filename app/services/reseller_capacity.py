"""Reseller capacity: renew package / buy extra volume & user slots."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, PgAdminSubscription, ResellerPlan, ResellerProfile
from app.services.billing import GB, is_payg
from app.services.db_safe import rollback_quiet
from app.services.formatting import format_toman
from app.services.reseller_shop_gates import assert_shop_active, assert_subscription_open
from app.services.volume_allocation_cascade import assert_child_growth_fits

logger = logging.getLogger(__name__)

# Whitelist quantities exposed in Telegram buttons — reject forged callback amounts.
ALLOWED_EXTRA_GB = frozenset({1, 5, 10, 50})
ALLOWED_EXTRA_USERS = frozenset({1, 5, 10, 20})
GENERATION_REQUIRED = "این صفحه قدیمی است؛ صفحه را دوباره باز کنید و خرید را تکرار کنید"


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


STALE_PURCHASE = "این صفحه قدیمی است یا خرید دیگری همین حالا انجام شد؛ صفحه را دوباره باز کنید"


@dataclass(slots=True)
class ExtraPurchase:
    uname: str
    amount: int
    gb: int
    users: int
    reason: str
    # The subscription's ``renew_generation`` the buyer saw; None skips the check (older callers).
    generation: int | None = None


async def _claim_generation(session: AsyncSession, purchase: ExtraPurchase) -> None:
    """Atomically move the subscription past the generation the buyer saw, so a repeated tap buys once.

    The claim shares the transaction of the debit: it is committed with the charge or rolled back with it.
    """
    if purchase.generation is None:
        return
    from app.services.pg_admin_subscription import get_or_create_subscription

    await get_or_create_subscription(session, purchase.uname)
    claimed = await session.execute(
        update(PgAdminSubscription)
        .where(
            PgAdminSubscription.pg_username == purchase.uname,
            PgAdminSubscription.renew_generation == int(purchase.generation),
        )
        .values(renew_generation=PgAdminSubscription.renew_generation + 1)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        raise ValueError(STALE_PURCHASE)


def _panel_rejected(exc: Exception) -> bool:
    """True only when the panel answered with a refusal, so nothing was changed there."""
    from app.services.pasarguard import PasarGuardError

    status = getattr(exc, "status_code", None) if isinstance(exc, PasarGuardError) else None
    return status is not None and 400 <= int(status) < 500


async def revert_extras(session: AsyncSession, purchase: ExtraPurchase) -> None:
    """Forget the extra a refused purchase had recorded (the caller commits)."""
    from app.services.pg_admin_subscription import get_subscription

    await rollback_quiet(session)
    sub = await get_subscription(session, purchase.uname)
    if sub is not None:
        sub.extra_gb_purchased = max(0, int(sub.extra_gb_purchased or 0) - purchase.gb)
        sub.extra_users_purchased = max(0, int(sub.extra_users_purchased or 0) - purchase.users)


async def _refund_extra(session: AsyncSession, user: BotUser, purchase: ExtraPurchase) -> None:
    from app.services.wallet import credit_wallet

    await revert_extras(session, purchase)
    await credit_wallet(session, user, purchase.amount, reason=purchase.reason.replace(":", "_refund:", 1), commit=False)
    await session.commit()


class PanelRaiseRefused(Exception):
    """The panel refused (or could not be read before) a limit raise, so it changed nothing."""


class PanelRaiseUnknown(Exception):
    """The panel's answer to a limit raise did not arrive; it may have been applied."""


async def raise_admin_limit(purchase: ExtraPurchase, apply: Callable[[Any, dict], Awaitable[int]]) -> int:
    """Read the admin and raise its live limit with ``apply``; the caller settles the money."""
    from app.services.pasarguard import get_pg

    pg = get_pg()
    try:
        admin = await pg.get_admin(purchase.uname)
    except Exception as exc:
        logger.warning("extra capacity: reading admin failed admin=%s", purchase.uname, exc_info=True)
        raise PanelRaiseRefused(EXTRA_FAILED_REFUNDED) from exc
    if not isinstance(admin, dict) or not admin:
        raise PanelRaiseRefused("ادمین پاسارگارد یافت نشد — مبلغ بازگردانده شد")
    try:
        return await apply(pg, admin)
    except Exception as exc:
        if _panel_rejected(exc):
            logger.warning("extra capacity refused by panel admin=%s", purchase.uname, exc_info=True)
            raise PanelRaiseRefused(EXTRA_FAILED_REFUNDED) from exc
        logger.error("extra capacity outcome unknown admin=%s reason=%s", purchase.uname, purchase.reason, exc_info=True)
        raise PanelRaiseUnknown from exc


async def apply_paid_extra(
    session: AsyncSession,
    *,
    user: BotUser,
    purchase: ExtraPurchase,
    apply: Callable[[Any, dict], Awaitable[int]],
) -> int:
    """Charge and record the extra, commit, then raise the panel limit with ``apply``.

    The purchase is committed before the panel call so a crash cannot lose the charge
    or the recorded extra. A refusal from the panel refunds; a timeout or transport
    error keeps the purchase, because the panel may already have applied it, and the
    next capacity sync from the subscription applies it again.
    """
    from app.services.pg_admin_subscription import record_unit_extra
    from app.services.wallet import debit_wallet

    await _claim_generation(session, purchase)
    await debit_wallet(session, user, purchase.amount, reason=purchase.reason, commit=False)
    await record_unit_extra(session, pg_username=purchase.uname, gb=purchase.gb, users=purchase.users)
    await session.commit()

    try:
        return await raise_admin_limit(purchase, apply)
    except PanelRaiseRefused as exc:
        await _refund_extra(session, user, purchase)
        raise ValueError(str(exc)) from exc
    except PanelRaiseUnknown as exc:
        raise ValueError(EXTRA_OUTCOME_UNKNOWN) from exc


def volume_raiser(uname: str, gb: int) -> Callable[[Any, dict], Awaitable[int]]:
    """Panel write that adds ``gb`` to the admin's live data limit (an unlimited admin starts from zero)."""

    async def raise_volume(pg, admin: dict) -> int:
        current = _admin_data_limit(admin)
        new_limit = (current if current > 0 else 0) + gb * GB
        await pg.modify_admin(uname, {"data_limit": int(new_limit)})
        return new_limit

    return raise_volume


def users_raiser(uname: str, count: int) -> Callable[[Any, dict], Awaitable[int]]:
    """Panel write that adds ``count`` to the admin's live user cap."""

    async def raise_users(pg, admin: dict) -> int:
        new_max = _admin_max_users(admin) + count
        overrides = (
            dict(admin.get("permission_overrides") or {})
            if isinstance(admin.get("permission_overrides"), dict)
            else {}
        )
        overrides["max_users"] = int(new_max)
        await pg.modify_admin(uname, {"permission_overrides": overrides, "max_users": int(new_max)})
        return new_max

    return raise_users


@dataclass(frozen=True, slots=True)
class CapacityOffer:
    """One validated capacity purchase: what it adds, what it costs, and which admin gets it."""

    uname: str
    gb: int
    users: int
    price: int
    reason: str

    def raiser(self) -> Callable[[Any, dict], Awaitable[int]]:
        return volume_raiser(self.uname, self.gb) if self.gb else users_raiser(self.uname, self.users)

    def purchase(self, generation: int | None = None) -> ExtraPurchase:
        return ExtraPurchase(
            uname=self.uname, amount=self.price, gb=self.gb, users=self.users, reason=self.reason, generation=generation,
        )


UNIT_TEXTS = {
    "gb": ("حجم", ALLOWED_EXTRA_GB, plan_extra_gb_price, "reseller_extra_gb"),
    "users": ("کاربر", ALLOWED_EXTRA_USERS, plan_extra_user_price, "reseller_extra_users"),
}


def unit_offer(profile: ResellerProfile, plan: ResellerPlan | None, kind: str, amount: object) -> CapacityOffer:
    """Validate a quick-buy of ``amount`` GB or users; raises ``ValueError`` with a message that is safe to show.

    Does not check subscription clock or shop activity — callers that take money must
    ``assert_shop_active`` and ``assert_subscription_open`` (wallet extras and capacity orders).
    """
    if kind not in UNIT_TEXTS:
        raise ValueError("نوع خرید نامعتبر است")
    noun, allowed, price_of, reason = UNIT_TEXTS[kind]
    if not plan_allows_buy_extra(plan):
        raise ValueError(f"این پلن امکان خرید {noun} اضافه ندارد")
    if is_payg(profile):
        raise ValueError(f"خرید {noun} اضافه برای پلن PAYG فعال نیست")
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise ValueError(f"مقدار {noun} نامعتبر است")
    if amount not in allowed:
        raise ValueError(f"مقدار {noun} مجاز نیست")
    unit_price = price_of(plan)
    if unit_price <= 0:
        raise ValueError(f"قیمت {noun} اضافه برای این پلن تعریف نشده است")
    uname = (profile.pg_admin_username or "").strip()
    if not uname:
        raise ValueError("ادمین پاسارگارد برای این نماینده تنظیم نشده است")
    return CapacityOffer(
        uname=uname, gb=amount if kind == "gb" else 0, users=amount if kind == "users" else 0,
        price=unit_price * amount, reason=f"{reason}:{amount}:{uname}",
    )


def _require_generation(generation: object) -> int:
    """Wallet extras always claim a generation so a double tap cannot charge twice."""
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise ValueError(GENERATION_REQUIRED)
    return int(generation)


async def buy_offer(
    session: AsyncSession, *, user: BotUser, offer: CapacityOffer, generation: int | None = None,
) -> int:
    """Pay ``offer`` from the owner's wallet and raise the admin's live limit; returns the new limit."""
    # Before the charge and the panel call; the parent stays locked until this purchase commits.
    await assert_child_growth_fits(session, pg_username=offer.uname, add_gb=offer.gb)
    return await apply_paid_extra(session, user=user, purchase=offer.purchase(generation), apply=offer.raiser())


async def buy_extra_gb(
    session: AsyncSession,
    *,
    user: BotUser,
    profile: ResellerProfile,
    plan: ResellerPlan,
    gb: int,
    generation: int,
) -> dict[str, Any]:
    """Buy unit GB from the shop wallet; ``generation`` is required (bot, web, Mini App)."""
    assert_shop_active(profile)
    offer = unit_offer(profile, plan, "gb", gb)
    await assert_subscription_open(session, offer.uname)
    new_limit = await buy_offer(
        session, user=user, offer=offer, generation=_require_generation(generation),
    )
    return {"gb": offer.gb, "amount": offer.price, "data_limit": new_limit}


async def buy_extra_users(
    session: AsyncSession,
    *,
    user: BotUser,
    profile: ResellerProfile,
    plan: ResellerPlan,
    count: int,
    generation: int,
) -> dict[str, Any]:
    """Buy unit user slots from the shop wallet; ``generation`` is required (bot, web, Mini App)."""
    assert_shop_active(profile)
    offer = unit_offer(profile, plan, "users", count)
    await assert_subscription_open(session, offer.uname)
    new_max = await buy_offer(
        session, user=user, offer=offer, generation=_require_generation(generation),
    )
    return {"users": offer.users, "amount": offer.price, "max_users": new_max}


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
