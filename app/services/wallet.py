from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import BotUser, Payment, PaymentMethod, PaymentStatus, WalletTransaction

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


async def credit_wallet(
    session: AsyncSession,
    user: BotUser,
    amount: int,
    reason: str,
) -> BotUser:
    if amount <= 0:
        raise ValueError("amount must be positive")
    with session.no_autoflush:
        result = await session.execute(
            update(BotUser)
            .where(BotUser.id == user.id)
            .values(wallet_balance=BotUser.wallet_balance + int(amount))
            .execution_options(synchronize_session=False)
        )
    if result.rowcount != 1:
        raise ValueError("کاربر یافت نشد")
    await session.refresh(user)
    session.add(
        WalletTransaction(
            user_id=user.id,
            amount=amount,
            balance_after=user.wallet_balance,
            reason=reason,
        )
    )
    await session.commit()
    await session.refresh(user)
    return user


async def debit_wallet(
    session: AsyncSession,
    user: BotUser,
    amount: int,
    reason: str,
) -> BotUser:
    if amount <= 0:
        raise ValueError("amount must be positive")
    with session.no_autoflush:
        result = await session.execute(
            update(BotUser)
            .where(
                BotUser.id == user.id,
                BotUser.wallet_balance >= int(amount),
            )
            .values(wallet_balance=BotUser.wallet_balance - int(amount))
            .execution_options(synchronize_session=False)
        )
    if result.rowcount != 1:
        raise ValueError("موجودی کافی نیست")
    await session.refresh(user)
    session.add(
        WalletTransaction(
            user_id=user.id,
            amount=-amount,
            balance_after=user.wallet_balance,
            reason=reason,
        )
    )
    await session.commit()
    await session.refresh(user)
    return user


async def list_transactions(session: AsyncSession, user_id: int, limit: int = 20):
    result = await session.execute(
        select(WalletTransaction)
        .where(WalletTransaction.user_id == user_id)
        .order_by(WalletTransaction.id.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def list_activity(session: AsyncSession, user_id: int, limit: int = 20) -> list[ActivityLine]:
    """Wallet ledger plus approved non-wallet purchases (card/gateway/crypto/stars).

    Wallet checkouts already create a WalletTransaction debit — those payments are
    not duplicated here. Wholesale and other card/crypto buys previously never
    appeared in «تراکنش‌ها»; they are included via Payment rows.
    """
    wtxs = await list_transactions(session, user_id, limit=limit)
    lines: list[ActivityLine] = [
        ActivityLine(
            amount=int(t.amount),
            reason=str(t.reason or ""),
            created_at=getattr(t, "created_at", None),
            sort_id=int(t.id) * 2,
        )
        for t in wtxs
    ]
    result = await session.execute(
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
            epoch = 0.0
        return (1, epoch, x.sort_id)

    lines.sort(key=_sort_key, reverse=True)
    return lines[:limit]
