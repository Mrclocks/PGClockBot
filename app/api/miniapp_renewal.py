"""Mini App renewal choices: the service's own plan, then the shop's categories, like the bot.

The service must be the Mini App user's and sold by this Mini App's shop; the plans and categories
come from that shop's catalog only. Picking a plan still goes through the renewal preview and the
wallet payment endpoints, which re-check the plan and the service.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.miniapp_feeds import catalog_plan_row
from app.api.miniapp_pages import _no_store, _owned_service_or_404, _require_commerce_ready, _scope_ui
from app.db.models import UserService
from app.services.miniapp_auth import load_mini_user
from app.services.miniapp_shop import mini_scope
from app.services.service_renewals import RenewalChoices, load_renewal_choices
from app.services.users import on

LINKED_NOT_RENEWABLE = "سرویس متصل‌شده فقط مشاهده است؛ تمدید از این مسیر ممکن نیست"


def _plan_row(choices: RenewalChoices, plan: object) -> dict:
    return {**catalog_plan_row(plan), "relation": choices.relations.get(int(plan.id))}


def renewal_options_payload(choices: RenewalChoices, *, wallet_pay_enabled: bool) -> dict:
    """Grouped when the shop has categories (current plan, categories, «سایر»); otherwise one list."""
    grouped = bool(choices.categories)
    return {
        "wallet_pay_enabled": wallet_pay_enabled,
        "grouped": grouped,
        "current": _plan_row(choices, choices.current) if choices.current else None,
        "categories": [
            {"id": int(c.id), "name": c.name, "plans": [_plan_row(choices, p) for p in choices.in_category(int(c.id))]}
            for c in choices.categories
        ] if grouped else [],
        "other": [_plan_row(choices, p) for p in choices.uncategorized] if grouped else [],
        "plans": [] if grouped else [_plan_row(choices, p) for p in choices.options],
    }


def register_miniapp_renewal(app: FastAPI, *, get_db) -> None:
    @app.get("/api/mini/service/{service_id}/renewal-options")
    async def mini_renewal_options(service_id: int, request: Request, session: AsyncSession = Depends(get_db)):
        user = await load_mini_user(session, request)
        await _require_commerce_ready(session, user)
        shop_id = mini_scope(request).shop_id
        svc = await _owned_service_or_404(session, await session.get(UserService, service_id), user, shop_id=shop_id)
        if (svc.remark or "").strip() == "linked":
            raise HTTPException(400, LINKED_NOT_RENEWABLE)
        choices = await load_renewal_choices(session, svc, shop_id=shop_id)
        ui = await _scope_ui(session, request)
        return _no_store(renewal_options_payload(choices, wallet_pay_enabled=on(ui.get("pay_wallet_enabled"))))
