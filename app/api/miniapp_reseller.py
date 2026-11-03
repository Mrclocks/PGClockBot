"""Mini App «اشتراک نمایندگی»: a shop owner's own package, wallet and capacity packs.

Only the platform Mini App reaches these routes (they are not in ``SHOP_ROUTES``) and only for the
account whose role is ``reseller``. The shop is the caller's own profile, never a request field, and
everything is paid from the caller's platform wallet by the same services the bot and the web panel
use. Every purchase carries the subscription ``generation`` the screen was loaded with.
"""

from __future__ import annotations

import logging

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.miniapp_pages import _no_store
from app.db.models import BotUser, ResellerProfile
from app.services.db_safe import rollback_quiet
from app.services.miniapp_auth import load_mini_user, load_reseller_profile, resolve_mini_persona
from app.services.miniapp_shop import mini_scope
from app.services.redact import user_safe_error
from app.services.reseller_capacity import buy_extra_gb, buy_extra_users, load_reseller_plan
from app.services.reseller_packs import buy_addon_pack
from app.services.reseller_package import renew_package
from app.services.reseller_subscription_overview import build_overview
from app.services.wallet import get_wallet_balance

log = logging.getLogger(__name__)

ID_MAX = 2**31 - 1
AMOUNT_MAX = 1000
NOT_A_RESELLER = "این بخش فقط برای نماینده است"


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")
    generation: StrictInt = Field(ge=0, le=ID_MAX)


class PackBody(_Body):
    pack_id: StrictInt = Field(gt=0, le=ID_MAX)


class ExtraBody(_Body):
    kind: StrictStr = Field(pattern="^(gb|users)$")
    amount: StrictInt = Field(gt=0, le=AMOUNT_MAX)


class RenewBody(_Body):
    pass


async def _owner_context(session: AsyncSession, request: Request) -> tuple[BotUser, ResellerProfile]:
    """The reseller behind this request and their own active profile; anyone else gets a 403."""
    user = await load_mini_user(session, request)
    if mini_scope(request).is_shop or resolve_mini_persona(user) != "reseller":
        raise HTTPException(403, NOT_A_RESELLER)
    profile = await load_reseller_profile(session, user)
    if profile is None or int(profile.user_id) != int(user.id) or not profile.is_active:
        raise HTTPException(403, NOT_A_RESELLER)
    return user, profile


async def _done(session: AsyncSession, user: BotUser, message: str) -> object:
    await session.refresh(user)
    return _no_store({"ok": True, "message": message, "wallet": await get_wallet_balance(session, user, shop_id=None)})


async def _refused(session: AsyncSession, exc: ValueError) -> HTTPException:
    await rollback_quiet(session)
    return HTTPException(400, user_safe_error(exc, fallback="خرید ناموفق بود"))


def register_miniapp_reseller(app: FastAPI, *, get_db) -> None:
    @app.get("/api/mini/reseller/subscription")
    async def mini_reseller_subscription(request: Request, session: AsyncSession = Depends(get_db)):
        user, profile = await _owner_context(session, request)
        return _no_store((await build_overview(session, user=user, profile=profile)).payload())

    @app.post("/api/mini/reseller/pack")
    async def mini_reseller_pack(body: PackBody, request: Request, session: AsyncSession = Depends(get_db)):
        user, profile = await _owner_context(session, request)
        plan = await load_reseller_plan(session, profile)
        try:
            await buy_addon_pack(
                session, user=user, profile=profile, plan=plan, pack_id=body.pack_id, generation=body.generation,
            )
        except ValueError as exc:
            raise await _refused(session, exc) from None
        return await _done(session, user, "بسته اعمال شد")

    @app.post("/api/mini/reseller/extra")
    async def mini_reseller_extra(body: ExtraBody, request: Request, session: AsyncSession = Depends(get_db)):
        user, profile = await _owner_context(session, request)
        plan = await load_reseller_plan(session, profile)
        if plan is None:
            raise HTTPException(400, "هنوز پلن نمایندگی (اشتراک) ندارید")
        try:
            if body.kind == "gb":
                await buy_extra_gb(
                    session, user=user, profile=profile, plan=plan, gb=body.amount, generation=body.generation,
                )
            else:
                await buy_extra_users(
                    session, user=user, profile=profile, plan=plan, count=body.amount, generation=body.generation,
                )
        except ValueError as exc:
            raise await _refused(session, exc) from None
        return await _done(session, user, "ظرفیت اضافه شد")

    @app.post("/api/mini/reseller/renew")
    async def mini_reseller_renew(body: RenewBody, request: Request, session: AsyncSession = Depends(get_db)):
        user, profile = await _owner_context(session, request)
        if not profile.plan_id:
            raise HTTPException(400, "هنوز پلن نمایندگی (اشتراک) ندارید")
        try:
            await renew_package(
                session, owner=user, profile=profile, plan_id=int(profile.plan_id), generation=body.generation,
            )
        except ValueError as exc:
            raise await _refused(session, exc) from None
        return await _done(session, user, "تمدید انجام شد")
