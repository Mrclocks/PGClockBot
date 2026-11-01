"""Who may write a customer's private staff note and risk tag."""

from __future__ import annotations

from typing import Any

from app.services.authz import authz_from_staff, can_shop
from app.services.shop_scope import bot_user_in_scope, is_platform_admin

CUSTOMER_NOTES_PERMISSION = "customer_notes"


def has_customer_notes_permission(staff: dict | None) -> bool:
    """The platform owner always; shop staff only with the ``customer_notes`` key."""
    if not staff:
        return False
    return is_platform_admin(staff) or can_shop(authz_from_staff(staff), CUSTOMER_NOTES_PERMISSION)


def may_edit_private_marks(staff: dict | None, user: Any) -> bool:
    """The note and tag live on the person, so only the shop that attributed them may write them."""
    return has_customer_notes_permission(staff) and bot_user_in_scope(staff or {}, user)
