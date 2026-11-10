"""Owner report of PAYG shops: wallet, credit, this month's charges, open debt and cashback.

Every figure is aggregated in SQL with one grouped query per figure (no query per shop)
over a bounded list of shops. The CSV export neutralises spreadsheet formulas.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, CashbackGrant, CreditStatement, ResellerCharge, ResellerProfile, ShopCreditAccount
from app.services.billing import BILLING_MODE_PAYG

SHOP_LIMIT = 500
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


@dataclass(frozen=True, slots=True)
class ShopFinanceRow:
    shop_id: int
    label: str
    balance: int
    credit_limit: int
    credit_status: str
    month_charges: int
    open_debt: int
    month_cashback: int

    @property
    def debt(self) -> int:
        return max(0, -self.balance)


async def _grouped(session: AsyncSession, query) -> dict[int, int]:
    return {int(shop): int(total or 0) for shop, total in (await session.execute(query)).all()}


async def shop_finance_rows(session: AsyncSession, *, month_start: datetime) -> list[ShopFinanceRow]:
    profiles = (await session.execute(
        select(ResellerProfile.user_id, ResellerProfile.pg_admin_username, ResellerProfile.web_username)
        .where(ResellerProfile.billing_mode == BILLING_MODE_PAYG)
        .order_by(ResellerProfile.user_id).limit(SHOP_LIMIT)
    )).all()
    ids = [int(row.user_id) for row in profiles]
    if not ids:
        return []
    balances = await _grouped(session, select(BotUser.id, BotUser.wallet_balance).where(BotUser.id.in_(ids)))
    accounts = {int(a.shop_id): a for a in (await session.scalars(
        select(ShopCreditAccount).where(ShopCreditAccount.shop_id.in_(ids)))).all()}
    charges = await _grouped(session, select(ResellerCharge.shop_id, func.sum(ResellerCharge.amount)).where(
        ResellerCharge.shop_id.in_(ids), ResellerCharge.created_at >= month_start,
        ResellerCharge.status != "refunded").group_by(ResellerCharge.shop_id))
    debt = await _grouped(session, select(CreditStatement.shop_id, func.sum(CreditStatement.amount_due)).where(
        CreditStatement.shop_id.in_(ids), CreditStatement.status == "issued").group_by(CreditStatement.shop_id))
    cashback = await _grouped(session, select(CashbackGrant.shop_id, func.sum(CashbackGrant.amount)).where(
        CashbackGrant.shop_id.in_(ids), CashbackGrant.created_at >= month_start,
        CashbackGrant.status.in_(["pending", "available"])).group_by(CashbackGrant.shop_id))
    rows = []
    for row in profiles:
        shop = int(row.user_id)
        account = accounts.get(shop)
        rows.append(ShopFinanceRow(
            shop_id=shop, label=str(row.pg_admin_username or row.web_username or f"#{shop}"),
            balance=balances.get(shop, 0), credit_limit=int(account.credit_limit or 0) if account else 0,
            credit_status=str(account.status) if account else "", month_charges=charges.get(shop, 0),
            open_debt=debt.get(shop, 0), month_cashback=cashback.get(shop, 0),
        ))
    return rows


def _cell(value: object) -> str:
    text = str(value)
    return f"'{text}" if text.startswith(_FORMULA_START) else text


def rows_csv(rows: list[ShopFinanceRow]) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["shop_id", "shop", "balance", "debt", "credit_limit", "credit_status", "month_charges",
                     "open_statement_debt", "month_cashback"])
    for r in rows:
        writer.writerow([_cell(v) for v in (r.shop_id, r.label, r.balance, r.debt, r.credit_limit, r.credit_status,
                                             r.month_charges, r.open_debt, r.month_cashback)])
    return out.getvalue()
