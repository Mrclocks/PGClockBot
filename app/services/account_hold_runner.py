"""Carry out a saved account hold on the panel, one step at a time, safely repeatable.

A worker first takes the hold's lease (one worker per hold). Every panel call is preceded by a
committed state (``sending`` / ``enabling``), so a timeout or a crash leaves the step marked and
the next run repeats the same call; disabling or enabling a user twice changes nothing. No
transaction is open during a panel call. A user that was already disabled before the hold is
recorded as ``skipped`` and is never turned on by the release.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AccountHold, AccountHoldItem, PgAdminSubscription, ResellerProfile, UserService
from app.services.account_hold_guard import defer_reenable_to_holds
from app.services.account_hold_notices import notify_hold
from app.services.account_holds import (
    KIND_CUSTOMER,
    STATUS_APPLYING,
    STATUS_HELD,
    STATUS_RELEASED,
    STATUS_RELEASING,
    add_items,
    finish_customer_release,
    finish_shop_release,
    sync_shop_bots,
)
from app.services.billing_suspend import parse_id_list
from app.services.pasarguard import PasarGuardError
from app.services.service_freeze_guard import frozen_pg_user_ids

logger = logging.getLogger(__name__)

LEASE_SECONDS = 300
ITEM_BATCH = 200
PANEL_CONCURRENCY = 5
PANEL_TIMEOUT_SECONDS = 20.0
USER_PAGE = 200
# A shop with more panel users than this is listed in full over several pages; this only stops a loop.
USER_LIST_CAP = 100_000
PANEL_ERRORS = (asyncio.TimeoutError, PasarGuardError, httpx.HTTPError, ValueError)
APPLY_PENDING = ("pending", "sending")
RELEASE_PENDING = ("disabled", "sending", "enabling")
SessionFactory = Callable[[], Any]


class PanelListError(RuntimeError):
    """The panel did not return a complete list of the admin's users."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_gone(exc: BaseException) -> bool:
    return isinstance(exc, PasarGuardError) and getattr(exc, "status_code", None) == 404


async def _call(coro: Any) -> Any:
    return await asyncio.wait_for(coro, timeout=PANEL_TIMEOUT_SECONDS)


async def _move(factory: SessionFactory, model: Any, row_id: int, *, from_states: tuple[str, ...], to: str,
                column: str = "state") -> bool:
    """Compare-and-set one row's state in its own short transaction."""
    field = getattr(model, column)
    async with factory() as session:
        result = await session.execute(
            update(model).where(model.id == int(row_id), field.in_(from_states)).values({column: to})
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        return result.rowcount == 1


async def _claim(factory: SessionFactory, hold_id: int) -> str | None:
    token = secrets.token_hex(16)
    async with factory() as session:
        result = await session.execute(
            update(AccountHold)
            .where(
                AccountHold.id == int(hold_id),
                AccountHold.status.in_((STATUS_APPLYING, STATUS_RELEASING)),
                or_(AccountHold.lease_until.is_(None), AccountHold.lease_until < _now()),
            )
            .values(lease_token=token, lease_until=_now() + timedelta(seconds=LEASE_SECONDS),
                    attempts=AccountHold.attempts + 1)
            .execution_options(synchronize_session=False)
        )
        await session.commit()
    return token if result.rowcount == 1 else None


async def _drop_lease(factory: SessionFactory, hold_id: int, token: str) -> None:
    async with factory() as session:
        await session.execute(
            update(AccountHold).where(AccountHold.id == int(hold_id), AccountHold.lease_token == token)
            .values(lease_token=None, lease_until=None).execution_options(synchronize_session=False)
        )
        await session.commit()


async def _load(factory: SessionFactory, hold_id: int, token: str) -> AccountHold | None:
    """The hold while this worker still owns its lease."""
    async with factory() as session:
        return await session.scalar(
            select(AccountHold).where(AccountHold.id == int(hold_id), AccountHold.lease_token == token)
        )


async def list_admin_user_ids(pg: Any, admin_username: str) -> list[int]:
    """Every panel user the admin owns; raises ``PanelListError`` instead of returning a partial list."""
    ids: list[int] = []
    offset = 0
    while offset < USER_LIST_CAP:
        try:
            data = await _call(pg.get_users(admin=admin_username, limit=USER_PAGE, offset=offset))
        except PANEL_ERRORS as exc:
            raise PanelListError("panel user list failed") from exc
        users = (data.get("users") or data.get("items") or []) if isinstance(data, dict) else data
        if not isinstance(users, list):
            raise PanelListError("panel user list has no users")
        ids.extend(int(u["id"]) for u in users if isinstance(u, dict) and str(u.get("id", "")).isdigit())
        if len(users) < USER_PAGE:
            return ids
        offset += USER_PAGE
    raise PanelListError("panel user list too long")


async def _record_shop_users(factory: SessionFactory, pg: Any, hold: AccountHold) -> None:
    uids = await list_admin_user_ids(pg, str(hold.pg_admin_username))
    async with factory() as session:
        services = dict(
            (await session.execute(
                select(UserService.pg_user_id, UserService.id).where(UserService.pg_user_id.in_(uids))
            )).all()
        ) if uids else {}
        await add_items(session, int(hold.id), [(uid, services.get(uid)) for uid in uids])
        await session.execute(
            update(AccountHold).where(AccountHold.id == int(hold.id)).values(items_listed=True)
            .execution_options(synchronize_session=False)
        )
        await session.commit()


async def _disable_user(factory: SessionFactory, pg: Any, item: AccountHoldItem) -> None:
    uid = int(item.pg_user_id)
    try:
        if item.state == "pending":
            info = await _call(pg.get_user_by_id(uid))
            if isinstance(info, dict) and str(info.get("status") or "").lower() == "disabled":
                await _move(factory, AccountHoldItem, item.id, from_states=("pending",), to="skipped")
                return
            if not await _move(factory, AccountHoldItem, item.id, from_states=("pending",), to="sending"):
                return
        await _call(pg.set_disabled_by_id(uid, True))
    except PANEL_ERRORS as exc:
        if _is_gone(exc):
            await _move(factory, AccountHoldItem, item.id, from_states=APPLY_PENDING, to="gone")
            return
        logger.warning("account hold: disable user %s failed; will retry", uid, exc_info=True)
        return
    await _move(factory, AccountHoldItem, item.id, from_states=("sending",), to="disabled")


async def _enable_user(factory: SessionFactory, pg: Any, item: AccountHoldItem) -> None:
    uid = int(item.pg_user_id)
    if not await _move(factory, AccountHoldItem, item.id, from_states=RELEASE_PENDING, to="enabling"):
        return
    try:
        await _call(pg.set_disabled_by_id(uid, False))
    except PANEL_ERRORS as exc:
        if _is_gone(exc):
            await _move(factory, AccountHoldItem, item.id, from_states=("enabling",), to="gone")
            return
        logger.warning("account hold: enable user %s failed; will retry", uid, exc_info=True)
        return
    await _move(factory, AccountHoldItem, item.id, from_states=("enabling",), to="restored")


async def _items(factory: SessionFactory, hold_id: int, states: tuple[str, ...]) -> list[AccountHoldItem]:
    async with factory() as session:
        return list(await session.scalars(
            select(AccountHoldItem).where(AccountHoldItem.hold_id == int(hold_id), AccountHoldItem.state.in_(states))
            .order_by(AccountHoldItem.id).limit(ITEM_BATCH)
        ))


async def _run_items(items: list[AccountHoldItem], step: Callable[[AccountHoldItem], Any]) -> None:
    gate = asyncio.Semaphore(PANEL_CONCURRENCY)

    async def one(item: AccountHoldItem) -> None:
        async with gate:
            await step(item)

    await asyncio.gather(*(one(item) for item in items))


async def _drain(factory: SessionFactory, hold: AccountHold, token: str, states: tuple[str, ...],
                 step: Callable[[AccountHoldItem], Any]) -> bool:
    """Process items in batches; True when none is left in ``states``."""
    seen: set[int] = set()
    while (current := await _load(factory, int(hold.id), token)) is not None and current.status == hold.status:
        batch = [i for i in await _items(factory, int(hold.id), states) if int(i.id) not in seen]
        if not batch:
            return not await _items(factory, int(hold.id), states)
        seen.update(int(i.id) for i in batch)
        await _run_items(batch, step)
    return False


async def _admin_step(factory: SessionFactory, pg: Any, hold: AccountHold, *, enable: bool) -> None:
    uname = str(hold.pg_admin_username or "")
    if not uname:
        return
    sending = "enabling" if enable else "sending"
    if not enable and hold.admin_state == "pending":
        try:
            gate, admin = await _call(pg.get_admin_gate(uname))
        except PANEL_ERRORS:
            logger.warning("account hold: admin %s unreadable; will retry", uname, exc_info=True)
            return
        if gate == "missing":
            await _move(factory, AccountHold, hold.id, from_states=("pending",), to="gone", column="admin_state")
            return
        if gate != "ok":
            return
        if str((admin or {}).get("status") or "").lower() == "disabled":
            await _move(factory, AccountHold, hold.id, from_states=("pending",), to="skipped", column="admin_state")
            return
    start = ("disabled", "sending", "enabling") if enable else ("pending", "sending")
    if not await _move(factory, AccountHold, hold.id, from_states=start, to=sending, column="admin_state"):
        return
    try:
        await _call(pg.modify_admin(uname, {"status": "active" if enable else "disabled"}))
    except PANEL_ERRORS as exc:
        if _is_gone(exc):
            await _move(factory, AccountHold, hold.id, from_states=(sending,), to="gone", column="admin_state")
            return
        logger.warning("account hold: admin %s status change failed; will retry", uname, exc_info=True)
        return
    done = "restored" if enable else "disabled"
    await _move(factory, AccountHold, hold.id, from_states=(sending,), to=done, column="admin_state")


async def _apply(factory: SessionFactory, pg: Any, hold: AccountHold, token: str) -> None:
    if hold.admin_state in ("pending", "sending"):
        await _admin_step(factory, pg, hold, enable=False)
        hold = await _load(factory, int(hold.id), token) or hold
    if not hold.items_listed:
        try:
            await _record_shop_users(factory, pg, hold)
        except PanelListError:
            logger.warning("account hold %s: panel users unreadable; will retry", hold.id, exc_info=True)
            return
    done = await _drain(factory, hold, token, APPLY_PENDING, lambda item: _disable_user(factory, pg, item))
    current = await _load(factory, int(hold.id), token)
    if not done or current is None or current.admin_state in ("pending", "sending"):
        return
    async with factory() as session:
        await session.execute(
            update(AccountHold).where(AccountHold.id == int(hold.id), AccountHold.status == STATUS_APPLYING,
                                      AccountHold.lease_token == token)
            .values(status=STATUS_HELD).execution_options(synchronize_session=False)
        )
        await session.commit()


async def _kept_off_elsewhere(session: AsyncSession, hold: AccountHold, uids: list[int]) -> tuple[set[int], bool, bool]:
    """Users another mechanism keeps off, and whether the shop's admin and the shop itself stay off.

    A frozen service, another open hold, a PAYG suspension or an expired subscription each turn
    the user back on themselves when they end, so the release leaves those users off.
    """
    kept = await frozen_pg_user_ids(session, uids)
    kept |= await defer_reenable_to_holds(session, uids, except_hold_id=int(hold.id))
    shop_ids = {int(hold.scope_shop_id)} if hold.scope_shop_id else set()
    if hold.kind == KIND_CUSTOMER:
        shop_ids |= {int(s) for s in await session.scalars(
            select(UserService.shop_id).where(UserService.pg_user_id.in_(uids), UserService.shop_id.is_not(None))
        )} if uids else set()
    admin_off = shop_off = False
    profiles = (await session.scalars(select(ResellerProfile).where(ResellerProfile.user_id.in_(shop_ids)))).all()
    names = [p.pg_admin_username for p in profiles if p.pg_admin_username]
    subs = {
        sub.pg_username: sub
        for sub in (await session.scalars(
            select(PgAdminSubscription).where(PgAdminSubscription.pg_username.in_(names))
        )).all()
    } if names else {}
    wanted = set(uids)
    for profile in profiles:
        kept |= set(parse_id_list(profile.billing_suspended_user_ids)) & wanted
        sub = subs.get(profile.pg_admin_username or "")
        expired = sub is not None and sub.access_status == "expired"
        if expired:
            kept |= set(parse_id_list(sub.expiry_disabled_user_ids)) & wanted
        if int(profile.user_id) == int(hold.target_user_id) and hold.kind != KIND_CUSTOMER:
            admin_off = profile.billing_suspended_at is not None or expired
            shop_off = expired
    return kept, admin_off, shop_off


async def _release(factory: SessionFactory, pg: Any, hold: AccountHold, token: str) -> None:
    async with factory() as session:
        await session.execute(
            update(AccountHoldItem).where(AccountHoldItem.hold_id == int(hold.id), AccountHoldItem.state == "pending")
            .values(state="skipped").execution_options(synchronize_session=False)
        )
        # The admin step never ran: the hold did not turn the admin off, so release leaves it as is.
        await session.execute(
            update(AccountHold).where(AccountHold.id == int(hold.id), AccountHold.admin_state == "pending")
            .values(admin_state="skipped").execution_options(synchronize_session=False)
        )
        uids = [int(u) for u in await session.scalars(
            select(AccountHoldItem.pg_user_id).where(AccountHoldItem.hold_id == int(hold.id),
                                                     AccountHoldItem.state.in_(RELEASE_PENDING))
        )]
        kept, admin_off, shop_off = await _kept_off_elsewhere(session, hold, uids)
        if kept:
            await session.execute(
                update(AccountHoldItem).where(AccountHoldItem.hold_id == int(hold.id),
                                              AccountHoldItem.pg_user_id.in_(kept),
                                              AccountHoldItem.state.in_(RELEASE_PENDING))
                .values(state="left").execution_options(synchronize_session=False)
            )
        if admin_off:
            await session.execute(
                update(AccountHold).where(AccountHold.id == int(hold.id),
                                          AccountHold.admin_state.in_(("disabled", "sending", "enabling")))
                .values(admin_state="left").execution_options(synchronize_session=False)
            )
        await session.commit()
    done = await _drain(factory, hold, token, RELEASE_PENDING, lambda item: _enable_user(factory, pg, item))
    current = await _load(factory, int(hold.id), token)
    if current is None:
        return
    if current.admin_state in ("disabled", "sending", "enabling"):
        await _admin_step(factory, pg, current, enable=True)
        current = await _load(factory, int(hold.id), token)
    if not done or current is None or current.admin_state in ("disabled", "sending", "enabling"):
        return
    await _finish_release(factory, current, token, shop_off=shop_off)


async def _finish_release(factory: SessionFactory, hold: AccountHold, token: str, *, shop_off: bool) -> None:
    async with factory() as session:
        claimed = await session.execute(
            update(AccountHold).where(AccountHold.id == int(hold.id), AccountHold.status == STATUS_RELEASING,
                                      AccountHold.lease_token == token)
            .values(status=STATUS_RELEASED, released_at=_now()).execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            await session.rollback()
            return
        if hold.kind == KIND_CUSTOMER:
            await finish_customer_release(session, hold)
        else:
            await finish_shop_release(session, hold, shop_stays_off=shop_off)
        await session.commit()
        if hold.kind != KIND_CUSTOMER:
            await sync_shop_bots(session, [int(hold.target_user_id)])
        await notify_hold(session, int(hold.id), disabled=False)


async def run_hold(factory: SessionFactory, pg: Any, hold_id: int) -> str | None:
    """Advance one hold as far as the panel allows; returns its status, or None when another worker has it."""
    token = await _claim(factory, hold_id)
    if token is None:
        return None
    try:
        hold = await _load(factory, hold_id, token)
        if hold is not None and hold.status == STATUS_APPLYING:
            await _apply(factory, pg, hold, token)
        elif hold is not None and hold.status == STATUS_RELEASING:
            await _release(factory, pg, hold, token)
    finally:
        await _drop_lease(factory, hold_id, token)
    async with factory() as session:
        return await session.scalar(select(AccountHold.status).where(AccountHold.id == int(hold_id)))


UNFINISHED_BATCH = 50


async def run_unfinished_holds(factory: SessionFactory, pg: Any) -> list[int]:
    """Scheduler pass: advance every hold still applying or releasing; one failure never stops the rest."""
    async with factory() as session:
        ids = [int(i) for i in await session.scalars(
            select(AccountHold.id).where(AccountHold.status.in_((STATUS_APPLYING, STATUS_RELEASING)))
            .order_by(AccountHold.id).limit(UNFINISHED_BATCH)
        )]
    finished: list[int] = []
    for hold_id in ids:
        try:
            status = await run_hold(factory, pg, hold_id)
        except Exception:  # noqa: BLE001 - per-hold isolation; the hold keeps its saved state for the next pass
            logger.exception("account hold %s: run failed", hold_id)
            continue
        if status in (STATUS_HELD, STATUS_RELEASED):
            finished.append(hold_id)
    return finished
