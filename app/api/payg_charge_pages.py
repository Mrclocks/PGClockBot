"""Owner review of PAYG charges whose panel outcome was unknown."""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser
from app.services.payg_charges import list_review_charges, resolve_review_charge

REVIEW_PATH = "/payg/charges/review"
SETTLED_MESSAGE = "هزینه تأیید شد"
REFUNDED_MESSAGE = "مبلغ به کیف پول نماینده برگشت"
ALREADY_DECIDED_MESSAGE = "این هزینه دیگر در انتظار بازبینی نیست"


def _back(*, ok: str = "", err: str = "") -> RedirectResponse:
    query = f"err={quote(err)}" if err else f"ok={quote(ok)}"
    return RedirectResponse(f"{REVIEW_PATH}?{query}", status_code=303)


def register_payg_charge_pages(
    app: FastAPI, *, render: Callable, require_admin: Callable, get_db: Callable
) -> None:
    @app.get(REVIEW_PATH, response_class=HTMLResponse)
    async def payg_charges_review(
        request: Request,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ) -> HTMLResponse:
        charges = await list_review_charges(session, shop_id=None)
        shop_ids = {int(c.shop_id) for c in charges}
        users = (
            (await session.scalars(select(BotUser).where(BotUser.id.in_(shop_ids)))).all() if shop_ids else []
        )
        names = {int(u.id): (u.full_name or u.username or f"#{int(u.id)}") for u in users}
        return render(request, "payg_charges_review.html", {"charges": charges, "shop_names": names})

    async def _decide(charge_id: int, staff: dict, session: AsyncSession, *, applied: bool) -> RedirectResponse:
        actor = str(staff.get("username") or "owner")
        if not await resolve_review_charge(session, charge_id, applied=applied, actor=actor):
            return _back(err=ALREADY_DECIDED_MESSAGE)
        return _back(ok=SETTLED_MESSAGE if applied else REFUNDED_MESSAGE)

    @app.post("/payg/charges/{charge_id}/settle")
    async def payg_charge_settle(
        charge_id: int,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ) -> RedirectResponse:
        return await _decide(charge_id, staff, session, applied=True)

    @app.post("/payg/charges/{charge_id}/refund")
    async def payg_charge_refund(
        charge_id: int,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ) -> RedirectResponse:
        return await _decide(charge_id, staff, session, applied=False)
