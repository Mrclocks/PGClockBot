"""Shared Provision Gate: PG quota + Billing empty-balance policy.

Every operation that creates a new financial commitment for a reseller shop
must pass through here (create user, renew, volume/plan change, similar).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.billing import BillingError, assert_billing_allows_provision
from app.services.pg_quota import (
    PgQuotaError,
    assert_can_create_user,
    assert_can_modify_user,
    assert_can_mutate_owned_users,
    assert_reseller_can_deliver,
    assert_reseller_can_renew,
)
from app.services.shop_scope import shop_owner_id


class ProvisionError(Exception):
    """Unified provision failure (quota or billing)."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def _wrap(exc: Exception) -> ProvisionError:
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
                session=session,
                data_limit=data_limit,
                expire_ts=expire_ts,
                from_template=from_template,
                quantity=quantity,
            )
        elif pg_admin_username is not None or rid:
            await assert_reseller_can_deliver(
                pg_admin_username=pg_admin_username,
                pg_role_id=pg_role_id,
                data_limit=data_limit,
                expire_ts=expire_ts,
                from_template=from_template,
                quantity=quantity,
            )
        elif staff is not None:
            await assert_can_create_user(
                staff,
                session=session,
                data_limit=data_limit,
                expire_ts=expire_ts,
                from_template=from_template,
                quantity=quantity,
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
                    session=session,
                    data_limit=data_limit,
                    expire_ts=expire_ts,
                    data_limit_changed=data_limit is not None,
                    expire_changed=expire_ts is not None,
                )
        else:
            await assert_reseller_can_renew(
                pg_admin_username=pg_admin_username,
                pg_role_id=pg_role_id,
                data_limit=data_limit,
                expire_ts=expire_ts,
                from_template=from_template,
            )
    except PgQuotaError as e:
        raise _wrap(e) from e


async def assert_provision_modify(
    session: AsyncSession,
    staff: dict,
    *,
    data_limit: int | None = None,
    expire_ts: int | None = None,
    data_limit_changed: bool = False,
    expire_changed: bool = False,
) -> None:
    """Gate for PG user edit (volume / expire / plan-like changes)."""
    rid = reseller_id_from_staff(staff)
    # Only block when the mutation increases commitment (volume/time change)
    if data_limit_changed or expire_changed:
        try:
            await assert_billing_allows_provision(session, reseller_user_id=rid)
        except BillingError as e:
            raise _wrap(e) from e
    try:
        await assert_can_modify_user(
            staff,
            session=session,
            data_limit=data_limit,
            expire_ts=expire_ts,
            data_limit_changed=data_limit_changed,
            expire_changed=expire_changed,
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
