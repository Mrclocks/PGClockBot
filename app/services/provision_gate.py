"""Shared Provision Gate: PG quota + Billing empty-balance policy + volume holds.

Every operation that creates a new financial commitment for a reseller shop
must pass through here (create user, renew, volume/plan change, similar).
Volume rules live in ``volume_allocation``; the ``hold_*`` helpers here are the only
entry points callers use, right before their panel call.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.billing import BillingError, assert_billing_allows_provision
from app.services.db_safe import rollback_quiet
from app.services.pg_quota import (
    PgQuotaError,
    assert_can_create_user,
    assert_can_modify_user,
    assert_can_mutate_owned_users,
    assert_reseller_can_deliver,
    assert_reseller_can_renew,
)
from app.services.shop_scope import shop_owner_id
from app.services.volume_allocation import (
    HOLD_TTL,
    Audience,
    HeldVolume,
    VolumeAllocationError,
    hold_volume,
    is_hard_panel_rejection,
    record_service_limit,
    release_volume_hold,
    shop_is_gated,
)

logger = logging.getLogger(__name__)

# A create that leaves no local service row is counted by its hold until the panel sync sees it.
PANEL_ONLY_HOLD_TTL = timedelta(hours=1)


class ProvisionError(Exception):
    """Unified provision failure (quota or billing)."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def _wrap(exc: Exception) -> ProvisionError:
    if isinstance(exc, VolumeAllocationError):
        return ProvisionError(exc.user_message())
    if isinstance(exc, (PgQuotaError, BillingError, ProvisionError)):
        return ProvisionError(getattr(exc, "message", str(exc)))
    return ProvisionError(str(exc))


def reseller_id_from_staff(staff: dict | None) -> int | None:
    """Billing applies to reseller shop owners (not platform admin / bare pg_staff)."""
    return shop_owner_id(staff)


async def assert_provision_create(
    session: AsyncSession,
    *,
    staff: dict | None = None,
    reseller_user_id: int | None = None,
    pg_admin_username: str | None = None,
    pg_role_id: int | None = None,
    data_limit: int | None = None,
    expire_ts: int | None = None,
    hwid_limit: int | None = None,
    from_template: bool = False,
    quantity: int = 1,
) -> None:
    """Gate for creating users (shop delivery or PG panel create)."""
    rid = reseller_user_id
    if rid is None and staff is not None:
        rid = reseller_id_from_staff(staff)

    try:
        await assert_billing_allows_provision(session, reseller_user_id=rid)
    except BillingError as e:
        raise _wrap(e) from e

    try:
        if staff is not None and pg_admin_username is None:
            await assert_can_create_user(
                staff,
                data_limit=data_limit,
                expire_ts=expire_ts,
                hwid_limit=hwid_limit,
                from_template=from_template,
                quantity=quantity,
                session=session,
            )
        elif pg_admin_username is not None or rid:
            await assert_reseller_can_deliver(
                pg_admin_username=pg_admin_username,
                pg_role_id=pg_role_id,
                data_limit=data_limit,
                expire_ts=expire_ts,
                hwid_limit=hwid_limit,
                from_template=from_template,
                quantity=quantity,
                session=session,
                reseller_user_id=rid,
            )
        elif staff is not None:
            await assert_can_create_user(
                staff,
                data_limit=data_limit,
                expire_ts=expire_ts,
                hwid_limit=hwid_limit,
                from_template=from_template,
                quantity=quantity,
                session=session,
            )
    except PgQuotaError as e:
        raise _wrap(e) from e


async def assert_provision_renew(
    session: AsyncSession,
    *,
    staff: dict | None = None,
    reseller_user_id: int | None = None,
    pg_admin_username: str | None = None,
    pg_role_id: int | None = None,
    data_limit: int | None = None,
    expire_ts: int | None = None,
    from_template: bool = False,
) -> None:
    """Gate for renewals / template re-apply."""
    rid = reseller_user_id
    if rid is None and staff is not None:
        rid = reseller_id_from_staff(staff)

    try:
        await assert_billing_allows_provision(session, reseller_user_id=rid)
    except BillingError as e:
        raise _wrap(e) from e

    try:
        if staff is not None and pg_admin_username is None:
            if from_template:
                await assert_can_mutate_owned_users(staff, session=session)
            else:
                await assert_can_modify_user(
                    staff,
                    data_limit=data_limit,
                    expire_ts=expire_ts,
                    data_limit_changed=data_limit is not None,
                    expire_changed=expire_ts is not None,
                    session=session,
                )
        else:
            await assert_reseller_can_renew(
                pg_admin_username=pg_admin_username,
                pg_role_id=pg_role_id,
                data_limit=data_limit,
                expire_ts=expire_ts,
                from_template=from_template,
                session=session,
                reseller_user_id=rid,
            )
    except PgQuotaError as e:
        raise _wrap(e) from e


async def assert_provision_modify(
    session: AsyncSession,
    staff: dict,
    *,
    data_limit: int | None = None,
    expire_ts: int | None = None,
    hwid_limit: int | None = None,
    data_limit_changed: bool = False,
    expire_changed: bool = False,
    hwid_changed: bool = False,
) -> None:
    """Gate for PG user edit (volume / expire / HWID / plan-like changes)."""
    rid = reseller_id_from_staff(staff)
    # Only block when the mutation increases commitment (volume/time/device change)
    if data_limit_changed or expire_changed or hwid_changed:
        try:
            await assert_billing_allows_provision(session, reseller_user_id=rid)
        except BillingError as e:
            raise _wrap(e) from e
    try:
        await assert_can_modify_user(
            staff,
            data_limit=data_limit,
            expire_ts=expire_ts,
            hwid_limit=hwid_limit,
            data_limit_changed=data_limit_changed,
            expire_changed=expire_changed,
            hwid_changed=hwid_changed,
            session=session,
        )
    except PgQuotaError as e:
        raise _wrap(e) from e


async def assert_provision_mutate(session: AsyncSession, staff: dict) -> None:
    """Gate for generic owned-user mutations that still commit resources (reset, etc.)."""
    rid = reseller_id_from_staff(staff)
    try:
        await assert_billing_allows_provision(session, reseller_user_id=rid)
    except BillingError as e:
        raise _wrap(e) from e
    try:
        await assert_can_mutate_owned_users(staff, session=session)
    except PgQuotaError as e:
        raise _wrap(e) from e


def new_hold_key(prefix: str) -> str:
    """A one-off hold key for a create or change that has no order id to key it by."""
    return f"{prefix}:{uuid4().hex}"


async def hold_create_volume(
    session: AsyncSession,
    *,
    shop_id: int | None,
    data_limit: int | None,
    quantity: int = 1,
    key: str,
    audience: Audience = "admin",
    ttl: timedelta = HOLD_TTL,
) -> HeldVolume | None:
    """Hold the volume of ``quantity`` new users (``data_limit`` each, 0 unlimited) and commit."""
    per_user = None if data_limit is None or isinstance(data_limit, bool) else int(data_limit)
    count = max(1, int(quantity or 1))
    delta = None if per_user is None else (per_user * count if per_user > 0 else 0)
    try:
        return await hold_volume(session, shop_id=shop_id, delta_bytes=delta, key=key, audience=audience, ttl=ttl)
    except VolumeAllocationError as e:
        raise _wrap(e) from e


async def hold_limit_change(
    session: AsyncSession,
    *,
    shop_id: int | None,
    current_limit: int | None,
    new_limit: int | None,
    key: str,
    audience: Audience = "admin",
    ttl: timedelta = HOLD_TTL,
) -> HeldVolume | None:
    """Hold the increase of one user's limit (decreases and unchanged limits hold nothing).

    ``new_limit`` None is unknown and 0 a switch to unlimited; both are refused while gated.
    An unlimited current limit counts as 0, so a switch to a finite limit is held in full.
    """
    if new_limit is None or isinstance(new_limit, bool):
        delta = None
    elif int(new_limit) <= 0:
        delta = 0
    else:
        delta = int(new_limit) - max(int(current_limit or 0), 0)
        if delta <= 0:
            return None
    try:
        return await hold_volume(
            session, shop_id=shop_id, delta_bytes=delta, key=key, audience=audience,
            allow_unlimited=False, ttl=ttl,
        )
    except VolumeAllocationError as e:
        raise _wrap(e) from e


async def template_limit_if_gated(session: AsyncSession, shop_id: int, pg: Any, template_id: int) -> int | None:
    """A user template's data limit; read from the panel only when the shop's volume is gated.

    None (unknown) is refused by the gate, so a failed read never sells an unknown volume.
    """
    if not await shop_is_gated(session, int(shop_id)):
        return 0
    try:
        template = await pg.get_user_template(int(template_id))
    except Exception:
        logger.warning("template limit read failed template=%s", template_id, exc_info=True)
        return None
    raw = template.get("data_limit") if isinstance(template, dict) else None
    return int(raw) if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else None


async def hold_plan_create(
    session: AsyncSession,
    *,
    shop_id: int | None,
    pg: Any,
    template_id: int | None,
    data_limit: int | None,
    quantity: int = 1,
    key: str,
    audience: Audience = "admin",
    ttl: timedelta = HOLD_TTL,
) -> HeldVolume | None:
    """Hold the volume of new users made from a plan or template (``data_limit`` None is unlimited)."""
    if not shop_id:
        return None
    if template_id:
        per_user = await template_limit_if_gated(session, int(shop_id), pg, int(template_id))
    else:
        per_user = int(data_limit or 0)
    return await hold_create_volume(
        session, shop_id=int(shop_id), data_limit=per_user, quantity=quantity, key=key, audience=audience, ttl=ttl,
    )


async def settle_failed_hold(
    session: AsyncSession, hold: HeldVolume | None, exc: BaseException, *, panel_touched: bool
) -> None:
    """After a failure: free the hold when nothing reached the panel or the panel refused.

    Any other failure may have granted the volume, so the hold is left to expire.
    """
    if hold is None or (panel_touched and not is_hard_panel_rejection(exc)):
        return
    try:
        await release_volume_hold(session, hold)
        await session.commit()
    except SQLAlchemyError:
        # The hold expires on its own; counting it a little longer only fails closed.
        logger.warning("volume hold release failed key=%s", hold.key, exc_info=True)
        await rollback_quiet(session)


async def hold_staff_create(
    session: AsyncSession, staff: dict | None, *, pg: Any, template_id: int | None, data_limit: int | None
) -> HeldVolume | None:
    """Hold a panel-page create of the staff's shop; it has no local service row to carry it."""
    return await hold_plan_create(
        session, shop_id=reseller_id_from_staff(staff), pg=pg, template_id=template_id,
        data_limit=data_limit, key=new_hold_key("pg-create"), ttl=PANEL_ONLY_HOLD_TTL,
    )


async def hold_staff_edit(
    session: AsyncSession, staff: dict | None, *, current_limit: int | None, new_limit: int | None
) -> HeldVolume | None:
    """Hold a panel-page limit increase of the staff's shop before the panel write."""
    return await hold_limit_change(
        session, shop_id=reseller_id_from_staff(staff), current_limit=current_limit,
        new_limit=new_limit, key=new_hold_key("pg-edit"), ttl=PANEL_ONLY_HOLD_TTL,
    )


async def finish_staff_edit(session: AsyncSession, hold: HeldVolume | None, *, pg_user_id: int, data_limit: int) -> None:
    """Move an edit's volume from its hold to the shop's local service row, when there is one."""
    if hold is None:
        return
    if await record_service_limit(session, shop_id=hold.shop_id, pg_user_id=pg_user_id, data_limit=data_limit):
        await release_volume_hold(session, hold)
    await session.commit()
