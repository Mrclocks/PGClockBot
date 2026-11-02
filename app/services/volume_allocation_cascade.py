"""Level-1 shops pass volume on to their level-2 child shops within their own ceiling.

A child's ceiling is its own subscription total (``base_gb + extra_gb_purchased``), the same
number that gates the child's own sales, so there is one source of truth and nothing drifts.
The parent counts the ceilings of its active children as allocated (``volume_allocation``).
Every change that raises a child's total, or re-enables a child, is checked here against the
parent's remaining volume, under the parent profile lock, after the new total is flushed and
before the panel is told.

A parent that is not gated (PAYG, oversell allowed, no ceiling) passes anything on. A child
without a subscription total is uncapped: it counts as zero for the parent and is listed as a
warning on the parent's capacity page.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.models import OrgPrincipal, PgAdminSubscription, ResellerProfile
from app.services.billing import GB
from app.services.org_principals import DEPTH_ONE, DEPTH_TWO, STATUS_ACTIVE
from app.services.formatting import format_bytes
from app.services.volume_allocation import child_ceilings_query, gated_ceiling_locked, sum_allocated_bytes

# Bounds the children read for the capacity page warning, and the warning list itself.
CHILDREN_READ_LIMIT = 1000
UNCAPPED_LIST_LIMIT = 50


class ParentVolumeError(ValueError):
    """The parent shop has no room for a child's larger ceiling."""


@dataclass(frozen=True, slots=True)
class UncappedChild:
    shop_id: int
    pg_username: str


async def parent_shop_of(session: AsyncSession, pg_username: str) -> int | None:
    """Shop id of the level-1 parent of the active level-2 shop whose panel admin is ``pg_username``."""
    child, parent = aliased(OrgPrincipal), aliased(OrgPrincipal)
    child_profile, parent_profile = aliased(ResellerProfile), aliased(ResellerProfile)
    return await session.scalar(
        select(parent_profile.user_id)
        .select_from(child)
        .join(child_profile, child_profile.id == child.reseller_profile_id)
        .join(parent, parent.id == child.parent_id)
        .join(parent_profile, parent_profile.id == parent.reseller_profile_id)
        .where(
            child_profile.pg_admin_username == (pg_username or "").strip(),
            child.depth == DEPTH_TWO,
            child.status == STATUS_ACTIVE,
            parent.depth == DEPTH_ONE,
        )
        .limit(1)
    )


async def assert_parent_has_room(session: AsyncSession, *, pg_username: str, previous_total_gb: int) -> None:
    """Refuse a child's raised ceiling (already flushed) that its parent cannot cover.

    ``previous_total_gb`` is the child's total before the change; a decrease or no change
    always passes. Raise ``ParentVolumeError`` with an admin-facing message.
    """
    sub = await session.scalar(
        select(PgAdminSubscription).where(PgAdminSubscription.pg_username == (pg_username or "").strip())
    )
    new_total_gb = int(sub.base_gb or 0) + int(sub.extra_gb_purchased or 0) if sub is not None else 0
    if new_total_gb <= max(0, int(previous_total_gb or 0)):
        return
    parent_shop = await parent_shop_of(session, pg_username)
    if parent_shop is None:
        return
    # The lock makes a parent's own sales and its children's raises take turns.
    ceiling = await gated_ceiling_locked(session, int(parent_shop))
    if ceiling is None:
        return
    allocated = await sum_allocated_bytes(session, int(parent_shop))
    if allocated > ceiling:
        raise ParentVolumeError(
            "حجم نمایندهٔ والد کافی نیست: "
            f"{format_bytes(allocated)} از {format_bytes(ceiling)} با این تغییر تخصیص می‌یابد"
        )


async def assert_parent_has_room_for_reenabled(session: AsyncSession, child: OrgPrincipal) -> None:
    """A re-enabled child (status already flushed) counts again: its whole ceiling must fit."""
    uname = await session.scalar(
        select(ResellerProfile.pg_admin_username).where(ResellerProfile.id == child.reseller_profile_id)
    )
    if uname:
        await assert_parent_has_room(session, pg_username=str(uname), previous_total_gb=0)


async def assert_child_growth_fits(session: AsyncSession, *, pg_username: str, add_gb: int) -> None:
    """Check ``add_gb`` more for a child before anything is written (the extra-volume purchase)."""
    if isinstance(add_gb, bool) or not isinstance(add_gb, int) or add_gb <= 0:
        return
    parent_shop = await parent_shop_of(session, pg_username)
    if parent_shop is None:
        return
    ceiling = await gated_ceiling_locked(session, int(parent_shop))
    if ceiling is None:
        return
    allocated = await sum_allocated_bytes(session, int(parent_shop)) + int(add_gb) * int(GB)
    if allocated > ceiling:
        raise ParentVolumeError(
            "حجم نمایندهٔ والد کافی نیست: "
            f"{format_bytes(allocated)} از {format_bytes(ceiling)} با این خرید تخصیص می‌یابد"
        )


async def list_uncapped_children(session: AsyncSession, shop_id: int) -> list[UncappedChild]:
    """Active children of a level-1 shop without a subscription total (counted as zero)."""
    rows = (await session.execute(child_ceilings_query([int(shop_id)]).limit(CHILDREN_READ_LIMIT))).all()
    uncapped = [int(child_id) for _, child_id, gb in rows if int(gb or 0) <= 0][:UNCAPPED_LIST_LIMIT]
    if not uncapped:
        return []
    names = dict(
        (
            await session.execute(
                select(ResellerProfile.user_id, ResellerProfile.pg_admin_username).where(
                    ResellerProfile.user_id.in_(uncapped)
                )
            )
        ).all()
    )
    return [UncappedChild(shop_id=sid, pg_username=str(names.get(sid) or "")) for sid in uncapped]

