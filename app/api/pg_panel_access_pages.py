"""Owner controls for a shop's «دسترسی به پاسارگارد» switch on the reseller edit page."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote

from fastapi import Depends, FastAPI
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ResellerProfile
from app.services.pg_panel_access import (
    ROTATION_FAILED_MESSAGE,
    RotationOutcome,
    finish_pg_password_rotation,
    may_share_pg_credentials,
    open_pg_panel_access,
    pg_panel_allowed,
    set_pg_panel_access,
)

ROTATION_RETRY_PATH = "/resellers/{user_id}/pg-password-rotation"
# Hidden marker: an unchecked switch is not posted, so its absence alone must not close access.
ACCESS_SHOWN_FIELD = "pg_access_shown"
ROTATION_DONE_MESSAGE = "رمز ادمین پاسارگارد عوض شد"


def access_target_from_form(form: Mapping[str, Any], profile: ResellerProfile) -> bool:
    """The access state the owner chose; the current one when the switch was not on the form."""
    if not form.get(ACCESS_SHOWN_FIELD):
        return pg_panel_allowed(profile)
    return bool(form.get("allow_pg_panel_access"))


def apply_access_before_save(
    profile: ResellerProfile, *, target: bool, share_panel_url: bool
) -> None:
    """Open access inside the edit transaction, so a new password in the same save syncs."""
    if target and not pg_panel_allowed(profile):
        open_pg_panel_access(profile)
    profile.share_pg_panel_url = may_share_pg_credentials(
        allow_access=target, share_panel_url=share_panel_url
    )


async def apply_access_after_save(
    session: AsyncSession, profile: ResellerProfile, *, target: bool
) -> str | None:
    """Close access after the edit is committed; returns a warning for the owner, if any."""
    if target or not pg_panel_allowed(profile):
        return None
    change = await set_pg_panel_access(session, profile, False)
    if change.rotation == RotationOutcome.PENDING:
        return ROTATION_FAILED_MESSAGE
    return None


def _edit_redirect(user_id: int, *, ok: str = "", err: str = "") -> RedirectResponse:
    query = f"edit={int(user_id)}"
    if err:
        query += f"&err={quote(err)}"
    elif ok:
        query += f"&ok={quote(ok)}"
    return RedirectResponse(f"/resellers?{query}", status_code=303)


def register_pg_panel_access_pages(
    app: FastAPI, *, require_admin: Callable, get_db: Callable
) -> None:
    @app.post(ROTATION_RETRY_PATH)
    async def retry_pg_password_rotation(
        user_id: int,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ) -> RedirectResponse:
        profile = (
            await session.execute(
                select(ResellerProfile).where(ResellerProfile.user_id == int(user_id))
            )
        ).scalar_one_or_none()
        if profile is None:
            return RedirectResponse("/resellers", status_code=303)
        outcome = await finish_pg_password_rotation(session, profile)
        if outcome == RotationOutcome.PENDING:
            return _edit_redirect(user_id, err=ROTATION_FAILED_MESSAGE)
        return _edit_redirect(user_id, ok=ROTATION_DONE_MESSAGE)
