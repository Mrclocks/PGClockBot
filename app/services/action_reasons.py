"""A reason is required for every operation staff perform on a customer or a panel user.

The reason is validated before any panel call, written to the shop staff log after the
operation succeeded, and (for service operations) shown to the customer. A missing or
too-short reason stops the operation with a Persian message.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.delete_reason import delete_reason_too_short, extract_delete_reason
from app.services.shop_scope import resolve_shop_scope_id
from app.db.models import BotUser
from app.services.staff_audit import (
    ACTOR_PLATFORM_OWNER,
    ACTOR_SHOP_OWNER,
    ACTOR_STAFF,
    LABEL_MAX,
    REASON_MAX,
    StaffActor,
    actor_from_staff,
    add_staff_action,
)

logger = logging.getLogger(__name__)

REASON_MIN = 3
REASON_REQUIRED_TEXT = f"علت این عملیات الزامی است (حداقل {REASON_MIN} نویسه)."
REASON_FORM_KEYS = ("reason", "confirm_reason")


class ReasonRequired(ValueError):
    """The reason is missing or too short; the message is safe to show staff."""


def clean_reason(raw: object) -> str:
    """The trimmed reason, bounded to the log column; raises ``ReasonRequired``."""
    text = " ".join(str(raw or "").split())
    if delete_reason_too_short(text, minimum=REASON_MIN):
        raise ReasonRequired(REASON_REQUIRED_TEXT)
    return text[:REASON_MAX]


def reason_from_form(form: Mapping[str, Any]) -> str:
    """The reason a panel form carries, whichever field name the confirm dialog used."""
    return clean_reason(extract_delete_reason(form, *REASON_FORM_KEYS))


async def log_reasoned_action_as(
    session: AsyncSession,
    actor: StaffActor,
    *,
    action: str,
    target_type: str,
    target_id: int | None,
    reason: str,
    amount: int | None = None,
) -> None:
    """Write the staff log record of an operation that already succeeded.

    The operation itself (a panel call or its own commit) cannot be undone by a log failure, so
    a failed write is logged and never turns a successful operation into an error.
    """
    try:
        add_staff_action(
            session, actor, action=action, target_type=target_type, target_id=target_id, reason=reason, amount=amount
        )
        await session.commit()
    except Exception:  # noqa: BLE001 - the operation succeeded; the log write is best effort
        logger.warning("staff action log failed action=%s target=%s", action, target_id, exc_info=True)
        await session.rollback()


async def log_reasoned_action(
    session: AsyncSession,
    staff: dict,
    *,
    action: str,
    target_type: str,
    target_id: int | None,
    reason: str,
    amount: int | None = None,
) -> None:
    """``log_reasoned_action_as`` for a web staff session; the shop comes from the session."""
    try:
        actor = actor_from_staff(staff, shop_id=resolve_shop_scope_id(staff))
    except Exception:  # noqa: BLE001 - the operation succeeded; an unresolvable actor only skips the log
        logger.warning("staff action actor unresolved action=%s target=%s", action, target_id, exc_info=True)
        return
    await log_reasoned_action_as(
        session, actor, action=action, target_type=target_type, target_id=target_id, reason=reason, amount=amount
    )


def bot_actor(db_user: BotUser, *, shop_id: int | None) -> StaffActor:
    """The acting person of a Telegram admin flow: the platform owner, or staff of ``shop_id``."""
    handle = f"@{db_user.username}" if db_user.username else str(db_user.telegram_id)
    if shop_id is None:
        kind = ACTOR_PLATFORM_OWNER
    elif int(db_user.id) == int(shop_id):
        kind = ACTOR_SHOP_OWNER
    else:
        kind = ACTOR_STAFF
    return StaffActor(shop_id=shop_id, kind=kind, user_id=int(db_user.id), label=f"ربات: {handle}"[:LABEL_MAX])
