"""Admin approval of new customers before a bot opens its menus to them.

Each shop (and the platform, ``shop_key`` 0) decides for its own bot. A customer who was
already a customer of the shop when the gate was switched on, or who came with an invitation
of that shop while «معافیت ورود با لینک دعوت» is on, gets an ``approved`` row the first time
they are seen, so later checks cost one indexed lookup.

Every change of status is an atomic ``UPDATE ... WHERE status IN (...)`` checked by its row
count, so two admins (or an admin and the expiry) can never both decide one request, and the
admin notice is claimed once through ``admin_notified_at``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import BotUser, BotUserShop, EntryApproval, Order, Role
from app.py_compat import StrEnum
from app.services.authz import authz_from_staff, can_shop
from app.services.entry_approval_config import (
    KEY_ENABLED,
    PERMISSION,
    EntryApprovalConfig,
    read_config,
)
from app.services.purchase_contact import verified_contact_phone_hash
from app.services.referral_scope import is_referral_of_shop
from app.services.reseller_access import resolve_reseller_owner_id
from app.services.resellers import get_reseller_profile, has_bot_perm
from app.services.shop_scope import is_platform_admin, shop_owner_id
from app.services.users import get_all_settings, set_settings_bulk

logger = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_BLOCKED = "blocked"
# Why a customer was let in without a request; kept in decided_by so it can be traced later.
EXEMPT_INVITED = "exempt:invited"
EXEMPT_MEMBER = "exempt:member"
DECIDED_BY_EXPIRY = "expired"
LIST_LIMIT = 50
VIEW_PENDING = "pending"
VIEW_CLOSED = "closed"

NOT_PENDING = "این درخواست پیدا نشد یا قبلاً بررسی شده است."
RETRY_CLOSED = "امکان درخواست دوباره وجود ندارد."
RETRY_NOT_YET = "هنوز زمان درخواست دوباره نرسیده است."
NO_ACCESS = "دسترسی تأیید ورود کاربران را ندارید."


class GateStep(StrEnum):
    OPEN = "open"
    NEEDS_PHONE = "needs_phone"
    PENDING = "pending"
    REJECTED = "rejected"
    BLOCKED = "blocked"


class Action(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    BLOCK = "block"


# Status an action leads to, and the statuses it may start from.
_TRANSITIONS: dict[Action, tuple[str, frozenset[str]]] = {
    Action.APPROVE: (STATUS_APPROVED, frozenset({STATUS_PENDING, STATUS_REJECTED, STATUS_BLOCKED})),
    Action.REJECT: (STATUS_REJECTED, frozenset({STATUS_PENDING})),
    Action.BLOCK: (STATUS_BLOCKED, frozenset({STATUS_PENDING, STATUS_REJECTED})),
}


@dataclass(frozen=True, slots=True)
class GateState:
    step: GateStep
    approval_id: int | None = None
    # A pending request whose admins were not told yet.
    needs_notice: bool = False
    # A rejected customer may ask again from this moment (None = never).
    retry_at: datetime | None = None

    @property
    def is_open(self) -> bool:
        return self.step is GateStep.OPEN


OPEN = GateState(GateStep.OPEN)


@dataclass(frozen=True, slots=True)
class Decision:
    approval_id: int
    user_id: int
    shop_key: int
    status: str


def shop_key_of(shop_id: int | None) -> int:
    """0 is the platform, otherwise the shop owner's bot_user id."""
    return int(shop_id or 0)


def as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def gate_config(session: AsyncSession, shop_key: int, ui: Mapping[str, str] | None = None) -> EntryApprovalConfig | None:
    """The gate's settings when it applies to ``shop_key``; ``None`` when customers enter freely.

    A shop needs the «تأیید ورود مشتریان جدید» permission: without it nobody there could
    decide, and its new customers would wait forever.
    """
    if ui is None:
        ui = await get_all_settings(session, reseller_id=shop_key or None)
    config = read_config(ui)
    if not config.enabled:
        return None
    if shop_key and not has_bot_perm(await get_reseller_profile(session, shop_key), PERMISSION):
        return None
    if config.since is None:
        # Switched on without a recorded moment (restored backup, direct DB edit): record it now,
        # so nobody who already uses the bot is shut out.
        await set_settings_bulk(session, {KEY_ENABLED: "1"}, reseller_id=shop_key or None)
        config = read_config(await get_all_settings(session, reseller_id=shop_key or None))
    return config


async def _load(session: AsyncSession, user_id: int, shop_key: int) -> EntryApproval | None:
    return await session.scalar(
        select(EntryApproval)
        .where(EntryApproval.bot_user_id == int(user_id), EntryApproval.shop_key == int(shop_key))
        .execution_options(populate_existing=True)
    )


async def _insert(
    session: AsyncSession, user_id: int, shop_key: int, status: str, *, decided_by: str | None = None
) -> EntryApproval:
    """Create the customer's row once; a simultaneous insert of the same pair wins quietly."""
    now = datetime.now(timezone.utc)
    try:
        async with session.begin_nested():
            session.add(EntryApproval(
                bot_user_id=int(user_id), shop_key=int(shop_key), status=status, requested_at=now,
                decided_at=now if decided_by else None, decided_by=decided_by,
            ))
            await session.flush()
    except IntegrityError:
        logger.debug("entry approval already recorded user=%s shop=%s", user_id, shop_key)
    await session.commit()
    row = await _load(session, user_id, shop_key)
    if row is None:  # pragma: no cover - the unique pair was just written or found
        raise RuntimeError("entry approval row missing after insert")
    return row


async def _joined_before(session: AsyncSession, user: BotUser, shop_key: int, since: datetime) -> bool:
    """True when the person was already a customer of this shop when the gate was switched on."""
    shop_clause = BotUserShop.shop_id.is_(None) if not shop_key else BotUserShop.shop_id == shop_key
    first_seen = await session.scalar(
        select(func.min(BotUserShop.created_at)).where(BotUserShop.bot_user_id == int(user.id), shop_clause)
    )
    # Visits to a second bot were only recorded recently, so an older order in this shop
    # also proves the person was already its customer.
    order_clause = Order.reseller_id.is_(None) if not shop_key else Order.reseller_id == shop_key
    first_order = await session.scalar(
        select(func.min(Order.created_at)).where(Order.user_id == int(user.id), order_clause)
    )
    candidates = [as_utc(seen) for seen in (first_seen, first_order) if seen]
    if shop_key_of(user.reseller_id) == shop_key and user.created_at:
        candidates.append(as_utc(user.created_at))
    # SQLite keeps whole seconds, so someone who joined in the second the gate was switched on
    # would read as earlier than the switch; compare whole seconds and count that second as after.
    return bool(candidates) and min(candidates) < since.replace(microsecond=0)


async def _exemption(session: AsyncSession, user: BotUser, shop_key: int, config: EntryApprovalConfig) -> str | None:
    """Why ``user`` may enter without a request (an ``EXEMPT_*`` reason), or None."""
    if config.skip_referred and is_referral_of_shop(user, shop_key or None):
        return EXEMPT_INVITED
    if config.since is not None and await _joined_before(session, user, shop_key, config.since):
        return EXEMPT_MEMBER
    return None


async def _expire_if_stale(session: AsyncSession, row: EntryApproval, config: EntryApprovalConfig) -> None:
    if row.status != STATUS_PENDING or config.expire_days <= 0:
        return
    now = datetime.now(timezone.utc)
    if as_utc(row.requested_at) > now - timedelta(days=config.expire_days):
        return
    await session.execute(
        update(EntryApproval)
        .where(EntryApproval.id == row.id, EntryApproval.status == STATUS_PENDING)
        .values(status=STATUS_REJECTED, decided_at=now, decided_by=DECIDED_BY_EXPIRY),
        execution_options={"synchronize_session": False},
    )
    await session.commit()
    await session.refresh(row)


def _state_of(row: EntryApproval, config: EntryApprovalConfig) -> GateState:
    if row.status == STATUS_APPROVED:
        return OPEN
    if row.status == STATUS_PENDING:
        return GateState(GateStep.PENDING, row.id, needs_notice=row.admin_notified_at is None)
    if row.status == STATUS_BLOCKED:
        return GateState(GateStep.BLOCKED, row.id)
    retry_at = None
    if config.retry_hours > 0 and row.decided_at is not None:
        retry_at = as_utc(row.decided_at) + timedelta(hours=config.retry_hours)
    return GateState(GateStep.REJECTED, row.id, retry_at=retry_at)


async def resolve_gate(
    session: AsyncSession, user: BotUser, *, shop_key: int, config: EntryApprovalConfig, create: bool = True
) -> GateState:
    """Where ``user`` stands at this shop's door. The caller has already checked the gate applies.

    With ``create`` a new customer's request is opened (after the phone step when it is on);
    without it (the Mini App) a new customer simply stays outside.
    """
    if (user.role or Role.USER.value) != Role.USER.value:
        # Resellers and admins were let in by other means (an approved application, ADMIN_IDS).
        return OPEN
    row = await _load(session, int(user.id), shop_key)
    if row is None:
        reason = await _exemption(session, user, shop_key, config)
        if reason is not None:
            logger.info("entry approval: user=%s shop=%s let in without a request (%s)", user.id, shop_key, reason)
            await _insert(session, int(user.id), shop_key, STATUS_APPROVED, decided_by=reason)
            return OPEN
        if not create:
            return GateState(GateStep.PENDING)
        if config.require_phone and not await verified_contact_phone_hash(session, int(user.id), shop_key=shop_key):
            return GateState(GateStep.NEEDS_PHONE)
        row = await _insert(session, int(user.id), shop_key, STATUS_PENDING)
    await _expire_if_stale(session, row, config)
    return _state_of(row, config)


async def claim_admin_notice(session: AsyncSession, approval_id: int) -> bool:
    """True for exactly one caller per pending request: that caller tells the admins."""
    result = await session.execute(
        update(EntryApproval)
        .where(
            EntryApproval.id == int(approval_id),
            EntryApproval.status == STATUS_PENDING,
            EntryApproval.admin_notified_at.is_(None),
        )
        .values(admin_notified_at=datetime.now(timezone.utc)),
        execution_options={"synchronize_session": False},
    )
    await session.commit()
    return result.rowcount == 1


async def request_again(session: AsyncSession, user_id: int, *, shop_key: int, config: EntryApprovalConfig) -> GateState:
    """A rejected customer asks again once the waiting time is over. Raises ``ValueError``."""
    if config.retry_hours <= 0:
        raise ValueError(RETRY_CLOSED)
    now = datetime.now(timezone.utc)
    result = await session.execute(
        update(EntryApproval)
        .where(
            EntryApproval.bot_user_id == int(user_id),
            EntryApproval.shop_key == int(shop_key),
            EntryApproval.status == STATUS_REJECTED,
            EntryApproval.decided_at <= now - timedelta(hours=config.retry_hours),
        )
        .values(status=STATUS_PENDING, requested_at=now, decided_at=None, decided_by=None, admin_notified_at=None),
        execution_options={"synchronize_session": False},
    )
    await session.commit()
    if result.rowcount != 1:
        row = await _load(session, user_id, shop_key)
        raise ValueError(RETRY_NOT_YET if row is not None and row.status == STATUS_REJECTED else RETRY_CLOSED)
    row = await _load(session, user_id, shop_key)
    return _state_of(row, config) if row is not None else OPEN


async def decide(
    session: AsyncSession, approval_id: int, *, shop_key: int, action: Action, decided_by: str
) -> Decision:
    """Approve, reject or block one request of ``shop_key``. Raises ``ValueError`` when it is not open."""
    target, sources = _TRANSITIONS[action]
    user_id = await session.scalar(
        select(EntryApproval.bot_user_id).where(EntryApproval.id == int(approval_id), EntryApproval.shop_key == int(shop_key))
    )
    if user_id is None:
        raise ValueError(NOT_PENDING)
    result = await session.execute(
        update(EntryApproval)
        .where(
            EntryApproval.id == int(approval_id),
            EntryApproval.shop_key == int(shop_key),
            EntryApproval.status.in_(sources),
        )
        .values(status=target, decided_at=datetime.now(timezone.utc), decided_by=decided_by[:64]),
        execution_options={"synchronize_session": False},
    )
    await session.commit()
    if result.rowcount != 1:
        raise ValueError(NOT_PENDING)
    return Decision(approval_id=int(approval_id), user_id=int(user_id), shop_key=int(shop_key), status=target)


def _view_filter(view: str, expire_days: int) -> tuple[Any, Any]:
    if view == VIEW_CLOSED:
        return EntryApproval.status.in_((STATUS_REJECTED, STATUS_BLOCKED)), EntryApproval.decided_at.desc()
    condition = EntryApproval.status == STATUS_PENDING
    if expire_days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=expire_days)
        condition = condition & (EntryApproval.requested_at > cutoff)
    return condition, EntryApproval.requested_at


async def list_requests(
    session: AsyncSession, shop_key: int, *, view: str = VIEW_PENDING, limit: int = LIST_LIMIT
) -> list[dict[str, Any]]:
    """Requests of one shop: waiting ones oldest first, or rejected and blocked ones newest first."""
    config = read_config(await get_all_settings(session, reseller_id=shop_key or None))
    condition, order = _view_filter(view, config.expire_days)
    rows = await session.execute(
        select(EntryApproval, BotUser.telegram_id, BotUser.full_name, BotUser.username)
        .join(BotUser, BotUser.id == EntryApproval.bot_user_id)
        .where(EntryApproval.shop_key == int(shop_key), condition)
        .order_by(order, EntryApproval.id)
        .limit(int(limit))
    )
    return [
        {
            "id": int(row.id),
            "customer": int(telegram_id),
            "customer_name": full_name or "",
            "username": username or "",
            "status": row.status,
            "requested_at": as_utc(row.requested_at).isoformat(),
            "decided_at": as_utc(row.decided_at).isoformat() if row.decided_at else "",
            "expired": row.decided_by == DECIDED_BY_EXPIRY,
        }
        for row, telegram_id, full_name, username in rows.all()
    ]


def approval_scope(staff: Mapping[str, Any]) -> int:
    """The shop whose requests ``staff`` may decide (0 = platform). Raises ``PermissionError``."""
    if is_platform_admin(dict(staff)):
        return 0
    if not can_shop(authz_from_staff(staff), PERMISSION):
        raise PermissionError(NO_ACCESS)
    shop_id = shop_owner_id(dict(staff))
    if shop_id is None:
        raise PermissionError(NO_ACCESS)
    return int(shop_id)


async def can_decide_in_bot(session: AsyncSession, reviewer: BotUser, *, shop_key: int, bot_shop_id: int | None) -> bool:
    """Whether ``reviewer``, tapping in the bot of ``bot_shop_id``, may decide requests of ``shop_key``.

    Platform requests are decided by the platform admins in the main bot; a shop's requests by
    its owner or bot admins in that shop's own bot, while the shop has the permission.
    """
    if shop_key == 0:
        return bot_shop_id is None and int(reviewer.telegram_id or 0) in get_settings().admin_ids
    if shop_key_of(bot_shop_id) != shop_key:
        return False
    owner = await resolve_reseller_owner_id(session, reviewer, is_reseller_bot=True, reseller_owner_id=shop_key)
    if owner != shop_key:
        return False
    return has_bot_perm(await get_reseller_profile(session, shop_key), PERMISSION)


def staff_label(*, username: str | None, user_id: int | str | None, role: str | None = None) -> str:
    who = username or user_id or ""
    return f"{role or 'staff'}:{who}"

