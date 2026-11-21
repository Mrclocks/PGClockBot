"""Wallet charge codes: create them and redeem one into the current shop's wallet."""

from __future__ import annotations

import re
import secrets
import string
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    BotUser,
    ChargeCode,
)
from app.services.staff_audit import StaffActor


def normalize_charge_code(raw: str) -> str:
    return re.sub(r"\s+", "", (raw or "").strip()).upper()


def generate_charge_code(prefix: str = "GIFT") -> str:
    alphabet = string.ascii_uppercase + string.digits
    body = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"{normalize_charge_code(prefix) or 'GIFT'}-{body}"


async def redeem_charge_code(
    session: AsyncSession,
    *,
    user: BotUser,
    code: str,
) -> tuple[ChargeCode, int]:
    """Credit wallet from a gift/charge code. Returns (code_row, new_balance).

    Uses ``charge_code_uses`` (gift rules) for expiry / caps / concurrency.
    When ``max_uses_per_user`` is unset, wallet redeem stays once-per-user
    (same hardening as the former ``charge_code_redemptions`` unique).
    """
    from app.db.models import ChargeCodeUse
    from app.services.gift_codes import claim_use
    from app.services.users import current_shop_reseller_id
    from app.services.wallet import credit_wallet

    key = normalize_charge_code(code)
    if not key:
        raise ValueError("کد نامعتبر است")
    row = (
        await session.execute(select(ChargeCode).where(ChargeCode.code == key))
    ).scalar_one_or_none()
    if not row or not row.is_active:
        raise ValueError("کد نامعتبر یا غیرفعال است")
    # Scope by current shop bot context — not sticky first-touch user.reseller_id.
    shop_rid = current_shop_reseller_id()
    user_rid = int(shop_rid) if shop_rid is not None else None
    code_rid = int(row.reseller_id) if row.reseller_id else None
    async with session.begin_nested():
        # claim_use locks the code row first; counting earlier uses only after that
        # lock makes a concurrent redeem of the same code wait and then see this one.
        await claim_use(session, row, user_id=user.id, shop_id=user_rid, kind="wallet")
        # Default once-per-user when no explicit per-user cap (limits hardening).
        if row.max_uses_per_user is None:
            prior = (
                await session.execute(
                    select(func.count(ChargeCodeUse.id)).where(
                        ChargeCodeUse.charge_code_id == row.id,
                        ChargeCodeUse.user_id == user.id,
                        ChargeCodeUse.status.in_(("reserved", "consumed")),
                    )
                )
            ).scalar_one()
            if int(prior or 0) >= 1:
                raise ValueError("این کد را قبلاً استفاده کرده‌اید")
        amount = int(row.amount or 0)
        if amount <= 0:
            raise ValueError("مبلغ کد نامعتبر است")
        # Credit and usage record commit together, or both roll back.
        await credit_wallet(
            session, user, amount, f"کد هدیه {row.code}", shop_id=code_rid, commit=False
        )
        session.add(ChargeCodeUse(charge_code_id=row.id, user_id=user.id, status="consumed"))
        await session.flush()
    await session.refresh(row)
    await session.refresh(user)
    from app.services.wallet import get_wallet_balance

    balance = await get_wallet_balance(session, user, shop_id=code_rid)
    return row, int(balance)


# Soft ceilings — blocks absurd minting while keeping normal gift flows usable.
MAX_CHARGE_CODE_AMOUNT = 50_000_000


MAX_CHARGE_CODE_USES = 1000


async def create_charge_code(
    session: AsyncSession,
    *,
    amount: int = 0,
    max_uses: int | None = 1,
    reseller_id: int | None = None,
    note: str | None = None,
    code: str | None = None,
    kind: str = "wallet",
    percent: int = 0,
    max_discount_toman: int | None = None,
    expires_at: datetime | None = None,
    max_uses_per_user: int | None = None,
    first_purchase_only: bool = False,
    purchase_types: str | list[str] | None = None,
    plan_ids: str | list[str] | None = None,
    audit: StaffActor | None = None,
) -> ChargeCode:
    from app.services.gift_codes import create_gift_code

    return await create_gift_code(
        session, amount=amount, max_uses=max_uses, reseller_id=reseller_id,
        note=note, code=code, kind=kind, percent=percent,
        max_discount_toman=max_discount_toman, expires_at=expires_at,
        max_uses_per_user=max_uses_per_user, first_purchase_only=first_purchase_only,
        purchase_types=purchase_types, plan_ids=plan_ids, audit=audit,
    )
