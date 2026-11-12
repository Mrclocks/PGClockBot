"""Bot authorization for read-only PasarGuard pages (inbounds, admins).

Same chain as the node/host/catalog gates: Telegram identity -> OrgPrincipal -> clamped staff ->
PG page capability -> the Principal's own client. A shop bot and tampered callback data fail closed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser
from app.services import bot_feedback_texts as feedback
from app.services.bot_principal_identity import (
    bot_pg_client_for_resolution,
    callback_carries_principal_tamper,
    resolve_bot_pg_family_identity,
)
from app.services.platform_identity import is_explicit_owner_staff
from app.services.principal_pg_authz import (
    apply_level1_pg_local_safety,
    authorize_pg_page,
    is_level1_principal_staff,
    principal_pg_authz_ready,
)

logger = logging.getLogger(__name__)

DENIED_MESSAGE = feedback.ACTION_NOT_ALLOWED


@dataclass(frozen=True, slots=True)
class BotPgPageGate:
    allowed: bool
    user_message: str = ""
    staff: dict[str, Any] | None = None
    pg_client: Any = None


def _deny(message: str = DENIED_MESSAGE) -> BotPgPageGate:
    return BotPgPageGate(allowed=False, user_message=message)


async def authorize_bot_pg_page(
    session: AsyncSession | None,
    *,
    db_user: BotUser | None,
    page: str,
    owner_only: bool = False,
    callback_data: str | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
) -> BotPgPageGate:
    """Allow a PG page only when the actor's own capabilities include it (and, if asked, the Owner)."""
    resolution = await resolve_bot_pg_family_identity(
        session,
        db_user=db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        identity_tamper=callback_carries_principal_tamper(callback_data),
    )
    if isinstance(resolution, str):
        return _deny(feedback.NOT_ADMIN)
    staff = dict(resolution.staff)
    if is_level1_principal_staff(staff):
        staff = apply_level1_pg_local_safety(staff)
    if not principal_pg_authz_ready(staff):
        return _deny()
    if owner_only and not is_explicit_owner_staff(staff):
        return _deny(feedback.SYSTEM_OWNER_REQUIRED)
    if not authorize_pg_page(staff, page).allowed:
        return _deny()
    try:
        pg_client, _as_owner = await bot_pg_client_for_resolution(session, resolution)
    except Exception:  # noqa: BLE001 - client construction can fail on any panel/config problem; fail closed
        logger.warning("bot pg page %s: client construction failed", page, exc_info=True)
        return _deny()
    return BotPgPageGate(allowed=True, staff=staff, pg_client=pg_client)
