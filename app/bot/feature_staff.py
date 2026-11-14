"""Resolve the acting bot user into the staff dict the services expect, for one shop permission."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.auth import require_bot_owner, resolve_bot_principal_bridge
from app.db.models import BotUser
from app.services.reseller_access import load_reseller_actor
from app.services.resellers import has_bot_perm
from app.services.shop_scope import is_platform_admin


async def resolve_feature_staff(
    session: AsyncSession,
    db_user: BotUser,
    permission: str,
    *,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> dict | None:
    """Staff scoped to the actor's own shop (holding ``permission``) or the explicit platform Owner.

    Fails closed: a shop bot never yields platform staff, and a missing permission yields ``None``.
    """
    owner_id, profile = await load_reseller_actor(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if owner_id and profile:
        if not has_bot_perm(profile, permission):
            return None
        bridge = await resolve_bot_principal_bridge(
            session, db_user, is_reseller_bot=True, reseller_owner_id=reseller_owner_id
        )
        return bridge.staff if bridge and bridge.staff else {"role": "reseller", "bot_user_id": int(owner_id)}
    if is_reseller_bot or not await require_bot_owner(session, db_user, is_reseller_bot=False, notify=False):
        return None
    bridge = await resolve_bot_principal_bridge(session, db_user, is_reseller_bot=False)
    return bridge.staff if bridge and bridge.staff and is_platform_admin(bridge.staff) else None
