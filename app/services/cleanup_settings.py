"""The finance cleanup tab's one save: stale-order cancel settings and, when allowed, deletion rules.

The cancel settings follow the finance settings permission (the platform owner, or shop staff
with ``shop_settings``); the deletion rules additionally need ``orders_purge``. Both go to the
shop resolved from the session, never from the form.
"""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.authz import authz_from_staff, can_shop
from app.services.order_purge import TAGS, PurgeRules, parse_days, purge_scope, save_rules
from app.services.shop_scope import is_platform_admin, shop_owner_id
from app.services.stale_order_cleanup import DEFAULT_APPROVAL_HOURS, DEFAULT_HOURS, clamp_hours
from app.services.users import set_settings_bulk

SAVE_PATH = "/finance/cleanup/save"
TOGGLE_KEYS = ("pending_order_cleanup_enabled", "pending_order_cleanup_awaiting_approval")
HOUR_KEYS = {"pending_order_ttl_hours": DEFAULT_HOURS, "pending_order_approval_ttl_hours": DEFAULT_APPROVAL_HOURS}
PURGE_SECTION_FIELD = "purge_section"


def cleanup_settings_scope(staff: dict) -> int | None:
    """Shop whose cleanup settings ``staff`` may save (``None`` = platform); raises ``PermissionError``."""
    if is_platform_admin(staff):
        return None
    if not can_shop(authz_from_staff(staff), "shop_settings"):
        raise PermissionError("دسترسی تنظیمات مالی ندارید")
    shop_id = shop_owner_id(staff)
    if shop_id is None:
        raise PermissionError("حساب شما به فروشگاه متصل نیست")
    return shop_id


def cancel_settings_payload(form: Mapping[str, object]) -> dict[str, str]:
    """The four stale-order settings from the form; unticked switches are off, hours are clamped."""
    payload = {key: "1" if str(form.get(f"s_{key}") or "") in {"1", "on", "true"} else "0" for key in TOGGLE_KEYS}
    for key, default in HOUR_KEYS.items():
        payload[key] = str(clamp_hours(form.get(f"s_{key}"), default))
    return payload


def purge_rules_from_form(form: Mapping[str, object]) -> PurgeRules:
    """Deletion rules from the form; raises ``ValueError`` for a bad day count."""
    tags = frozenset(token for token in TAGS if str(form.get(f"t_{token}") or "") == "1")
    return PurgeRules(tags=tags, auto=str(form.get("auto") or "") == "1", days=parse_days(form.get("days")))


def _may_purge(staff: dict, shop_id: int | None) -> bool:
    try:
        return purge_scope(staff) == shop_id
    except PermissionError:
        return False


async def save_cleanup_tab(session: AsyncSession, *, staff: dict, form: Mapping[str, object]) -> None:
    """Save the whole tab in one go; nothing is saved when the deletion rules are invalid.

    Raises ``PermissionError`` without the finance settings permission and ``ValueError`` for
    invalid deletion rules. Deletion fields from staff without ``orders_purge`` are ignored.
    """
    shop_id = cleanup_settings_scope(staff)
    rules = None
    if str(form.get(PURGE_SECTION_FIELD) or "") == "1" and _may_purge(staff, shop_id):
        rules = purge_rules_from_form(form)
    await set_settings_bulk(session, cancel_settings_payload(form), reseller_id=shop_id)
    if rules is not None:
        await save_rules(session, shop_id=shop_id, rules=rules)
