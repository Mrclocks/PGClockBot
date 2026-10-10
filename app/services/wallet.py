from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (
    BotUser,
    Payment,
    PaymentMethod,
    PaymentStatus,
    ShopWallet,
    WalletTransaction,
)

logger = logging.getLogger(__name__)

_METHOD_FA = {
    PaymentMethod.CARD.value: "کارت",
    PaymentMethod.GATEWAY.value: "درگاه",
    PaymentMethod.CRYPTO.value: "رمزارز",
    PaymentMethod.STARS.value: "استارز",
    PaymentMethod.WALLET.value: "کیف پول",
}


@dataclass(frozen=True)
class ActivityLine:
    """Unified wallet / purchase row for «تراکنش‌ها»."""

    amount: int
    reason: str
    created_at: datetime | None
    sort_id: int


def normalize_shop_id(shop_id: int | None) -> int | None:
    """Canonical shop scope: None = platform purse; positive int = shop purse."""
    if shop_id is None:
        return None
    try:
        sid = int(shop_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("شناسه فروشگاه نامعتبر است") from exc
    if sid <= 0:
        raise ValueError("شناسه فروشگاه نامعتبر است")
    return sid


def _payment_activity_reason(payment: Payment) -> str:
    order = payment.order
    method = _METHOD_FA.get(payment.method, payment.method or "پرداخت")
    qty = 1
    note = ""
    oid = payment.order_id
    if order is not None:
        oid = order.id
        note = (order.note or "").strip()
        try:
            qty = int(getattr(order, "quantity", None) or 1)
        except (TypeError, ValueError):
            qty = 1
        if qty < 1 and note.startswith("wholesale:"):
            try:
                qty = int(note.split(":", 1)[1])
            except (TypeError, ValueError):
                qty = 1
    if note.startswith("wholesale:") or qty > 1:
        return f"خرید عمده #{oid} ({max(1, qty)} سرویس · {method})"
    if note.startswith("renew:"):
        return f"تمدید سرویس #{oid} ({method})"
    if note.startswith("reseller_app:"):
        return f"هزینه نمایندگی #{oid} ({method})"
    return f"خرید سفارش #{oid} ({method})"


async def get_wallet_balance(
    session: AsyncSession,
    user: BotUser | int,
    *,
    shop_id: int | None = None,
) -> int:
    """Return balance for platform purse (shop_id=None) or a shop purse."""
    sid = normalize_shop_id(shop_id)
    uid = int(user.id if isinstance(user, BotUser) else user)
    if sid is None:
        if isinstance(user, BotUser):
            return int(user.wallet_balance or 0)
        row = await session.get(BotUser, uid)
        return int(row.wallet_balance or 0) if row else 0
    bal = (
        await session.execute(
            select(ShopWallet.balance).where(
                ShopWallet.user_id == uid,
                ShopWallet.reseller_id == sid,
            )
        )
    ).scalar_one_or_none()
    return int(bal or 0)


async def wallet_balance_for_context(
    session: AsyncSession,
    user: BotUser,
    *,
    shop_id: int | None = None,
) -> int:
    """Balance visible in the current bot context (shop or platform)."""
    if shop_id is None:
        from app.services.users import current_shop_reseller_id

        shop_id = current_shop_reseller_id()
    return await get_wallet_balance(session, user, shop_id=shop_id)


async def wallet_balances_for_scope(
    session: AsyncSession,
    user_ids: list[int],
    *,
    shop_id: int | None = None,
) -> dict[int, int]:
    """Map ``user_id → balance`` for one shop scope (or platform purse).

    ``shop_id=None`` reads ``BotUser.wallet_balance`` only.
    A positive ``shop_id`` reads that shop's ``ShopWallet`` rows and never
    another tenant's purse — missing rows are ``0`` (fail-closed, no leak).
    """
    uids = sorted({int(x) for x in user_ids if x is not None})
    if not uids:
        return {}
    sid = normalize_shop_id(shop_id)
    if sid is None:
        rows = (
            await session.execute(
                select(BotUser.id, BotUser.wallet_balance).where(BotUser.id.in_(uids))
            )
        ).all()
        out = {uid: 0 for uid in uids}
        for uid, bal in rows:
            out[int(uid)] = int(bal or 0)
        return out
    out = {uid: 0 for uid in uids}
    rows = (
        await session.execute(
            select(ShopWallet.user_id, ShopWallet.balance).where(
                ShopWallet.user_id.in_(uids),
                ShopWallet.reseller_id == sid,
            )
        )
    ).all()
    for uid, bal in rows:
        out[int(uid)] = int(bal or 0)
    return out


async def _get_or_create_shop_wallet(
    session: AsyncSession, *, user_id: int, reseller_id: int
) -> ShopWallet:
    """Return shop wallet row, creating a zero-balance row if needed (race-safe)."""
    row = (
        await session.execute(
            select(ShopWallet).where(
                ShopWallet.user_id == int(user_id),
                ShopWallet.reseller_id == int(reseller_id),
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        return row
    try:
        async with session.begin_nested():
            session.add(
                ShopWallet(
                    user_id=int(user_id),
                    reseller_id=int(reseller_id),
                    balance=0,
                )
            )
            await session.flush()
    except IntegrityError:
        pass
    row = (
        await session.execute(
            select(ShopWallet).where(
                ShopWallet.user_id == int(user_id),
                ShopWallet.reseller_id == int(reseller_id),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise ValueError("ساخت کیف فروشگاه ناموفق بود")
    return row


async def credit_wallet(
    session: AsyncSession,
    user: BotUser,
    amount: int,
    reason: str,
    *,
    shop_id: int | None = None,
    commit: bool = True,
) -> BotUser:
    """Credit platform purse (shop_id=None) or an isolated shop purse.

    Shop-sourced money must pass ``shop_id=<reseller owner id>`` so it cannot
    be spent on platform / other-shop orders.
    """
    if amount <= 0:
        raise ValueError("amount must be positive")
    sid = normalize_shop_id(shop_id)
    uid = int(user.id)

    if sid is None:
        with session.no_autoflush:
            result = await session.execute(
                update(BotUser)
                .where(BotUser.id == uid)
                .values(wallet_balance=BotUser.wallet_balance + int(amount))
                .returning(BotUser.wallet_balance)
                .execution_options(synchronize_session=False)
            )
        balance_after = result.scalar_one_or_none()
        if balance_after is None:
            raise ValueError("کاربر یافت نشد")
        balance_after = int(balance_after)
        user.wallet_balance = balance_after
    else:
        row = await _get_or_create_shop_wallet(
            session, user_id=uid, reseller_id=sid
        )
        with session.no_autoflush:
            result = await session.execute(
                update(ShopWallet)
                .where(
                    ShopWallet.id == int(row.id),
                    ShopWallet.user_id == uid,
                    ShopWallet.reseller_id == sid,
                )
                .values(balance=ShopWallet.balance + int(amount))
                .returning(ShopWallet.balance)
                .execution_options(synchronize_session=False)
            )
        balance_after = result.scalar_one_or_none()
        if balance_after is None:
            raise ValueError("کیف فروشگاه یافت نشد")
        balance_after = int(balance_after)
        row.balance = balance_after

    session.add(
        WalletTransaction(
            user_id=uid,
            amount=int(amount),
            balance_after=balance_after,
            reason=reason,
            reseller_id=sid,
        )
    )

    # PAYG auto-unsuspend only for platform / owner purse credits.
    if sid is None:
        try:
            from app.services.billing_suspend import maybe_restore_after_wallet_credit

            await maybe_restore_after_wallet_credit(
                session, uid, int(amount), commit=False
            )
        except Exception:
            logger.exception(
                "PAYG restore-after-wallet-credit failed user=%s", uid
            )

    if commit:
        await session.commit()
        await session.refresh(user)
    else:
        await session.flush()
    return user


async def debit_wallet(
    session: AsyncSession,
    user: BotUser,
    amount: int,
    reason: str,
    *,
    shop_id: int | None = None,
    commit: bool = True,
) -> BotUser:
    """Debit platform purse or the matching shop purse. Fail-closed on shortfall."""
    if amount <= 0:
        raise ValueError("amount must be positive")
    sid = normalize_shop_id(shop_id)
    uid = int(user.id)

    if sid is None:
        with session.no_autoflush:
            result = await session.execute(
                update(BotUser)
                .where(
                    BotUser.id == uid,
                    BotUser.wallet_balance >= int(amount),
                )
                .values(wallet_balance=BotUser.wallet_balance - int(amount))
                .returning(BotUser.wallet_balance)
                .execution_options(synchronize_session=False)
            )
        balance_after = result.scalar_one_or_none()
        if balance_after is None:
            raise ValueError("موجودی کافی نیست")
        balance_after = int(balance_after)
        user.wallet_balance = balance_after
    else:
        with session.no_autoflush:
            result = await session.execute(
                update(ShopWallet)
                .where(
                    ShopWallet.user_id == uid,
                    ShopWallet.reseller_id == sid,
                    ShopWallet.balance >= int(amount),
                )
                .values(balance=ShopWallet.balance - int(amount))
                .returning(ShopWallet.balance)
                .execution_options(synchronize_session=False)
            )
        balance_after = result.scalar_one_or_none()
        if balance_after is None:
            raise ValueError("موجودی کافی نیست")
        balance_after = int(balance_after)

    session.add(
        WalletTransaction(
            user_id=uid,
            amount=-int(amount),
            balance_after=balance_after,
            reason=reason,
            reseller_id=sid,
        )
    )
    if commit:
        await session.commit()
        await session.refresh(user)
    else:
        await session.flush()
    return user


async def list_transactions(
    session: AsyncSession,
    user_id: int,
    limit: int = 20,
    *,
    shop_id: int | None = None,
):
    """Ledger rows for one purse (platform or a specific shop)."""
    sid = normalize_shop_id(shop_id)
    q = select(WalletTransaction).where(WalletTransaction.user_id == int(user_id))
    if sid is None:
        q = q.where(WalletTransaction.reseller_id.is_(None))
    else:
        q = q.where(WalletTransaction.reseller_id == sid)
    result = await session.execute(
        q.order_by(WalletTransaction.id.desc()).limit(limit)
    )
    return list(result.scalars().all())


async def list_activity(
    session: AsyncSession,
    user_id: int,
    limit: int = 20,
    *,
    shop_id: int | None = None,
) -> list[ActivityLine]:
    """Wallet ledger plus approved non-wallet purchases for the same shop scope."""
    from app.db.models import Order

    if shop_id is None:
        from app.services.users import current_shop_reseller_id

        shop_id = current_shop_reseller_id()
    sid = normalize_shop_id(shop_id)

    wtxs = await list_transactions(session, user_id, limit=limit, shop_id=sid)
    lines: list[ActivityLine] = [
        ActivityLine(
            amount=int(t.amount),
            reason=str(t.reason or ""),
            created_at=getattr(t, "created_at", None),
            sort_id=int(t.id) * 2,
        )
        for t in wtxs
    ]
    pay_q = (
        select(Payment)
        .where(
            Payment.user_id == user_id,
            Payment.status == PaymentStatus.APPROVED.value,
            Payment.is_wallet_topup.is_(False),
            Payment.method != PaymentMethod.WALLET.value,
        )
        .options(selectinload(Payment.order))
        .order_by(Payment.id.desc())
        .limit(limit)
    )
    # Scope non-wallet purchases to the same shop the wallet UI is showing.
    if sid is None:
        pay_q = pay_q.outerjoin(Order, Order.id == Payment.order_id).where(
            (Payment.order_id.is_(None)) | (Order.reseller_id.is_(None))
        )
    else:
        pay_q = pay_q.join(Order, Order.id == Payment.order_id).where(
            Order.reseller_id == sid
        )
    result = await session.execute(pay_q)
    for p in result.scalars().all():
        lines.append(
            ActivityLine(
                amount=-abs(int(p.amount or 0)),
                reason=_payment_activity_reason(p),
                created_at=getattr(p, "created_at", None),
                sort_id=int(p.id) * 2 + 1,
            )
        )

    def _sort_key(x: ActivityLine):
        ts = x.created_at
        if ts is None:
            return (0, 0, x.sort_id)
        try:
            epoch = ts.timestamp()
        except Exception:
            epoch = 0
        return (1, epoch, x.sort_id)

    lines.sort(key=_sort_key, reverse=True)
    return lines[:limit]
