"""Keep other paths from turning back on what an account hold turned off.

A hold disables a customer's or a shop's panel users until the owner releases it. Renewals,
add-ons, automatic purchases, the end of a freeze, a PAYG top-up and a subscription renewal could
each re-enable a user meanwhile, so they ask here first. When such a path wanted a user or a shop
back on, the hold takes that over and release turns it on. This module only reads and marks the
hold tables, so those services can import it without importing the hold service.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AccountHold, AccountHoldItem, UserService

OPEN_STATUSES = ("applying", "held", "releasing")
# Item states in which the hold still owns, or may still own, the panel user.
COVERING_STATES = ("pending", "sending", "disabled", "skipped", "enabling")
HELD_REFUSAL = "حساب این سرویس غیرفعال شده است؛ اول حساب را فعال کنید"


def _open_hold_ids() -> object:
    return select(AccountHold.id).where(AccountHold.status.in_(OPEN_STATUSES)).scalar_subquery()


async def held_pg_user_ids(
    session: AsyncSession, pg_user_ids: Iterable[int], *, except_hold_id: int | None = None
) -> set[int]:
    """Panel users among ``pg_user_ids`` that an open hold covers (read only)."""
    ids = {int(i) for i in pg_user_ids}
    if not ids:
        return set()
    query = select(AccountHoldItem.pg_user_id).where(
        AccountHoldItem.pg_user_id.in_(ids),
        AccountHoldItem.state.in_(COVERING_STATES),
        AccountHoldItem.hold_id.in_(_open_hold_ids()),
    )
    if except_hold_id is not None:
        query = query.where(AccountHoldItem.hold_id != int(except_hold_id))
    return {int(i) for i in await session.scalars(query)}


async def defer_reenable_to_holds(
    session: AsyncSession, pg_user_ids: Iterable[int], *, except_hold_id: int | None = None
) -> set[int]:
    """Users a bulk re-enable must skip; the covering holds turn them back on at release.

    A user the hold found already off (``skipped``) becomes the hold's own (``disabled``), because
    the path asking wanted it on again. The caller commits.
    """
    held = await held_pg_user_ids(session, pg_user_ids, except_hold_id=except_hold_id)
    if not held:
        return held
    query = (
        update(AccountHoldItem)
        .where(
            AccountHoldItem.pg_user_id.in_(held),
            AccountHoldItem.state == "skipped",
            AccountHoldItem.hold_id.in_(_open_hold_ids()),
        )
        .values(state="disabled")
        .execution_options(synchronize_session=False)
    )
    if except_hold_id is not None:
        query = query.where(AccountHoldItem.hold_id != int(except_hold_id))
    await session.execute(query)
    return held


async def is_service_held(session: AsyncSession, service_id: int | None) -> bool:
    if not service_id:
        return False
    pg_user_id = await session.scalar(select(UserService.pg_user_id).where(UserService.id == int(service_id)))
    if not pg_user_id:
        return False
    return bool(await held_pg_user_ids(session, [int(pg_user_id)]))


async def assert_service_not_held(session: AsyncSession, service_id: int | None) -> None:
    if await is_service_held(session, service_id):
        raise ValueError(HELD_REFUSAL)


async def open_shop_hold_id(session: AsyncSession, pg_admin_username: str | None) -> int | None:
    uname = (pg_admin_username or "").strip()
    if not uname:
        return None
    return await session.scalar(
        select(AccountHold.id)
        .where(
            AccountHold.kind == "shop",
            AccountHold.pg_admin_username == uname,
            AccountHold.status.in_(OPEN_STATUSES),
        )
        .limit(1)
    )


async def defer_shop_reenable_to_hold(
    session: AsyncSession, pg_admin_username: str | None, *, include_shop: bool
) -> bool:
    """True when a hold keeps this shop off; release then turns its admin (and the shop) back on.

    The caller skips enabling the panel admin and, with ``include_shop``, the shop switch it would
    have turned on. The caller commits.
    """
    hold_id = await open_shop_hold_id(session, pg_admin_username)
    if hold_id is None:
        return False
    if include_shop:
        await session.execute(
            update(AccountHold)
            .where(AccountHold.id == int(hold_id))
            .values(restore_shop_active=True)
            .execution_options(synchronize_session=False)
        )
    await session.execute(
        update(AccountHold)
        .where(AccountHold.id == int(hold_id), AccountHold.admin_state == "skipped")
        .values(admin_state="disabled")
        .execution_options(synchronize_session=False)
    )
    return True


UNBLOCK_REFUSAL = "این حساب با «غیرفعال‌سازی حساب» بسته شده است؛ از همان بخش آن را فعال کنید"


async def customer_hold_open(session: AsyncSession, customer_id: int, scope_shop_id: int | None) -> bool:
    """True while an open hold keeps this customer blocked in the scope (0 / None: account-wide)."""
    found = await session.scalar(
        select(AccountHold.id)
        .where(
            AccountHold.kind == "customer",
            AccountHold.target_user_id == int(customer_id),
            AccountHold.scope_shop_id == int(scope_shop_id or 0),
            AccountHold.status.in_(OPEN_STATUSES),
        )
        .limit(1)
    )
    return found is not None


async def shop_hold_open(session: AsyncSession, shop_user_id: int) -> bool:
    """True while an open hold keeps this shop off."""
    found = await session.scalar(
        select(AccountHold.id)
        .where(
            AccountHold.kind == "shop",
            AccountHold.target_user_id == int(shop_user_id),
            AccountHold.status.in_(OPEN_STATUSES),
        )
        .limit(1)
    )
    return found is not None
