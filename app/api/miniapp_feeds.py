"""Mini App screen feeds: paged services, the plan catalog, wallet activity and ops.

Each screen loads only its own data, so a launch never reads the panel for every
service. The invariants of ``miniapp_pages`` hold here too: the caller is the
``BotUser`` from initData, services are scoped to that customer and the platform
shop, and the catalog is platform plans only.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.miniapp_pages import (
    _addons_allowed,
    _admin_ops_payload,
    _enrich_services,
    _no_store,
    _require_commerce,
    _reseller_ops_payload,
)
from app.db.models import Plan
from app.services.catalog_pricing import annotate_prices, displayed_quote
from app.services.db_safe import rollback_quiet
from app.services.miniapp_auth import load_mini_user, load_reseller_profile, resolve_mini_persona
from app.services.miniapp_shop import mini_scope
from app.services.receipt_review import may_review_receipts
from app.services.service_scope import SERVICE_SEARCH_MAX_LENGTH, page_customer_services
from app.services.users import get_all_settings, on
from app.services.wallet import list_activity

log = logging.getLogger(__name__)

# Each listed service costs one panel read, so a page stays small.
SERVICES_PAGE_DEFAULT = 6
SERVICES_PAGE_MAX = 10
ACTIVITY_DEFAULT = 20
ACTIVITY_MAX = 50
_ID_MAX = 2**31 - 1


async def catalog_plans_payload(session: AsyncSession, *, shop_id: int | None = None) -> dict:
    """Active plans of this Mini App's shop (its trial first) with their displayed quote.

    ``shop_id`` None is the platform catalog; a shop sees only the plans it sells.
    """
    ui = await get_all_settings(session, reseller_id=shop_id)
    owner = Plan.owner_reseller_id.is_(None) if shop_id is None else Plan.owner_reseller_id == int(shop_id)
    platform = (Plan.is_active.is_(True), owner)
    trial = await session.scalar(
        select(Plan).where(*platform, Plan.is_trial.is_(True)).order_by(Plan.id).limit(1)
    )
    plans = list(
        await session.scalars(
            select(Plan)
            .where(*platform, Plan.is_trial.is_(False))
            .order_by(Plan.sort_order, Plan.id)
        )
    )
    await annotate_prices(session, plans, shop_id=shop_id)
    return {
        "wallet_pay_enabled": on(ui.get("pay_wallet_enabled")),
        "plans": [
            {
                "id": p.id,
                "name": p.name,
                **displayed_quote(p).payload(),
                "days": p.duration_days,
                "gb": p.data_limit_gb,
                "is_trial": bool(p.is_trial),
            }
            for p in (([trial] if trial else []) + plans)
        ],
    }


def _activity_row(line) -> dict:
    created = line.created_at
    if isinstance(created, datetime):
        created = (created if created.tzinfo else created.replace(tzinfo=timezone.utc)).isoformat()
    else:
        created = None
    return {"amount": int(line.amount), "reason": line.reason or "", "created_at": created}


def register_miniapp_feeds(app: FastAPI, *, get_db) -> None:
    @app.get("/api/mini/services")
    async def mini_services(
        request: Request,
        before_id: int | None = Query(None, ge=1, le=_ID_MAX),
        limit: int = Query(SERVICES_PAGE_DEFAULT, ge=1, le=SERVICES_PAGE_MAX),
        q: str | None = Query(None, max_length=SERVICE_SEARCH_MAX_LENGTH),
        session: AsyncSession = Depends(get_db),
    ):
        user = await load_mini_user(session, request)
        _require_commerce(user)
        services, has_more = await page_customer_services(
            session, int(user.id), mini_scope(request).shop_id, limit=limit, before_id=before_id, text=q
        )
        rows = await _enrich_services(services, session=session)
        for row, svc in zip(rows, services, strict=True):
            row["addons_allowed"] = _addons_allowed(user, svc)
        return _no_store({
            "items": rows,
            "next_before_id": int(services[-1].id) if has_more and services else None,
        })

    @app.get("/api/mini/plans")
    async def mini_plans(request: Request, session: AsyncSession = Depends(get_db)):
        user = await load_mini_user(session, request)
        _require_commerce(user)
        return _no_store(await catalog_plans_payload(session, shop_id=mini_scope(request).shop_id))

    @app.get("/api/mini/activity")
    async def mini_activity(
        request: Request,
        limit: int = Query(ACTIVITY_DEFAULT, ge=1, le=ACTIVITY_MAX),
        session: AsyncSession = Depends(get_db),
    ):
        user = await load_mini_user(session, request)
        _require_commerce(user)
        try:
            lines = await list_activity(session, int(user.id), limit=limit, shop_id=mini_scope(request).shop_id)
        except SQLAlchemyError:
            await rollback_quiet(session)
            log.exception("miniapp activity failed user=%s", user.id)
            raise HTTPException(503, "دریافت تراکنش‌ها ناموفق بود؛ دوباره تلاش کنید") from None
        return _no_store({"items": [_activity_row(line) for line in lines]})

    @app.get("/api/mini/ops")
    async def mini_ops(request: Request, session: AsyncSession = Depends(get_db)):
        user = await load_mini_user(session, request)
        persona = resolve_mini_persona(user)
        if persona == "admin":
            payload = await _admin_ops_payload(session)
        elif persona == "reseller":
            profile = await load_reseller_profile(session, user)
            payload = await _reseller_ops_payload(session, profile, user=user)
        else:
            raise HTTPException(403, "این بخش برای حساب شما فعال نیست")
        return _no_store({**payload, "can_review": await may_review_receipts(session, user)})
