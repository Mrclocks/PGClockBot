"""Finance cleanup tab: one save for tag-based permanent deletion rules.

Only staff with ``orders_purge`` (or the platform owner) may save. Saving also
clears legacy ``pending_order_cleanup_enabled`` so soft-cancel cannot linger after
an intentional purge configuration. Soft-cancel disable for shops that never open
the tab runs in the hourly purge job (see ``stale_order_cleanup.disable_all_legacy_soft_cancel``).
"""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.order_purge import TAGS, PurgeRules, parse_days, purge_scope, save_rules
from app.services.users import set_settings_bulk

SAVE_PATH = "/finance/cleanup/save"
SOFT_CANCEL_ENABLED = "pending_order_cleanup_enabled"


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
    await set_settings_bulk(session, {SOFT_CANCEL_ENABLED: "0"}, reseller_id=shop_id)
