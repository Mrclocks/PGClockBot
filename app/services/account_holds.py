"""Turn a customer or a shop off together with every panel service, and back on exactly as before.

Opening a hold saves everything it will change before the panel is called: the hold row (one open
hold per target and scope), the block or shop switch it flips, and the panel users it will turn
off. ``account_hold_runner`` then talks to the panel item by item and can repeat any step after a
timeout. Release turns back on only what the hold turned off. Authorization is checked by the
caller (``account_hold_access``); this module trusts the target it is given.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AccountHold,
    AccountHoldItem,
    BotUser,
    BotUserShop,
    OrgPrincipal,
    ResellerProfile,
    UserService,
)
from app.services.account_hold_guard import OPEN_STATUSES
from app.services.db_safe import rollback_quiet
from app.services.org_principals import DEPTH_TWO
from app.services.shop_membership import ensure_member

logger = logging.getLogger(__name__)

KIND_CUSTOMER = "customer"
KIND_SHOP = "shop"
PLATFORM_SCOPE = 0
STATUS_APPLYING = "applying"
STATUS_HELD = "held"
STATUS_RELEASING = "releasing"
STATUS_RELEASED = "released"
ADMIN_NONE = "none"
ADMIN_PENDING = "pending"
# A customer has a handful of services; this bounds one hold's local service read.
SERVICE_READ_LIMIT = 5000
REASON_MIN = 3
REASON_MAX = 255
ACTOR_MAX = 128

ALREADY_HELD = "این حساب از قبل غیرفعال است"
NOT_HELD = "این حساب غیرفعال نیست"
REASON_REQUIRED = f"علت غیرفعال‌سازی الزامی است (حداقل {REASON_MIN} کاراکتر)"


@dataclass(frozen=True, slots=True)
class HoldRequest:
    """Who asked and why; the target itself is resolved and authorized by the caller."""

    actor: str
    reason: str
    placed_by_shop_id: int = PLATFORM_SCOPE


def clean_reason(raw: object) -> str:
    reason = " ".join(str(raw or "").split())
    if len(reason) < REASON_MIN:
        raise ValueError(REASON_REQUIRED)
    return reason[:REASON_MAX]


def _open(query_kind: str, target_user_id: int, scope_shop_id: int) -> object:
    return select(AccountHold).where(
        AccountHold.kind == query_kind,
        AccountHold.target_user_id == int(target_user_id),
        AccountHold.scope_shop_id == int(scope_shop_id),
        AccountHold.status.in_(OPEN_STATUSES),
    )


async def open_hold_for(
    session: AsyncSession, *, kind: str, target_user_id: int, scope_shop_id: int
) -> AccountHold | None:
    return await session.scalar(_open(kind, target_user_id, scope_shop_id).limit(1))


async def _insert_hold(session: AsyncSession, hold: AccountHold) -> AccountHold:
    """Add the hold; a second open hold for the same target loses on the partial unique index."""
    session.add(hold)
    try:
        await session.flush()
    except IntegrityError as exc:
        await rollback_quiet(session)
        raise ValueError(ALREADY_HELD) from exc
    return hold


async def _set_customer_block(session: AsyncSession, customer: BotUser, scope_shop_id: int, *, blocked: bool) -> bool:
    """Set the block flag of the hold's scope; returns whether it was on before."""
    if scope_shop_id == PLATFORM_SCOPE:
        was_blocked = bool(customer.is_blocked)
        customer.is_blocked = blocked
        return was_blocked
    await ensure_member(session, int(customer.id), int(scope_shop_id))
    row = await session.scalar(
        select(BotUserShop)
        .where(BotUserShop.bot_user_id == int(customer.id), BotUserShop.shop_id == int(scope_shop_id))
        .with_for_update()
    )
    was_blocked = bool(row.is_blocked)
    row.is_blocked = blocked
    row.blocked_at = datetime.now(timezone.utc) if blocked else None
    if not blocked:
        row.blocked_reason = None
    return was_blocked


def _customer_services(customer_id: int, scope_shop_id: int) -> object:
    query = select(UserService.id, UserService.pg_user_id).where(
        UserService.bot_user_id == int(customer_id),
        UserService.is_cancelled.is_(False),
        UserService.pg_user_id.is_not(None),
    )
    if scope_shop_id != PLATFORM_SCOPE:
        query = query.where(UserService.shop_id == int(scope_shop_id))
    return query.order_by(UserService.id).limit(SERVICE_READ_LIMIT)


async def add_items(session: AsyncSession, hold_id: int, pairs: list[tuple[int, int | None]]) -> None:
    """Record panel users (``pg_user_id``, ``service_id``) for the hold; repeats are ignored."""
    rows = [{"hold_id": int(hold_id), "pg_user_id": int(uid), "service_id": sid} for uid, sid in pairs if uid]
    if not rows:
        return
    await session.execute(
        insert(AccountHoldItem).values(rows).on_conflict_do_nothing(constraint="uq_account_hold_items_user")
    )


async def open_customer_hold(
    session: AsyncSession, *, customer: BotUser, scope_shop_id: int, request: HoldRequest
) -> AccountHold:
    """Block the customer in ``scope_shop_id`` (0: account-wide) and save the services to turn off.

    The caller commits, together with its audit record.
    """
    hold = await _insert_hold(
        session,
        AccountHold(
            kind=KIND_CUSTOMER, target_user_id=int(customer.id), scope_shop_id=int(scope_shop_id),
            status=STATUS_APPLYING, reason=request.reason, actor=request.actor[:ACTOR_MAX], items_listed=True,
            placed_by_shop_id=int(request.placed_by_shop_id),
        ),
    )
    was_blocked = await _set_customer_block(session, customer, int(scope_shop_id), blocked=True)
    hold.restore_unblock = not was_blocked
    services = (await session.execute(_customer_services(int(customer.id), int(scope_shop_id)))).all()
    await add_items(session, int(hold.id), [(int(uid), int(sid)) for sid, uid in services])
    return hold


async def _child_shop_ids(session: AsyncSession, profile: ResellerProfile) -> list[int]:
    """Shop (bot user) ids of the level-2 shops directly under this shop."""
    parent = await session.scalar(
        select(OrgPrincipal.id).where(OrgPrincipal.reseller_profile_id == int(profile.id)).limit(1)
    )
    if parent is None:
        return []
    rows = await session.scalars(
        select(ResellerProfile.user_id)
        .join(OrgPrincipal, OrgPrincipal.reseller_profile_id == ResellerProfile.id)
        .where(OrgPrincipal.parent_id == int(parent), OrgPrincipal.depth == DEPTH_TWO)
        .order_by(ResellerProfile.user_id)
    )
    return [int(uid) for uid in rows]


async def _new_shop_hold(
    session: AsyncSession, profile: ResellerProfile, request: HoldRequest, parent_hold_id: int | None
) -> AccountHold:
    uname = (profile.pg_admin_username or "").strip() or None
    hold = await _insert_hold(
        session,
        AccountHold(
            kind=KIND_SHOP, target_user_id=int(profile.user_id), scope_shop_id=int(profile.user_id),
            status=STATUS_APPLYING, parent_hold_id=parent_hold_id, pg_admin_username=uname,
            admin_state=ADMIN_PENDING if uname else ADMIN_NONE, restore_shop_active=bool(profile.is_active),
            items_listed=uname is None, reason=request.reason, actor=request.actor[:ACTOR_MAX],
            placed_by_shop_id=int(request.placed_by_shop_id),
        ),
    )
    profile.is_active = False
    return hold


async def open_shop_hold(session: AsyncSession, *, profile: ResellerProfile, request: HoldRequest) -> AccountHold:
    """Switch the shop and its level-2 shops off; the runner turns their panel admins and users off.

    The caller commits, together with its audit record.
    """
    locked = await session.scalar(
        select(ResellerProfile).where(ResellerProfile.id == int(profile.id)).with_for_update()
    )
    hold = await _new_shop_hold(session, locked, request, None)
    for child_id in await _child_shop_ids(session, locked):
        if await open_hold_for(session, kind=KIND_SHOP, target_user_id=child_id, scope_shop_id=child_id):
            continue  # already off on its own; its own release decides
        child = await session.scalar(
            select(ResellerProfile).where(ResellerProfile.user_id == child_id).with_for_update()
        )
        if child is not None:
            await _new_shop_hold(session, child, request, int(hold.id))
    return hold


async def start_release(session: AsyncSession, hold: AccountHold) -> list[int]:
    """Claim the hold and its child holds for release; returns the ids the runner should process."""
    claimed = await session.execute(
        update(AccountHold)
        .where(
            (AccountHold.id == int(hold.id)) | (AccountHold.parent_hold_id == int(hold.id)),
            AccountHold.status.in_((STATUS_APPLYING, STATUS_HELD)),
        )
        .values(status=STATUS_RELEASING)
        .returning(AccountHold.id)
        .execution_options(synchronize_session=False)
    )
    ids = [int(i) for i in claimed.scalars()]
    await session.commit()
    if int(hold.id) not in ids:
        raise ValueError(NOT_HELD)
    return ids


async def finish_customer_release(session: AsyncSession, hold: AccountHold) -> None:
    """Put the block flag back as it was before the hold (the caller commits)."""
    if not hold.restore_unblock:
        return
    customer = await session.get(BotUser, int(hold.target_user_id))
    if customer is not None:
        await _set_customer_block(session, customer, int(hold.scope_shop_id), blocked=False)


async def finish_shop_release(session: AsyncSession, hold: AccountHold, *, shop_stays_off: bool) -> None:
    """Switch the shop back on when it was on before (the caller commits)."""
    if not hold.restore_shop_active or shop_stays_off:
        return
    await session.execute(
        update(ResellerProfile)
        .where(ResellerProfile.user_id == int(hold.target_user_id))
        .values(is_active=True)
        .execution_options(synchronize_session=False)
    )


async def hold_progress(session: AsyncSession, hold_id: int) -> dict[str, int]:
    """Item counts by state, for the edit pages."""
    rows = await session.execute(
        select(AccountHoldItem.state, func.count())
        .where(AccountHoldItem.hold_id == int(hold_id))
        .group_by(AccountHoldItem.state)
    )
    return {str(state): int(count) for state, count in rows.all()}


async def sync_shop_bots(session: AsyncSession, shop_user_ids: list[int]) -> None:
    """Stop or start the shops' own bots to match their switch, after the commit that changed it."""
    # Deferred: the bot manager pulls in aiogram and the bot layer at import time.
    from app.services.reseller_bots import sync_reseller_bot_runtime

    if not shop_user_ids:
        return
    profiles = (await session.scalars(
        select(ResellerProfile).where(ResellerProfile.user_id.in_([int(i) for i in shop_user_ids]))
    )).all()
    for profile in profiles:
        try:
            await sync_reseller_bot_runtime(profile)
        except (RuntimeError, OSError):
            logger.warning("account hold: shop bot %s could not be synced", profile.user_id, exc_info=True)


async def shop_hold_tree_ids(session: AsyncSession, hold_id: int) -> list[int]:
    """The shop ids of a shop hold and its child holds."""
    rows = await session.scalars(
        select(AccountHold.target_user_id).where(
            (AccountHold.id == int(hold_id)) | (AccountHold.parent_hold_id == int(hold_id)),
            AccountHold.kind == KIND_SHOP,
        )
    )
    return [int(i) for i in rows]
