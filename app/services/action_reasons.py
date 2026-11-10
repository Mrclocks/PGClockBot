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
from app.services.staff_audit import REASON_MAX, actor_from_staff, add_staff_action

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


async def log_reasoned_action(
    session: AsyncSession,
    staff: dict,
    *,
    action: str,
    target_type: str,
    target_id: int | None,
    reason: str,
) -> None:
    """Write the staff log record of an operation that already succeeded.

    The operation itself (a panel call or its own commit) cannot be undone by a log failure, so
    a failed write is logged and never turns a successful operation into an error.
    """
    try:
        actor = actor_from_staff(staff, shop_id=resolve_shop_scope_id(staff))
        add_staff_action(session, actor, action=action, target_type=target_type, target_id=target_id, reason=reason)
        await session.commit()
    except Exception:  # noqa: BLE001 - the operation succeeded; the log write is best effort
        logger.warning("staff action log failed action=%s target=%s", action, target_id, exc_info=True)
        await session.rollback()
