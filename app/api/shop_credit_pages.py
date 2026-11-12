"""Owner controls for a PAYG shop's credit line on the reseller edit page."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CreditStatement, ResellerProfile
from app.services.billing import is_payg
from app.services.credit_statements import write_off_statement
from app.services.numbers import parse_int
from app.services.shop_credit import CreditLimitChange, set_credit_frozen, set_credit_limit

MAX_TEMP_DAYS = 365
FROZEN_SHOWN_FIELD = "frozen_shown"
SAVED_MESSAGE = "اعتبار نماینده ذخیره شد"
NOT_PAYG_MESSAGE = "اعتبار فقط برای نمایندهٔ PAYG است"
WRITTEN_OFF_MESSAGE = "بدهی این صورت‌حساب بخشیده شد"
STATEMENT_MISSING_MESSAGE = "این صورت‌حساب باز نیست"


def _back(user_id: int, *, ok: str = "", err: str = "") -> RedirectResponse:
    query = f"err={quote(err)}" if err else f"ok={quote(ok)}"
    return RedirectResponse(f"/resellers?edit={int(user_id)}&{query}", status_code=303)


def _amount(form: Mapping[str, Any], key: str, *, label: str, optional: bool = False) -> int | None:
    raw = str(form.get(key) or "").strip()
    if not raw:
        return None if optional else 0
    try:
        return parse_int(raw)
    except ValueError:
        raise ValueError(f"«{label}» باید عدد باشد") from None


def credit_change_from_form(form: Mapping[str, Any], *, now: datetime) -> CreditLimitChange:
    """The submitted credit line; raises ``ValueError`` (safe to show)."""
    limit = int(_amount(form, "credit_limit", label="سقف اعتبار") or 0)
    temp = _amount(form, "temp_limit", label="سقف موقت", optional=True)
    days = _amount(form, "temp_limit_days", label="مدت سقف موقت", optional=True)
    if temp is None:
        return CreditLimitChange(limit=limit, note=str(form.get("note") or "")[:255])
    if days is None or days < 1 or days > MAX_TEMP_DAYS:
        raise ValueError(f"مدت سقف موقت باید بین ۱ تا {MAX_TEMP_DAYS} روز باشد")
    return CreditLimitChange(
        limit=limit, temp_limit=temp, temp_limit_until=now + timedelta(days=days),
        note=str(form.get("note") or "")[:255],
    )


async def _payg_shop(session: AsyncSession, user_id: int) -> ResellerProfile | None:
    profile = await session.scalar(select(ResellerProfile).where(ResellerProfile.user_id == int(user_id)))
    return profile if profile is not None and is_payg(profile) else None


def register_shop_credit_pages(app: FastAPI, *, require_admin: Callable, get_db: Callable) -> None:
    @app.post("/resellers/{user_id}/credit")
    async def shop_credit_save(
        user_id: int,
        request: Request,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ) -> RedirectResponse:
        if await _payg_shop(session, user_id) is None:
            return _back(user_id, err=NOT_PAYG_MESSAGE)
        form = await request.form()
        actor = str(staff.get("username") or "owner")
        try:
            change = credit_change_from_form(form, now=datetime.now(timezone.utc))
            await set_credit_limit(session, int(user_id), change, actor=actor)
        except ValueError as exc:
            return _back(user_id, err=str(exc))
        # Hidden marker: an unchecked switch posts nothing, so its absence alone must not unfreeze.
        if form.get(FROZEN_SHOWN_FIELD):
            await set_credit_frozen(session, int(user_id), bool(form.get("frozen")), actor=actor)
        return _back(user_id, ok=SAVED_MESSAGE)

    @app.post("/resellers/{user_id}/credit/statements/{statement_id}/writeoff")
    async def shop_credit_writeoff(
        user_id: int,
        statement_id: int,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ) -> RedirectResponse:
        statement = await session.get(CreditStatement, int(statement_id))
        if statement is None or int(statement.shop_id) != int(user_id):
            return _back(user_id, err=STATEMENT_MISSING_MESSAGE)
        done = await write_off_statement(
            session, int(statement_id), actor=str(staff.get("username") or "owner"), now=datetime.now(timezone.utc)
        )
        return _back(user_id, ok=WRITTEN_OFF_MESSAGE) if done else _back(user_id, err=STATEMENT_MISSING_MESSAGE)
