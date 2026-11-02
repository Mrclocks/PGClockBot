"""Manual increase or decrease of a reseller's wallet by the platform owner.

A reseller has one wallet (``BotUser.wallet_balance``). For a PAYG reseller it is also the shop
wallet that traffic is charged from, so every manual change goes through the Billing ledger
(``credit_topup`` / ``debit_adjustment``) wherever it is made: reseller page, user page or bot.
A fixed-plan reseller's wallet is changed like any user's. After the change is committed the
reseller is told, and a PAYG wallet emptied by a decrease is suspended at once in the background
instead of on the next billing tick.
"""

from __future__ import annotations

import asyncio
import html
import logging
import secrets
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, ResellerBillingTransaction, ResellerProfile
from app.services.billing import credit_topup, debit_adjustment, is_payg
from app.services.bot_user_admin import (
    MAX_ADMIN_WALLET_CREDIT,
    MIN_ADMIN_WALLET_ADJUST,
    admin_adjust_user_wallet,
    parse_admin_wallet_mode,
)
from app.services.formatting import format_toman

logger = logging.getLogger(__name__)

# Follow-up tasks are kept referenced until they finish so they are not garbage collected.
_followups: set[asyncio.Task[None]] = set()


@dataclass(frozen=True, slots=True)
class WalletChange:
    reseller_user_id: int
    mode: str  # credit | debit
    amount: int
    balance: int
    is_payg: bool
    note: str | None
    # The same form or button sent again: nothing changed, nobody is told twice.
    replayed: bool = False

    @property
    def emptied_payg(self) -> bool:
        return self.is_payg and self.mode == "debit" and self.balance <= 0


async def load_reseller_profile(session: AsyncSession, user_id: int) -> ResellerProfile | None:
    return (
        await session.execute(select(ResellerProfile).where(ResellerProfile.user_id == int(user_id)))
    ).scalar_one_or_none()


def _checked_amount(amount: object) -> int:
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise ValueError("مبلغ باید مثبت باشد")
    if amount < MIN_ADMIN_WALLET_ADJUST:
        raise ValueError(f"حداقل مبلغ {MIN_ADMIN_WALLET_ADJUST:,} تومان است")
    if amount > MAX_ADMIN_WALLET_CREDIT:
        raise ValueError(f"سقف تنظیم دستی {MAX_ADMIN_WALLET_CREDIT:,} تومان است")
    return amount


async def adjust_reseller_wallet(
    session: AsyncSession,
    user: BotUser,
    amount: int,
    *,
    mode: str,
    actor: str,
    note: str | None = None,
    idempotency_key: str | None = None,
) -> WalletChange:
    """Apply and commit one manual change; raise ValueError with a safe message when refused.

    ``idempotency_key`` makes a repeated form or button press a no-op on the PAYG ledger. Without
    one (a form opened before the key existed) a fresh key is used, as such forms always did.
    """
    direction = parse_admin_wallet_mode(mode)
    amt = _checked_amount(amount)
    profile = await load_reseller_profile(session, int(user.id))
    clean_note = (note or "").strip()[:200] or None
    if profile is not None and is_payg(profile):
        key = idempotency_key or f"wallet:{direction}:{int(user.id)}:{secrets.token_hex(8)}"
        replayed = await session.scalar(
            select(ResellerBillingTransaction.id).where(ResellerBillingTransaction.idempotency_key == key)
        ) is not None
        apply = credit_topup if direction == "credit" else debit_adjustment
        default_note = "شارژ دستی ادمین" if direction == "credit" else "کسر دستی ادمین"
        tx = await apply(
            session, int(user.id), amt, created_by=actor[:128], note=clean_note or default_note, idempotency_key=key
        )
        balance = int(tx.balance_after)
    else:
        replayed = False
        await admin_adjust_user_wallet(session, user, amt, mode=direction, actor=actor, note=clean_note)
        await session.refresh(user)
        balance = int(user.wallet_balance or 0)
    return WalletChange(
        reseller_user_id=int(user.id),
        mode=direction,
        amount=amt,
        balance=balance,
        is_payg=profile is not None and is_payg(profile),
        note=clean_note,
        replayed=replayed,
    )


def change_notice(change: WalletChange) -> str:
    """The reseller's Telegram notice; the admin note is escaped customer-facing text."""
    head = "➕ <b>کیف پول شما شارژ شد</b>" if change.mode == "credit" else "➖ <b>از کیف پول شما کسر شد</b>"
    lines = [
        head,
        f"مبلغ: <b>{format_toman(change.amount)}</b>",
        f"موجودی فعلی: <b>{format_toman(change.balance)}</b>",
    ]
    if change.note:
        lines.append(f"توضیح: {html.escape(change.note)}")
    return "\n".join(lines)


async def _run_followups(change: WalletChange) -> None:
    # Own session: the request's session is closed by the time this runs.
    from app.db.session import SessionLocal
    from app.services.billing_suspend import send_reseller_notice, suspend_payg_reseller

    async with SessionLocal() as session:
        if change.emptied_payg:
            try:
                profile = await load_reseller_profile(session, change.reseller_user_id)
                if profile is not None:
                    await suspend_payg_reseller(session, profile, commit=True)
            except Exception:
                # The billing tick suspends an empty wallet anyway; log and carry on.
                logger.exception("PAYG suspend after manual debit failed reseller=%s", change.reseller_user_id)
                await session.rollback()
        try:
            await send_reseller_notice(session, change.reseller_user_id, change_notice(change))
        except Exception:
            logger.warning("wallet change notice failed reseller=%s", change.reseller_user_id, exc_info=True)


def schedule_followups(change: WalletChange) -> None:
    """Notify the reseller and suspend an emptied PAYG wallet, after the change is committed."""
    if change.replayed:
        return
    task = asyncio.get_running_loop().create_task(_run_followups(change), name="reseller-wallet-followup")
    _followups.add(task)
    task.add_done_callback(_followups.discard)
