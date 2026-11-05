"""The finance cleanup tab's one save: deletion rules by status tag.

Only staff with ``orders_purge`` (or the platform owner) may save. The shop always
comes from the session, never from the form.
"""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.order_purge import TAGS, PurgeRules, parse_days, purge_scope, save_rules

SAVE_PATH = "/finance/cleanup/save"


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
