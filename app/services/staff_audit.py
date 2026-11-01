"""The shop staff action log: record a change with the change, and list one shop's records.

A record is added to the caller's session and committed together with the change it
describes, so there is never a change without its record or a record without its change.
Every read is limited to one shop in SQL.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ShopStaffAction
from app.services.shop_scope import is_platform_admin, shop_owner_id

DISPLAY_ZONE = ZoneInfo("Asia/Tehran")
PAGE_SIZE = 50
REASON_MAX = 255
LABEL_MAX = 128

# Web sessions of a shop's own owner (the reseller login, or its Level-1 principal identity).
SHOP_OWNER_ROLES = frozenset({"reseller", "principal"})

ACTOR_PLATFORM_OWNER = "platform_owner"
ACTOR_SHOP_OWNER = "shop_owner"
ACTOR_STAFF = "staff"
ACTOR_LABELS = {
    ACTOR_PLATFORM_OWNER: "مدیر پلتفرم",
    ACTOR_SHOP_OWNER: "صاحب فروشگاه",
    ACTOR_STAFF: "کارمند",
}

# Fixed in code: a record's action is always one of these.
ACTION_LABELS = {
    "gift_code_create": "ساخت کد هدیه",
    "gift_code_edit": "ویرایش کد هدیه",
    "gift_code_toggle": "روشن یا خاموش کردن کد هدیه",
}

TARGET_LABELS = {
    "gift_code": "کد هدیه",
}


@dataclass(frozen=True, slots=True)
class StaffActor:
    """The acting staff member and the shop they act for, resolved from the session."""

    shop_id: int | None
    kind: str
    user_id: int | None
    label: str


def _positive(value: Any) -> int | None:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def actor_from_staff(staff: dict, *, shop_id: int | None) -> StaffActor:
    """The actor for ``staff`` acting for ``shop_id``, the shop the caller's own scope rule resolved."""
    if is_platform_admin(staff):
        kind = ACTOR_PLATFORM_OWNER
    elif str(staff.get("role") or "") in SHOP_OWNER_ROLES:
        kind = ACTOR_SHOP_OWNER
    else:
        kind = ACTOR_STAFF
    # Local import: notifications pulls in the bot layer at import time.
    from app.services.notifications import actor_label_from_staff

    label = (actor_label_from_staff(staff) or ACTOR_LABELS[kind]).strip()[:LABEL_MAX]
    return StaffActor(shop_id=shop_id, kind=kind, user_id=_positive(staff.get("bot_user_id")), label=label)


def add_staff_action(
    session: AsyncSession,
    actor: StaffActor,
    *,
    action: str,
    target_type: str,
    target_id: int | None,
    amount: int | None = None,
    reason: str | None = None,
    request_key: str | None = None,
) -> ShopStaffAction:
    """Add one record to the session; the caller's commit writes it with the change."""
    if action not in ACTION_LABELS:
        raise ValueError(f"unknown staff action: {action}")
    if target_type not in TARGET_LABELS:
        raise ValueError(f"unknown staff action target: {target_type}")
    if amount is not None and (isinstance(amount, bool) or not isinstance(amount, int)):
        raise ValueError("amount must be an integer")
    row = ShopStaffAction(
        shop_id=actor.shop_id,
        actor_kind=actor.kind,
        actor_user_id=actor.user_id,
        actor_label=actor.label,
        action=action,
        target_type=target_type,
        target_id=target_id,
        amount=amount,
        reason=((reason or "").strip()[:REASON_MAX] or None),
        request_key=request_key,
    )
    session.add(row)
    return row


def staff_audit_scope(staff: dict) -> int | None:
    """Shop whose log ``staff`` may read (``None`` = platform); raises ``PermissionError``.

    The platform owner reads the platform's log and a shop owner always reads their own
    shop's; nobody else reads any.
    """
    if is_platform_admin(staff):
        return None
    if str(staff.get("role") or "") not in SHOP_OWNER_ROLES:
        raise PermissionError("دسترسی گزارش کارکنان ندارید")
    shop_id = shop_owner_id(staff)
    if shop_id is None:
        raise PermissionError("حساب شما به فروشگاه متصل نیست")
    return shop_id


async def list_staff_actions(
    session: AsyncSession,
    *,
    shop_id: int | None,
    before_id: int | None = None,
    limit: int = PAGE_SIZE,
) -> list[ShopStaffAction]:
    """Newest records of one shop, keyset-paginated by id."""
    shop_clause = ShopStaffAction.shop_id.is_(None) if shop_id is None else ShopStaffAction.shop_id == int(shop_id)
    query = select(ShopStaffAction).where(shop_clause)
    if before_id is not None:
        query = query.where(ShopStaffAction.id < int(before_id))
    query = query.order_by(ShopStaffAction.id.desc()).limit(max(1, min(int(limit), PAGE_SIZE)))
    return list((await session.scalars(query)).all())


@dataclass(frozen=True, slots=True)
class StaffActionView:
    """One record as the log page shows it; free text is escaped by the template."""

    id: int
    when: str
    actor: str
    actor_kind: str
    action: str
    target: str
    amount: int | None
    reason: str


def _target_text(row: ShopStaffAction) -> str:
    label = TARGET_LABELS.get(row.target_type, row.target_type)
    return f"{label} #{row.target_id}" if row.target_id is not None else label


def staff_action_views(rows: list[ShopStaffAction]) -> list[StaffActionView]:
    views = []
    for row in rows:
        created = row.created_at
        if created is not None and created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        views.append(
            StaffActionView(
                id=int(row.id),
                when=created.astimezone(DISPLAY_ZONE).strftime("%Y-%m-%d %H:%M") if created else "",
                actor=row.actor_label,
                actor_kind=ACTOR_LABELS.get(row.actor_kind, row.actor_kind),
                action=ACTION_LABELS.get(row.action, row.action),
                target=_target_text(row),
                amount=row.amount,
                reason=row.reason or "",
            )
        )
    return views
