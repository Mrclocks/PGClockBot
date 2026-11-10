"""Charge PAYG shops for user changes made directly in PasarGuard, outside the bot.

Each billing tick compares a shop's panel users with ``PaygPanelSnapshot``. A difference
that no charge since the snapshot paid for (a new user, more volume, a traffic reset,
more days on a time-priced user) is charged with ``charge_shop_change``. Bot paths
already charged their own changes, so only the unpaid remainder is charged.

The first run for a shop only records the baseline: users that existed before charging
at creation are never charged. A charge key carries the snapshot revision, so a rerun
after a crash cannot charge the same difference twice. When a charge is refused for lack
of funds the snapshot is kept, the shop is alerted once, and the next run tries again.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PaygPanelSnapshot, ResellerCharge, ResellerProfile, UserService
from app.services.billing import BILLING_MODE_PAYG, WalletShortfall
from app.services.db_safe import rollback_quiet
from app.services.formatting import parse_expire
from app.services.payg_charges import ChargeStatus, send_to_review
from app.services.payg_gate import added_days, charge_shop_change, shop_charges_at_creation
from app.services.payg_pricing import ChargeEvent, ServiceChange
from app.services.provision_gate import ProvisionError
from app.services.volume_allocation import HOLD_TTL

logger = logging.getLogger(__name__)

PAGE_SIZE = 200
MAX_PANEL_USERS = 50_000
MAX_CHARGES_PER_SHOP = 5_000
SHOP_LIMIT = 500
SECONDS_PER_DAY = 86400
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_VOLUME_EVENTS = frozenset({ChargeEvent.CREATE, ChargeEvent.ADD_VOLUME, ChargeEvent.RESET})
_PERIOD_EVENTS = frozenset({ChargeEvent.RENEW, ChargeEvent.REACTIVATE})


@dataclass(frozen=True, slots=True)
class PanelUser:
    id: int
    username: str
    data_limit: int
    expire_ts: int | None
    used_traffic: int


@dataclass(slots=True)
class Paid:
    """What charges since a snapshot already paid for one user."""

    bytes: int = 0
    days: int = 0
    renewed: bool = False
    created: bool = False
    unlimited: bool = False


@dataclass(slots=True)
class ReconcileStats:
    shops: int = 0
    charged: int = 0
    baseline: int = 0
    short: int = 0
    unpriced: int = 0
    errors: int = 0
    swept: int = 0


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _nonneg(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return 0 if value is None else None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def panel_user(raw: Any) -> PanelUser | None:
    """A panel user from the API payload; ``None`` for anything malformed (never guessed)."""
    if not isinstance(raw, dict):
        return None
    uid, limit, used = _nonneg(raw.get("id")), _nonneg(raw.get("data_limit")), _nonneg(raw.get("used_traffic"))
    username = str(raw.get("username") or "").strip()
    if not uid or limit is None or used is None or not username:
        return None
    expire = parse_expire(raw.get("expire") or raw.get("expire_date"))
    return PanelUser(uid, username[:128], limit, int(expire.timestamp()) if expire else None, used)


async def _panel_users(pg: Any, admin_username: str) -> list[PanelUser]:
    users: list[PanelUser] = []
    offset = 0
    while offset < MAX_PANEL_USERS:
        data = await pg.get_users(admin=admin_username, limit=PAGE_SIZE, offset=offset, cache_max_age=0)
        page = (data.get("users") or data.get("items") or []) if isinstance(data, dict) else data
        if not isinstance(page, list) or not page:
            break
        users.extend(u for u in (panel_user(raw) for raw in page) if u is not None)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return users


def _event_of(charge: ResellerCharge) -> ChargeEvent | None:
    try:
        return ChargeEvent(str(charge.event))
    except ValueError:
        return None


def paid_since(charges: list[ResellerCharge], since: datetime) -> Paid:
    paid = Paid()
    for charge in charges:
        created = _as_utc(charge.created_at)
        if created is None or created < since or charge.status == ChargeStatus.REFUNDED:
            continue
        event = _event_of(charge)
        paid.renewed = paid.renewed or event in _PERIOD_EVENTS
        paid.created = paid.created or event == ChargeEvent.CREATE
        paid.unlimited = paid.unlimited or bool(charge.is_unlimited)
        if event in _VOLUME_EVENTS:
            paid.bytes += int(charge.bytes_charged or 0) * max(1, int(charge.quantity or 1))
        if event == ChargeEvent.ADD_DAYS:
            paid.days += int(charge.days_charged or 0)
    return paid


def _volume_change(snap: PaygPanelSnapshot, live: PanelUser, paid: Paid) -> ServiceChange | None:
    if live.data_limit == 0 and int(snap.data_limit) > 0:
        return None if paid.unlimited else ServiceChange(
            ChargeEvent.ADD_VOLUME, data_limit_bytes=0, previous_limit_bytes=int(snap.data_limit)
        )
    reset = live.used_traffic < int(snap.used_traffic)
    needed = live.data_limit if reset else max(0, live.data_limit - int(snap.data_limit))
    remainder = needed - paid.bytes
    if remainder <= 0:
        return None
    if reset:
        return ServiceChange(ChargeEvent.RESET, data_limit_bytes=remainder)
    return ServiceChange(ChargeEvent.ADD_VOLUME, data_limit_bytes=remainder, previous_limit_bytes=0)


def _days_change(snap: PaygPanelSnapshot, live: PanelUser, paid: Paid, *, since_ts: int) -> ServiceChange | None:
    days = added_days(snap.expire_ts, live.expire_ts, now_ts=since_ts)
    if days is None:
        return None if paid.unlimited else ServiceChange(ChargeEvent.ADD_DAYS, days=None)
    if days > paid.days:
        return ServiceChange(ChargeEvent.ADD_DAYS, days=days - paid.days)
    return None


def uncovered_changes(snap: PaygPanelSnapshot, live: PanelUser, paid: Paid) -> list[ServiceChange]:
    """Changes since ``snap`` that no bot charge paid for; a renewal covers the whole window.

    A bot change racing the panel read may be subtracted in two windows; that can only
    under-charge, never charge twice.
    """
    if paid.renewed:
        return []
    since_ts = int(_as_utc(snap.seen_at).timestamp())
    return [c for c in (_volume_change(snap, live, paid), _days_change(snap, live, paid, since_ts=since_ts)) if c]


def new_user_change(live: PanelUser, *, now_ts: int) -> ServiceChange:
    days = 0 if not live.expire_ts else max(1, -(-(live.expire_ts - now_ts) // SECONDS_PER_DAY))
    return ServiceChange(ChargeEvent.CREATE, data_limit_bytes=live.data_limit, days=days)


async def _charges_by_username(
    session: AsyncSession, shop_id: int, since: datetime
) -> dict[str, list[ResellerCharge]]:
    rows = (
        await session.scalars(
            select(ResellerCharge)
            .where(ResellerCharge.shop_id == shop_id, ResellerCharge.created_at >= since)
            .order_by(ResellerCharge.id)
            .limit(MAX_CHARGES_PER_SHOP)
        )
    ).all()
    grouped: dict[str, list[ResellerCharge]] = defaultdict(list)
    for row in rows:
        grouped[str(row.pg_username or "")].append(row)
    return grouped


@dataclass(slots=True)
class _ShopState:
    shop_id: int
    snapshots: dict[int, PaygPanelSnapshot]
    local_user_ids: set[int]
    charges: dict[str, list[ResellerCharge]]


async def _shop_state(session: AsyncSession, shop_id: int, now: datetime) -> _ShopState:
    snapshots = {
        int(s.pg_user_id): s
        for s in (await session.scalars(select(PaygPanelSnapshot).where(PaygPanelSnapshot.shop_id == shop_id))).all()
    }
    local = set(
        (await session.scalars(
            select(UserService.pg_user_id).where(UserService.shop_id == shop_id, UserService.pg_user_id.is_not(None))
        )).all()
    )
    since = min((_as_utc(s.seen_at) for s in snapshots.values()), default=now)
    return _ShopState(shop_id, snapshots, {int(u) for u in local}, await _charges_by_username(session, shop_id, since))


def _snapshot_from(shop_id: int, live: PanelUser, now: datetime) -> PaygPanelSnapshot:
    return PaygPanelSnapshot(
        shop_id=shop_id, pg_user_id=live.id, pg_username=live.username, data_limit=live.data_limit,
        expire_ts=live.expire_ts, used_traffic=live.used_traffic, revision=0, seen_at=now,
    )


def _refresh_snapshot(snap: PaygPanelSnapshot, live: PanelUser, now: datetime) -> None:
    snap.pg_username = live.username
    snap.data_limit = live.data_limit
    snap.expire_ts = live.expire_ts
    snap.used_traffic = live.used_traffic
    snap.revision = int(snap.revision or 0) + 1
    snap.seen_at = now


async def _charge_all(
    session: AsyncSession, state: _ShopState, live: PanelUser, changes: list[ServiceChange], *, key_base: str,
    stats: ReconcileStats,
) -> bool:
    """Charge each change; False when one must be retried (shortfall). Unpriceable ones are logged."""
    for index, change in enumerate(changes):
        try:
            charge = await charge_shop_change(
                session, shop_id=state.shop_id, change=change, request_key=f"{key_base}:{index}",
                pg_username=live.username, panel_target={"pg_user_id": live.id, "source": "panel"},
                created_by="reconcile",
                ladder_exempt=True,
            )
        except ProvisionError as exc:
            if isinstance(exc.__cause__, WalletShortfall):
                stats.short += 1
                return False
            stats.unpriced += 1
            logger.warning("payg reconcile could not price shop=%s user=%s: %s", state.shop_id, live.id, exc.message)
            continue
        if charge is not None:
            stats.charged += 1
    return True


async def _reconcile_user(
    session: AsyncSession, state: _ShopState, live: PanelUser, *, first_run: bool, now: datetime,
    stats: ReconcileStats,
) -> None:
    snap = state.snapshots.get(live.id)
    charges = state.charges.get(live.username, [])
    if snap is None:
        made_by_bot = live.id in state.local_user_ids or paid_since(charges, _EPOCH).created
        changes = [] if first_run or made_by_bot else [new_user_change(live, now_ts=int(now.timestamp()))]
        if first_run:
            stats.baseline += 1
        if await _charge_all(session, state, live, changes, key_base=f"recon:{live.id}:new", stats=stats):
            session.add(_snapshot_from(state.shop_id, live, now))
            await session.commit()
        return
    changes = uncovered_changes(snap, live, paid_since(charges, _as_utc(snap.seen_at)))
    if await _charge_all(session, state, live, changes, key_base=f"recon:{live.id}:{int(snap.revision or 0)}", stats=stats):
        _refresh_snapshot(snap, live, now)
        await session.commit()


async def reconcile_shop(
    session: AsyncSession, profile: ResellerProfile, pg: Any, *, now: datetime, stats: ReconcileStats
) -> None:
    shop_id = int(profile.user_id)
    users = await _panel_users(pg, str(profile.pg_admin_username))
    state = await _shop_state(session, shop_id, now)
    first_run = not state.snapshots
    for live in users:
        await _reconcile_user(session, state, live, first_run=first_run, now=now, stats=stats)


async def sweep_stale_pending(session: AsyncSession, *, now: datetime, older_than: timedelta = HOLD_TTL) -> int:
    """Charges left pending (a crash between the panel call and the settle) go to review."""
    cutoff = now - older_than
    stale = (
        await session.scalars(
            select(ResellerCharge)
            .where(ResellerCharge.status == str(ChargeStatus.PENDING), ResellerCharge.created_at < cutoff)
            .order_by(ResellerCharge.id)
            .limit(MAX_CHARGES_PER_SHOP)
        )
    ).all()
    moved = 0
    for charge in stale:
        moved += int(await send_to_review(session, charge))
    return moved


async def run_payg_reconcile(session: AsyncSession, pg: Any, *, now: datetime | None = None) -> ReconcileStats:
    """One pass over PAYG shops charged at creation; one shop's failure never stops the rest."""
    now = now or datetime.now(timezone.utc)
    stats = ReconcileStats()
    stats.swept = await sweep_stale_pending(session, now=now)
    profiles = (
        await session.scalars(
            select(ResellerProfile)
            .where(ResellerProfile.is_active.is_(True), ResellerProfile.billing_mode == BILLING_MODE_PAYG)
            .order_by(ResellerProfile.id)
            .limit(SHOP_LIMIT)
        )
    ).all()
    for profile in profiles:
        if not (profile.pg_admin_username or "").strip():
            continue
        try:
            if not await shop_charges_at_creation(session, int(profile.user_id)):
                await _drop_snapshots(session, int(profile.user_id))
                continue
            stats.shops += 1
            await reconcile_shop(session, profile, pg, now=now, stats=stats)
        except Exception:  # noqa: BLE001 - per-shop isolation in a background job; logged and counted
            stats.errors += 1
            logger.warning("payg reconcile failed shop=%s", profile.user_id, exc_info=True)
            await rollback_quiet(session)
    return stats


async def _drop_snapshots(session: AsyncSession, shop_id: int) -> None:
    """A shop not charged at creation keeps no baseline, so switching it on starts afresh."""
    result = await session.execute(delete(PaygPanelSnapshot).where(PaygPanelSnapshot.shop_id == shop_id))
    if result.rowcount:
        await session.commit()
