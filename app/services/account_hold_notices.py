"""Tell the customer or the shop on Telegram that their account was turned off or back on.

A notice is sent once, after the commit that changed the hold, from the bot of the scope that
acted: the platform bot for the owner, the shop's own bot for a shop. A failed send is logged and
never undoes or repeats the hold.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AccountHold, BotUser
from app.services.account_holds import KIND_CUSTOMER

logger = logging.getLogger(__name__)

EVENTS = {
    (KIND_CUSTOMER, True): "account_hold",
    (KIND_CUSTOMER, False): "account_release",
    ("shop", True): "shop_hold",
    ("shop", False): "shop_release",
}


def notice_shop_id(hold: AccountHold) -> int | None:
    """The shop whose bot speaks: the customer's shop, or the shop that placed a shop hold."""
    shop = int(hold.scope_shop_id if hold.kind == KIND_CUSTOMER else hold.placed_by_shop_id)
    return shop or None


async def notify_hold(session: AsyncSession, hold_id: int, *, disabled: bool) -> bool:
    """Send the notice for this hold to its target; True when it reached Telegram."""
    # Deferred: notifications pulls in the bot layer at import time.
    from app.services.notifications import notify_account_edit

    hold = await session.get(AccountHold, int(hold_id))
    user = await session.get(BotUser, int(hold.target_user_id)) if hold is not None else None
    if hold is None or user is None:
        return False
    try:
        result = await notify_account_edit(
            session,
            user=user,
            event=EVENTS[(str(hold.kind), disabled)],
            reason=hold.reason if disabled else None,
            actor=hold.actor,
            shop_id=notice_shop_id(hold),
        )
    except Exception:  # noqa: BLE001 - a notice must never undo or repeat the hold
        logger.warning("account hold %s: notice failed", hold.id, exc_info=True)
        return False
    return bool(result.get("subject"))
