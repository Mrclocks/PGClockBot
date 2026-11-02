"""Which marketing & loyalty sections a signed-in staff member may open.

The panel groups the loyalty club, discounts, targeted campaigns and broadcasts under one
navigation entry. Visibility here mirrors the guard of each route exactly, so
the sidebar and the shared tab strip never offer a link that ends in a 403 and
never reveal a section the route would refuse.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.services.authz import authz_from_staff, can_shop
from app.services.funnel_access import funnel_shop_scope
from app.services.gift_codes import gift_code_scope
from app.services.platform_identity import is_explicit_owner_staff
from app.services.shop_scope import ShopScopeError, resolve_shop_scope_id

CLUB = "club"
CLUB_TRANSACTIONS = "club_transactions"
DISCOUNTS = "discounts"
GIFT_CODES = "gift_codes"
FUNNEL = "funnel"
CAMPAIGNS = "campaigns"
TRIAL_USERS = "trial_users"
BROADCAST = "broadcast"


@dataclass(frozen=True, slots=True)
class MarketingSection:
    key: str
    label: str
    href: str


_CLUB_SECTIONS: tuple[MarketingSection, ...] = (
    MarketingSection(CLUB, "باشگاه مشتریان", "/loyalty?tab=overview"),
    MarketingSection(CLUB_TRANSACTIONS, "تراکنش‌های باشگاه", "/loyalty?tab=transactions"),
)
_DISCOUNT_SECTION = MarketingSection(DISCOUNTS, "تخفیف‌ها", "/catalog-discounts")
_GIFT_CODE_SECTION = MarketingSection(GIFT_CODES, "کد هدیه", "/gift-codes")
_FUNNEL_SECTION = MarketingSection(FUNNEL, "رفتار کاربر", "/funnel")
_CAMPAIGN_SECTION = MarketingSection(CAMPAIGNS, "کمپین هدفمند", "/campaigns")
_TRIAL_USERS_SECTION = MarketingSection(TRIAL_USERS, "کاربران تست", "/trial-users")
_BROADCAST_SECTION = MarketingSection(BROADCAST, "پیام گروهی", "/broadcast")


def _has_shop_scope(staff: Mapping[str, Any]) -> bool:
    """Staff without a resolvable shop (broken reseller, PG staff, bare ``role=admin``) get no tenant data."""
    try:
        resolve_shop_scope_id(dict(staff))
    except ShopScopeError:
        return False
    return True


def _can_open_feature(staff: Mapping[str, Any], permission: str) -> bool:
    """Feature permission plus a shop scope, as the loyalty, discount and campaign pages all require."""
    return can_shop(authz_from_staff(staff), permission) and _has_shop_scope(staff)


def _can_open_gift_codes(staff: Mapping[str, Any]) -> bool:
    """Same rule as the gift-code page and its POST routes (``gift_code_scope``)."""
    try:
        gift_code_scope(staff)
    except PermissionError:
        return False
    return True


def _can_open_funnel(staff: Mapping[str, Any]) -> bool:
    """Same rule as the funnel page (``funnel_shop_scope``)."""
    try:
        funnel_shop_scope(staff)
    except PermissionError:
        return False
    return True


def _can_open_broadcast(staff: Mapping[str, Any]) -> bool:
    """Same rule as ``require_admin``: ``role=admin`` plus an explicit Owner principal."""
    return staff.get("role") == "admin" and is_explicit_owner_staff(dict(staff))


def marketing_sections(staff: Mapping[str, Any] | None) -> tuple[MarketingSection, ...]:
    """Sections the staff member can open, in display order. Empty when unauthenticated."""
    if not staff:
        return ()
    sections: list[MarketingSection] = []
    if _can_open_feature(staff, "loyalty"):
        sections.extend(_CLUB_SECTIONS)
    if _can_open_feature(staff, "catalog_discounts"):
        sections.append(_DISCOUNT_SECTION)
    if _can_open_gift_codes(staff):
        sections.append(_GIFT_CODE_SECTION)
    if _can_open_feature(staff, "campaigns"):
        sections.append(_CAMPAIGN_SECTION)
    if _can_open_feature(staff, "trial_users"):
        sections.append(_TRIAL_USERS_SECTION)
    if _can_open_funnel(staff):
        sections.append(_FUNNEL_SECTION)
    if _can_open_broadcast(staff):
        sections.append(_BROADCAST_SECTION)
    return tuple(sections)
