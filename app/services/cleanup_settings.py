"""Finance cleanup tab: save tag deletion rules, and fold legacy soft-cancel into them.

Soft-cancel (``pending_order_cleanup_*``) used to cancel abandoned orders in place.
The cleanup tab is now tag-based permanent deletion only. When a shop still has
soft-cancel switched on, ``migrate_soft_cancel_to_purge`` copies the statuses it
would have cancelled into purge tags, turns automatic purge on, converts the hour
TTL to whole days, and switches soft-cancel off so nothing keeps cancelling in
secret. Staff with ``shop_settings`` or ``orders_purge`` may save the rules.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OrderStatus, ResellerSetting
from app.services.db_safe import rollback_quiet
from app.services.order_purge import (
    MAX_DAYS,
    MIN_DAYS,
    ORDER,
    PurgeRules,
    TAGS,
    parse_days,
    purge_scope,
    rules_from_settings,
    save_rules,
)
from app.services.stale_order_cleanup import policy_from_settings
from app.services.users import get_all_settings, on, set_settings_bulk

logger = logging.getLogger(__name__)

SAVE_PATH = "/finance/cleanup/save"
SOFT_CANCEL_ENABLED = "pending_order_cleanup_enabled"
SOFT_CANCEL_BASE_TAGS = frozenset(
    {
        f"{ORDER}:{OrderStatus.PENDING.value}",
        f"{ORDER}:{OrderStatus.AWAITING_RECEIPT.value}",
        f"{ORDER}:{OrderStatus.REJECTED.value}",
    }
)
SOFT_CANCEL_APPROVAL_TAG = f"{ORDER}:{OrderStatus.AWAITING_APPROVAL.value}"
SHOP_SCAN_LIMIT = 2000


def hours_to_purge_days(hours: int) -> int:
    """Ceil hour TTL to whole purge days inside the allowed range."""
    days = (max(1, int(hours)) + 23) // 24
    return max(MIN_DAYS, min(MAX_DAYS, days))


def soft_cancel_purge_rules(settings: Mapping[str, str], existing: PurgeRules) -> PurgeRules | None:
    """Merged purge rules when legacy soft-cancel is on; ``None`` when there is nothing to fold."""
    if not on(settings.get(SOFT_CANCEL_ENABLED, "0")):
        return None
    policy = policy_from_settings(dict(settings))
    tags = set(SOFT_CANCEL_BASE_TAGS)
    if policy.approval_hours is not None:
        tags.add(SOFT_CANCEL_APPROVAL_TAG)
    tags |= set(existing.tags)
    # Keep an existing day count when purge tags were already chosen; otherwise convert hours.
    days = existing.days if existing.tags else hours_to_purge_days(policy.hours)
    return PurgeRules(tags=frozenset(tags), auto=True, days=days)


async def migrate_soft_cancel_to_purge(session: AsyncSession, shop_id: int | None) -> bool:
    """Fold soft-cancel into tag purge and disable it. Idempotent; ``True`` when something changed."""
    settings = await get_all_settings(session, reseller_id=shop_id)
    migrated = soft_cancel_purge_rules(settings, rules_from_settings(settings))
    if migrated is None:
        return False
    await save_rules(session, shop_id=shop_id, rules=migrated)
    await set_settings_bulk(session, {SOFT_CANCEL_ENABLED: "0"}, reseller_id=shop_id)
    logger.info(
        "folded soft-cancel into purge shop=%s tags=%s days=%s",
        shop_id,
        ",".join(sorted(migrated.tags)),
        migrated.days,
    )
    return True


async def _shops_with_soft_cancel(session: AsyncSession) -> list[int]:
    rows = await session.scalars(
        select(ResellerSetting.reseller_user_id)
        .where(
            ResellerSetting.key == SOFT_CANCEL_ENABLED,
            ResellerSetting.value.in_(("1", "true", "yes", "on")),
        )
        .order_by(ResellerSetting.reseller_user_id)
        .limit(SHOP_SCAN_LIMIT)
    )
    return [int(shop_id) for shop_id in rows.all()]


async def migrate_all_soft_cancel(session: AsyncSession) -> int:
    """Platform + every shop still on soft-cancel; one failure does not stop the rest."""
    done = 0
    for shop_id in [None, *await _shops_with_soft_cancel(session)]:
        try:
            done += int(await migrate_soft_cancel_to_purge(session, shop_id))
        except Exception:
            logger.exception("soft-cancel migration failed shop=%s", shop_id)
            await rollback_quiet(session)
    return done


def purge_rules_from_form(form: Mapping[str, object]) -> PurgeRules:
    """Deletion rules from the form; raises ``ValueError`` for a bad day count."""
    tags = frozenset(token for token in TAGS if str(form.get(f"t_{token}") or "") == "1")
    return PurgeRules(tags=tags, auto=str(form.get("auto") or "") == "1", days=parse_days(form.get("days")))


async def save_cleanup_tab(session: AsyncSession, *, staff: dict, form: Mapping[str, object]) -> None:
    """Save the tag rules for the staff member's shop.

    Raises ``PermissionError`` without cleanup access and ``ValueError`` for an invalid day count.
    Also clears legacy soft-cancel so it cannot keep running after a save.
    """
    shop_id = purge_scope(staff)
    await save_rules(session, shop_id=shop_id, rules=purge_rules_from_form(form))
    await set_settings_bulk(session, {SOFT_CANCEL_ENABLED: "0"}, reseller_id=shop_id)
