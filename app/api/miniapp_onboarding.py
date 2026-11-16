"""Mini App onboarding routes: referral code, required channels and entry terms.

Thin routes over ``app.services.miniapp_onboarding``. The person comes from the signed
initData and the shop from the verified Mini App scope; these routes skip only the gates
they exist to clear, never the signature, the block or the shop checks.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.miniapp_pages import _no_store
from app.db.models import BotUser
from app.services.miniapp_auth import (
    REFERRAL_REQUIRED,
    load_mini_principal,
    mini_reject,
    verified_telegram_id,
)
from app.services.miniapp_onboarding import (
    REFERRAL_INVALID_TEXT,
    OnboardingThrottled,
    accept_entry_terms,
    onboarding_state,
    referral_step,
    throttle_join_check,
    throttle_referral_try,
)
from app.services.miniapp_shop import mini_scope
from app.services.referral_registration import ReferralRequired, normalize_referral_code

REFERRAL_CODE_MAX_CHARS = 64


async def _json_object(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "درخواست نامعتبر است") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "درخواست نامعتبر است")
    return body


def _referral_code(body: dict) -> str:
    raw = body.get("code")
    if not isinstance(raw, str) or len(raw) > REFERRAL_CODE_MAX_CHARS:
        raise HTTPException(400, REFERRAL_INVALID_TEXT)
    code = normalize_referral_code(raw)
    if not code:
        raise HTTPException(400, REFERRAL_INVALID_TEXT)
    return code


async def _registered_user(session: AsyncSession, request: Request) -> BotUser:
    try:
        return await load_mini_principal(session, request)
    except ReferralRequired:
        raise mini_reject(403, REFERRAL_REQUIRED) from None


def register_miniapp_onboarding(app: FastAPI, *, get_db) -> None:
    @app.get("/api/mini/onboarding")
    async def mini_onboarding(request: Request, session: AsyncSession = Depends(get_db)):
        try:
            user = await load_mini_principal(session, request)
        except ReferralRequired as exc:
            return _no_store(referral_step(exc))
        return _no_store(await onboarding_state(session, user, scope=mini_scope(request)))

    @app.post("/api/mini/onboarding/referral")
    async def mini_onboarding_referral(request: Request, session: AsyncSession = Depends(get_db)):
        """Register with a typed referral code; someone already registered just gets their next step."""
        code = _referral_code(await _json_object(request))
        try:
            throttle_referral_try(await verified_telegram_id(session, request))
        except OnboardingThrottled as exc:
            raise HTTPException(429, str(exc)) from None
        try:
            user = await load_mini_principal(session, request, referral_code=code)
        except ReferralRequired:
            raise HTTPException(400, REFERRAL_INVALID_TEXT) from None
        return _no_store(await onboarding_state(session, user, scope=mini_scope(request)))

    @app.post("/api/mini/onboarding/join-check")
    async def mini_onboarding_join_check(request: Request, session: AsyncSession = Depends(get_db)):
        """«عضو شدم»: ask Telegram again instead of trusting a recent answer."""
        user = await _registered_user(session, request)
        try:
            throttle_join_check(int(user.telegram_id))
        except OnboardingThrottled as exc:
            raise HTTPException(429, str(exc)) from None
        return _no_store(await onboarding_state(session, user, scope=mini_scope(request), fresh=True))

    @app.post("/api/mini/onboarding/terms")
    async def mini_onboarding_terms(request: Request, session: AsyncSession = Depends(get_db)):
        user = await _registered_user(session, request)
        scope = mini_scope(request)
        await accept_entry_terms(session, user, scope=scope)
        return _no_store(await onboarding_state(session, user, scope=scope))
