"""Staff actions: turn a customer or a shop off with all its services, or back on.

Each action authorizes from the signed-in staff, saves the hold with its audit record in one
commit, and asks the scheduler to carry it out on the panel now. The panel work itself is never
done inside the request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AccountHold, BotUser
from app.services.account_hold_access import (
    ACCOUNTS_DISABLE_PERMISSION,
    assert_may_release,
    customer_target,
    open_customer_hold_of,
    open_shop_hold_of,
    shop_target,
)
from app.services.account_hold_guard import OPEN_STATUSES
from app.services.account_holds import (
    KIND_CUSTOMER,
    KIND_SHOP,
    PLATFORM_SCOPE,
    NOT_HELD,
    HoldRequest,
    clean_reason,
    hold_progress,
    open_customer_hold,
    open_shop_hold,
    shop_hold_tree_ids,
    start_release,
    sync_shop_bots,
)
from app.services.authz import authz_from_staff, can_shop
from app.services.job_trigger import run_job_now
from app.services.platform_identity import is_explicit_owner_staff
from app.services.shop_scope import shop_owner_id
from app.services.staff_audit import actor_from_staff, add_staff_action

HOLD_JOB_ID = "account_holds"


@dataclass(frozen=True, slots=True)
class HoldView:
    """What the edit pages show about an open hold."""

    status: str
    reason: str | None
    actor: str | None
    progress: dict[str, int]
    releasable: bool


def _audit(session: AsyncSession, staff: dict[str, Any], *, shop_id: int, action: str, target_type: str,
           target_id: int, reason: str | None) -> None:
    actor = actor_from_staff(staff, shop_id=shop_id or None)
    add_staff_action(session, actor, action=action, target_type=target_type, target_id=target_id, reason=reason)


def _request(staff: dict[str, Any], reason_raw: object, placed_by_shop_id: int) -> HoldRequest:
    actor = actor_from_staff(staff, shop_id=placed_by_shop_id or None)
    return HoldRequest(actor=actor.label, reason=clean_reason(reason_raw), placed_by_shop_id=placed_by_shop_id)


async def disable_customer(session: AsyncSession, staff: dict[str, Any], customer_id: int, reason_raw: object) -> None:
    target = await customer_target(session, staff, customer_id)
    request = _request(staff, reason_raw, target.placed_by_shop_id)
    await open_customer_hold(session, customer=target.customer, scope_shop_id=target.scope_shop_id,
                             request=request)
    _audit(session, staff, shop_id=target.scope_shop_id, action="account_disable", target_type="customer",
           target_id=int(target.customer.id), reason=request.reason)
    await session.commit()
    run_job_now(HOLD_JOB_ID)


async def _release(session: AsyncSession, staff: dict[str, Any], hold: AccountHold | None, *, placed_by: int,
                   scope_shop_id: int, target_type: str, target_id: int) -> None:
    if hold is None:
        raise ValueError(NOT_HELD)
    assert_may_release(hold, staff, placed_by)
    _audit(session, staff, shop_id=scope_shop_id, action="account_enable", target_type=target_type,
           target_id=target_id, reason=None)
    await session.flush()
    await start_release(session, hold)
    run_job_now(HOLD_JOB_ID)


async def enable_customer(session: AsyncSession, staff: dict[str, Any], customer_id: int) -> None:
    target = await customer_target(session, staff, customer_id)
    hold = await open_customer_hold_of(session, target)
    await _release(session, staff, hold, placed_by=target.placed_by_shop_id, scope_shop_id=target.scope_shop_id,
                   target_type="customer", target_id=int(target.customer.id))


async def disable_shop(session: AsyncSession, staff: dict[str, Any], shop_user_id: int, reason_raw: object) -> None:
    target = await shop_target(session, staff, shop_user_id)
    request = _request(staff, reason_raw, target.placed_by_shop_id)
    hold = await open_shop_hold(session, profile=target.profile, request=request)
    _audit(session, staff, shop_id=target.placed_by_shop_id, action="account_disable", target_type="shop",
           target_id=int(target.profile.user_id), reason=request.reason)
    await session.commit()
    run_job_now(HOLD_JOB_ID)
    await sync_shop_bots(session, await shop_hold_tree_ids(session, int(hold.id)))


async def enable_shop(session: AsyncSession, staff: dict[str, Any], shop_user_id: int) -> None:
    target = await shop_target(session, staff, shop_user_id)
    hold = await open_shop_hold_of(session, target)
    await _release(session, staff, hold, placed_by=target.placed_by_shop_id, scope_shop_id=target.placed_by_shop_id,
                   target_type="shop", target_id=int(target.profile.user_id))


@dataclass(frozen=True, slots=True)
class HoldPanel:
    """The staff may act on this account; ``hold`` is the open hold, if any."""

    hold: HoldView | None


async def _panel(session: AsyncSession, staff: dict[str, Any], hold: AccountHold | None, placed_by: int) -> HoldPanel:
    if hold is None:
        return HoldPanel(None)
    try:
        assert_may_release(hold, staff, placed_by)
        releasable = True
    except PermissionError:
        releasable = False
    view = HoldView(hold.status, hold.reason, hold.actor, await hold_progress(session, int(hold.id)), releasable)
    return HoldPanel(view)


async def customer_hold_panel(session: AsyncSession, staff: dict[str, Any], customer_id: int) -> HoldPanel | None:
    """What the customer edit modal shows; None when this staff may not disable the customer."""
    try:
        target = await customer_target(session, staff, customer_id)
    except (PermissionError, ValueError):
        return None
    return await _panel(session, staff, await open_customer_hold_of(session, target), target.placed_by_shop_id)


async def shop_hold_panel(session: AsyncSession, staff: dict[str, Any], shop_user_id: int) -> HoldPanel | None:
    """What the reseller edit modal shows; None when this staff may not disable the shop."""
    try:
        target = await shop_target(session, staff, shop_user_id)
    except (PermissionError, ValueError):
        return None
    return await _panel(session, staff, await open_shop_hold_of(session, target), target.placed_by_shop_id)


async def customer_hold_form_context(session: AsyncSession, staff: dict[str, Any], customer: BotUser) -> dict[str, Any]:
    """Template values for the customer edit modal: the section only for staff who may use it."""
    return {
        "account_hold_panel": await customer_hold_panel(session, staff, int(customer.id)),
        "account_hold_platform": is_explicit_owner_staff(staff),
    }


@dataclass(frozen=True, slots=True)
class ShopHoldRow:
    status: str
    releasable: bool


def may_hold_shops(staff: dict[str, Any]) -> bool:
    """Whether the reseller list shows the disable action; the action itself re-checks each shop."""
    return is_explicit_owner_staff(staff) or can_shop(authz_from_staff(staff), ACCOUNTS_DISABLE_PERMISSION)


async def _hold_rows(session: AsyncSession, staff: dict[str, Any], *, kind: str, target_ids: list[int],
                     scope_shop_id: int | None) -> dict[int, ShopHoldRow]:
    """Open holds of the listed targets, in one query; ``scope_shop_id`` None matches every scope."""
    if not target_ids:
        return {}
    query = select(AccountHold.target_user_id, AccountHold.status, AccountHold.placed_by_shop_id).where(
        AccountHold.kind == kind,
        AccountHold.target_user_id.in_([int(i) for i in target_ids]),
        AccountHold.status.in_(OPEN_STATUSES),
    )
    if scope_shop_id is not None:
        query = query.where(AccountHold.scope_shop_id == int(scope_shop_id))
    owner = is_explicit_owner_staff(staff)
    mine = int(shop_owner_id(staff) or 0)
    return {
        int(uid): ShopHoldRow(str(status), owner or (mine > 0 and int(placed_by) == mine))
        for uid, status, placed_by in (await session.execute(query)).all()
    }


async def shop_hold_rows(session: AsyncSession, staff: dict[str, Any], shop_ids: list[int]) -> dict[int, ShopHoldRow]:
    """Open shop holds of the listed shops, for the reseller list rows and their cards."""
    return await _hold_rows(session, staff, kind=KIND_SHOP, target_ids=shop_ids, scope_shop_id=None)


def customer_hold_scope(staff: dict[str, Any]) -> int | None:
    """The scope this staff disables customers in (0: account-wide), or None when it may not."""
    if is_explicit_owner_staff(staff):
        return PLATFORM_SCOPE
    if not can_shop(authz_from_staff(staff), ACCOUNTS_DISABLE_PERMISSION):
        return None
    shop_id = shop_owner_id(staff)
    return int(shop_id) if shop_id else None


async def customer_hold_rows(session: AsyncSession, staff: dict[str, Any],
                             user_ids: list[int]) -> dict[int, ShopHoldRow]:
    """Open customer holds in this staff's scope, for the users list rows and their cards."""
    scope = customer_hold_scope(staff)
    if scope is None:
        return {}
    return await _hold_rows(session, staff, kind=KIND_CUSTOMER, target_ids=user_ids, scope_shop_id=scope)
