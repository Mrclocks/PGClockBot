"""One-time repair for pre-isolation shop wallet ledger rows (N2 / N3).

N2 — claw back synthetic shop-sourced credits that still sit on the platform
purse (gift / lucky-wheel / loyalty / referral minted before shop_wallets).

N3 — move real shop top-up credits from the platform purse into the matching
``shop_wallets`` row so customers can spend what they paid in the shop bot.

Both steps are idempotent via marker reasons on ``wallet_transactions``.
Sync helpers run inside Alembic; async wrappers serve tests / app callers.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db.models import (
    BotUser,
    ChargeCode,
    LoyaltyReward,
    LuckyWheelSpin,
    Payment,
    ShopWallet,
    WalletTransaction,
)
from app.services.charge_codes import MAX_CHARGE_CODE_AMOUNT, MAX_CHARGE_CODE_USES

logger = logging.getLogger(__name__)

_GIFT_REASON_RE = re.compile(r"^(?:کد هدیه|gift)\s+(\S+)$", re.IGNORECASE)
_LUCKY_REASON_RE = re.compile(r"^lucky_wheel:(\d+):")
_LOYALTY_REASON_RE = re.compile(r"^loyalty_reward:(\d+):")
_REFERRAL_REASON_RE = re.compile(r"^referral:(\d+)$")
_TOPUP_REASON_RE = re.compile(r"^شارژ کیف پول #(\d+)$")

_CLAWBACK_PREFIX = "clawback:legacy_shop:"
_MIGRATE_DEBIT_PREFIX = "migrate:shop_topup:"
_MIGRATE_CREDIT_PREFIX = "migrate:shop_topup_credit:"


def _marker_exists_sync(session: Session, reason: str) -> bool:
    row = session.execute(
        select(WalletTransaction.id).where(WalletTransaction.reason == reason).limit(1)
    ).scalar_one_or_none()
    return row is not None


def _get_or_create_shop_wallet_sync(
    session: Session, *, user_id: int, reseller_id: int
) -> ShopWallet:
    row = session.execute(
        select(ShopWallet).where(
            ShopWallet.user_id == int(user_id),
            ShopWallet.reseller_id == int(reseller_id),
        )
    ).scalar_one_or_none()
    if row is not None:
        return row
    row = ShopWallet(user_id=int(user_id), reseller_id=int(reseller_id), balance=0)
    session.add(row)
    session.flush()
    return row


def _debit_platform_sync(
    session: Session, user: BotUser, amount: int, reason: str
) -> None:
    amt = int(amount)
    if amt <= 0:
        raise ValueError("amount must be positive")
    result = session.execute(
        update(BotUser)
        .where(BotUser.id == int(user.id), BotUser.wallet_balance >= amt)
        .values(wallet_balance=BotUser.wallet_balance - amt)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise ValueError("موجودی کافی نیست")
    session.refresh(user)
    session.add(
        WalletTransaction(
            user_id=int(user.id),
            amount=-amt,
            balance_after=int(user.wallet_balance or 0),
            reason=reason,
            reseller_id=None,
        )
    )


def _credit_shop_sync(
    session: Session, user: BotUser, amount: int, reason: str, *, shop_id: int
) -> None:
    amt = int(amount)
    if amt <= 0:
        raise ValueError("amount must be positive")
    row = _get_or_create_shop_wallet_sync(
        session, user_id=int(user.id), reseller_id=int(shop_id)
    )
    result = session.execute(
        update(ShopWallet)
        .where(
            ShopWallet.id == int(row.id),
            ShopWallet.user_id == int(user.id),
            ShopWallet.reseller_id == int(shop_id),
        )
        .values(balance=ShopWallet.balance + amt)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise ValueError("کیف فروشگاه یافت نشد")
    session.refresh(row)
    session.add(
        WalletTransaction(
            user_id=int(user.id),
            amount=amt,
            balance_after=int(row.balance or 0),
            reason=reason,
            reseller_id=int(shop_id),
        )
    )


def _shop_id_for_synthetic_tx_sync(
    session: Session, tx: WalletTransaction
) -> int | None:
    reason = (tx.reason or "").strip()
    m = _GIFT_REASON_RE.match(reason)
    if m:
        code = m.group(1)
        rid = session.execute(
            select(ChargeCode.reseller_id).where(ChargeCode.code == code).limit(1)
        ).scalar_one_or_none()
        return int(rid) if rid else None

    m = _LUCKY_REASON_RE.match(reason)
    if m:
        spin = session.get(LuckyWheelSpin, int(m.group(1)))
        if spin and spin.reseller_id:
            return int(spin.reseller_id)
        return None

    m = _LOYALTY_REASON_RE.match(reason)
    if m:
        reward = session.get(LoyaltyReward, int(m.group(1)))
        if reward and reward.reseller_id:
            return int(reward.reseller_id)
        return None

    m = _REFERRAL_REASON_RE.match(reason)
    if m:
        buyer = session.get(BotUser, int(m.group(1)))
        if buyer and buyer.reseller_id:
            return int(buyer.reseller_id)
        return None

    return None


def _shop_id_for_topup_tx_sync(session: Session, tx: WalletTransaction) -> int | None:
    m = _TOPUP_REASON_RE.match((tx.reason or "").strip())
    if not m:
        return None
    payment = session.get(Payment, int(m.group(1)))
    if not payment or not payment.is_wallet_topup:
        return None
    if getattr(payment, "wallet_shop_id", None):
        return int(payment.wallet_shop_id)
    user = session.get(BotUser, int(tx.user_id))
    if user and user.reseller_id:
        return int(user.reseller_id)
    return None


def deactivate_oversized_shop_gift_codes_sync(session: Session) -> int:
    result = session.execute(
        update(ChargeCode)
        .where(
            ChargeCode.reseller_id.is_not(None),
            ChargeCode.is_active.is_(True),
        )
        .where(
            (ChargeCode.amount > MAX_CHARGE_CODE_AMOUNT)
            | (ChargeCode.max_uses.is_(None))
            | (ChargeCode.max_uses > MAX_CHARGE_CODE_USES)
        )
        .values(is_active=False)
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)


def clawback_legacy_shop_synthetic_credits_sync(session: Session) -> dict[str, int]:
    txs = list(
        session.execute(
            select(WalletTransaction)
            .where(
                WalletTransaction.reseller_id.is_(None),
                WalletTransaction.amount > 0,
            )
            .order_by(WalletTransaction.id.asc())
        )
        .scalars()
        .all()
    )
    clawed = skipped = 0
    for tx in txs:
        shop_id = _shop_id_for_synthetic_tx_sync(session, tx)
        if not shop_id:
            continue
        marker = f"{_CLAWBACK_PREFIX}{int(tx.id)}"
        if _marker_exists_sync(session, marker):
            skipped += 1
            continue
        user = session.get(BotUser, int(tx.user_id))
        if not user:
            skipped += 1
            continue
        amt = int(tx.amount or 0)
        take = min(amt, int(user.wallet_balance or 0))
        if take <= 0:
            skipped += 1
            continue
        try:
            _debit_platform_sync(session, user, take, marker)
            clawed += 1
        except ValueError:
            skipped += 1
    return {"clawed": clawed, "skipped": skipped}


def migrate_legacy_shop_topups_sync(session: Session) -> dict[str, int]:
    txs = list(
        session.execute(
            select(WalletTransaction)
            .where(
                WalletTransaction.reseller_id.is_(None),
                WalletTransaction.amount > 0,
            )
            .order_by(WalletTransaction.id.asc())
        )
        .scalars()
        .all()
    )
    moved = skipped = 0
    for tx in txs:
        shop_id = _shop_id_for_topup_tx_sync(session, tx)
        if not shop_id:
            continue
        debit_marker = f"{_MIGRATE_DEBIT_PREFIX}{int(tx.id)}"
        credit_marker = f"{_MIGRATE_CREDIT_PREFIX}{int(tx.id)}"
        if _marker_exists_sync(session, debit_marker) or _marker_exists_sync(
            session, credit_marker
        ):
            skipped += 1
            continue
        user = session.get(BotUser, int(tx.user_id))
        if not user:
            skipped += 1
            continue
        amt = int(tx.amount or 0)
        take = min(amt, int(user.wallet_balance or 0))
        if take <= 0:
            skipped += 1
            continue
        try:
            _debit_platform_sync(session, user, take, debit_marker)
            _credit_shop_sync(session, user, take, credit_marker, shop_id=shop_id)
            moved += 1
        except ValueError:
            skipped += 1
    return {"moved": moved, "skipped": skipped}


def repair_legacy_wallet_isolation_sync(session: Session) -> dict[str, Any]:
    deactivated = deactivate_oversized_shop_gift_codes_sync(session)
    clawback = clawback_legacy_shop_synthetic_credits_sync(session)
    migrate = migrate_legacy_shop_topups_sync(session)
    stats = {
        "gift_codes_deactivated": deactivated,
        "clawback": clawback,
        "migrate": migrate,
    }
    logger.info("legacy wallet isolation repair: %s", stats)
    return stats


async def repair_legacy_wallet_isolation(session: AsyncSession) -> dict[str, Any]:
    """Async wrapper for tests / callers that already hold an AsyncSession."""
    return await session.run_sync(repair_legacy_wallet_isolation_sync)
