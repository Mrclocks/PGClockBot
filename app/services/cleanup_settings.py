"""Finance cleanup tab: save tag-based permanent deletion rules, and ack soft-cancel retirement.

Only staff with ``orders_purge`` (or the platform owner) may save deletion rules.
Staff with ``shop_settings`` or ``orders_purge`` may dismiss the soft-cancel retirement
banner. Soft-cancel itself is disabled by the hourly purge job
(``stale_order_cleanup.disable_all_legacy_soft_cancel``).
"""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.authz import authz_from_staff, can_shop
from app.services.order_purge import PURGE_PERMISSION, TAGS, PurgeRules, parse_days, purge_scope, save_rules
from app.services.shop_scope import is_platform_admin, shop_owner_id
from app.services.stale_order_cleanup import (
    KEY_ENABLED,
    KEY_RETIRED_NOTICE,
    clear_soft_cancel_retired_notice,
)
from app.services.users import set_settings_bulk

SAVE_PATH = "/finance/cleanup/save"
ACK_RETIRED_PATH = "/finance/cleanup/ack-retired"


def notice_scope(staff: dict) -> int | None:
    """Shop whose soft-cancel retirement banner ``staff`` may clear (``None`` = platform)."""
    if is_platform_admin(staff):
        return None
    authz = authz_from_staff(staff)
    if not (can_shop(authz, "shop_settings") or can_shop(authz, PURGE_PERMISSION)):
        raise PermissionError("دسترسی تنظیمات مالی ندارید")
    shop_id = shop_owner_id(staff)
    if shop_id is None:
        raise PermissionError("حساب شما به فروشگاه متصل نیست")
    return shop_id


def purge_rules_from_form(form: Mapping[str, object]) -> PurgeRules:
    """Deletion rules from the form; raises ``ValueError`` for a bad day count."""
    tags = frozenset(token for token in TAGS if str(form.get(f"t_{token}") or "") == "1")
    return PurgeRules(tags=tags, auto=str(form.get("auto") or "") == "1", days=parse_days(form.get("days")))


async def save_cleanup_tab(session: AsyncSession, *, staff: dict, form: Mapping[str, object]) -> None:
    """Save the tag rules for the staff member's shop.

    Raises ``PermissionError`` without ``orders_purge`` and ``ValueError`` for an invalid day count.
    """
    shop_id = purge_scope(staff)
    await save_rules(session, shop_id=shop_id, rules=purge_rules_from_form(form))
    await set_settings_bulk(
        session, {KEY_ENABLED: "0", KEY_RETIRED_NOTICE: "0"}, reseller_id=shop_id
    )


async def ack_soft_cancel_retired(session: AsyncSession, *, staff: dict) -> None:
    """Clear the one-shot soft-cancel retirement banner for the staff member's shop."""
    await clear_soft_cancel_retired_notice(session, notice_scope(staff))
