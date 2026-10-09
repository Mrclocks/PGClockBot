"""Shop tools for cancellation review and targeted campaigns."""
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.db.models import TargetedCampaign
from app.services.authz import authz_from_staff, can_shop
from app.services.campaigns import (
    AUDIENCES,
    RECIPIENT_LIMIT,
    audience_count,
    campaign_progress,
    change_campaign_state,
    create_campaign,
)
from app.services.panel_sidebar_cache import invalidate_sidebar_counts, sidebar_cache_key
from app.services.service_cancellations import approve_cancellation, reject_cancellation
from app.services.shop_scope import ShopScopeError, resolve_shop_scope_id

FINANCE_CANCEL_PATH = "/finance?tab=cancellations"


def feature_scope(staff, permission):
    if not can_shop(authz_from_staff(staff), permission):
        raise HTTPException(403)
    try:
        return resolve_shop_scope_id(staff)
    except ShopScopeError:
        raise HTTPException(403) from None


def safe_flash_error(message: object, *, limit: int = 180) -> str:
    """Single-line operator-safe flash for redirects (no raw exception dumps)."""
    text = " ".join(str(message or "").split())
    return text[:limit]


def redirect(path, error=""):
    flash = safe_flash_error(error)
    if not flash:
        return RedirectResponse(path, status_code=303)
    sep = "&" if "?" in path else "?"
    return RedirectResponse(path + sep + "error=" + quote(flash), status_code=303)


def register_customer_features(app, *, render, require_staff, get_db):
    @app.get("/service-cancellations")
    async def cancellations(request: Request, staff=Depends(require_staff)):
        # Auth first so scopeless staff still get 403 (not a silent redirect).
        feature_scope(staff, "orders")
        before = (request.query_params.get("before") or "").strip()
        target = FINANCE_CANCEL_PATH
        if before.isdigit():
            target += f"&before={int(before)}"
        return RedirectResponse(target, status_code=303)

    @app.post("/service-cancellations/{request_id}/{action}")
    async def cancellation_review(
        request: Request,
        request_id: int,
        action: str,
        staff=Depends(require_staff),
        session=Depends(get_db),
    ):
        scope = feature_scope(staff, "orders")
        form = await request.form()
        actor = str(
            staff.get("org_principal_id")
            or staff.get("username")
            or staff.get("bot_user_id")
            or "operator"
        )[:128]
        try:
            if action == "approve":
                feature_scope(staff, "payments")
                raw = str(form.get("amount", ""))
                if not raw.isascii() or not raw.isdigit() or len(raw) > 13:
                    raise ValueError("مبلغ را به تومان و با ارقام انگلیسی وارد کنید")
                await approve_cancellation(
                    session,
                    request_id,
                    shop_id=scope,
                    amount=int(raw),
                    actor=actor,
                    note=str(form.get("note", "")),
                )
            elif action == "reject":
                await reject_cancellation(
                    session,
                    request_id,
                    shop_id=scope,
                    actor=actor,
                    note=str(form.get("note", "")),
                )
            else:
                raise ValueError("عملیات نامعتبر است")
        except ValueError as exc:
            await session.rollback()
            return redirect(FINANCE_CANCEL_PATH, str(exc))
        invalidate_sidebar_counts(sidebar_cache_key(staff))
        return redirect(FINANCE_CANCEL_PATH)

    @app.get("/campaigns")
    async def campaigns(request: Request, staff=Depends(require_staff), session=Depends(get_db)):
        scope = feature_scope(staff, "campaigns")
        audience = request.query_params.get("audience", "trial")
        try:
            days = int(request.query_params.get("days", "7"))
            count = await audience_count(
                session, shop_id=scope, audience=audience, days=days
            )
        except ValueError:
            raise HTTPException(400, "گروه یا بازهٔ مخاطبان نامعتبر است") from None
        rows = list(
            (
                await session.scalars(
                    select(TargetedCampaign)
                    .where(TargetedCampaign.reseller_id == scope)
                    .order_by(TargetedCampaign.id.desc())
                    .limit(50)
                )
            ).all()
        )
        # Progress only for campaigns already scoped to this shop — never by bare id.
        history = [(row, await campaign_progress(session, row.id)) for row in rows]
        return render(
            request,
            "customer_features.html",
            {
                "staff": staff,
                "mode": "campaigns",
                "audiences": AUDIENCES,
                "audience": audience,
                "days": days,
                "count": count,
                "limit": RECIPIENT_LIMIT,
                "campaigns": history,
            },
        )

    @app.post("/campaigns")
    async def campaign_create(
        request: Request, staff=Depends(require_staff), session=Depends(get_db)
    ):
        scope = feature_scope(staff, "campaigns")
        form = await request.form()
        try:
            row = await create_campaign(
                session,
                shop_id=scope,
                audience=str(form.get("audience", "")),
                days=int(str(form.get("days", "7"))),
                text=str(form.get("text", "")),
                actor=str(staff.get("username") or staff.get("bot_user_id") or "operator")[
                    :128
                ],
            )
        except ValueError as exc:
            await session.rollback()
            msg = str(exc)
            safe = msg if msg.startswith(("متن", "گروه", "مخاطب")) else "بازهٔ روز نامعتبر است"
            return redirect("/campaigns", safe)
        return redirect(f"/campaigns#campaign-{row.id}")

    @app.post("/campaigns/{campaign_id}/{action}")
    async def campaign_control(
        campaign_id: int,
        action: str,
        staff=Depends(require_staff),
        session=Depends(get_db),
    ):
        scope = feature_scope(staff, "campaigns")
        try:
            await change_campaign_state(
                session, campaign_id, shop_id=scope, action=action
            )
        except ValueError as exc:
            await session.rollback()
            return redirect("/campaigns", str(exc))
        return redirect(f"/campaigns#campaign-{campaign_id}")
