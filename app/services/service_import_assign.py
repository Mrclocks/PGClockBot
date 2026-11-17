"""Staff attach a customer's existing panel service from the customer's edit screen.

The customer's own flow (sending the link to the bot) is unchanged. Here a staff member with
``service_import_assign`` enters the subscription link or the panel username on the customer's
edit screen, and the service goes through the same checks: the panel user must belong to a
panel admin this shop may attach, it must not be attached to any other customer (also a partial
unique index), and nothing is written to the panel. On purpose, unlike the customer's flow:

* it works whether or not customers may add services themselves, and needs no approval, since
  the staff member is the one approving;
* the per-customer cap and the customer's failed-try budget do not apply; staff lookups spend
  the shop's own lookup budget instead;
* staff see why a link is refused, but never which customer or which admin owns a service.

Attaching takes two steps. The preview names the panel user; the attach checks everything again
against the panel and refuses if the panel user is not the one previewed, so staff attach exactly
what they confirmed. The staff log records who attached what, and why.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, ServiceImportRequest
from app.services.authz import authz_from_staff, can_shop
from app.services.bot_user_admin import may_manage_customer_services
from app.services.customer_action_notices import NoticeTarget, log_and_notify
from app.services.formatting import format_bytes, format_expire_short, on_hold_expire_duration_seconds, status_label_plain
from app.services.pasarguard import PasarGuardError
from app.services.service_import import (
    MAX_LINK_LENGTH,
    REMARK_IMPORTED,
    ImportOutcome,
    ImportResult,
    Reason,
    ServiceImportError,
    VerifiedService,
    attach_verified,
    existing_rows,
    is_not_found_error,
    verify_link,
    verify_panel_username,
)
from app.services.service_import_admin import REASON_LABELS, lookup_budget_left
from app.services.service_import_config import ImportSettings, read_settings
from app.services.service_import_store import STATUS_APPROVED, STATUS_PENDING
from app.services.shop_membership import assert_customer_in_staff_scope, is_shop_customer
from app.services.shop_scope import resolve_shop_scope_id
from app.services.staff_audit import StaffActor, actor_from_staff
from app.services.users import get_all_settings

logger = logging.getLogger(__name__)

ASSIGN_PERMISSION = "service_import_assign"
STAFF_ACTION = "service_import_assign"
KIND_LINK = "link"
KIND_USERNAME = "username"
LOOKUP_KINDS = frozenset({KIND_LINK, KIND_USERNAME})
_PANEL_USERNAME = re.compile(r"^[A-Za-z0-9_.@-]{3,128}$")
DECIDED_BY_MAX = 64

NO_PERMISSION = "دسترسی افزودن سرویس قبلی به مشتری را ندارید."
BAD_KIND = "نوع ورودی را انتخاب کنید: لینک اشتراک یا نام کاربری پنل."
BAD_LINK = REASON_LABELS[Reason.INVALID_LINK.value]
BAD_USERNAME = "نام کاربری پنل معتبر نیست (۳ تا ۱۲۸ نویسه؛ حروف انگلیسی، عدد و - _ @ .)."
BAD_PREVIEW = "اول سرویس را بررسی کنید و بعد افزودن را بزنید."
CHANGED = "سرویس پنل با موردی که بررسی شد فرق دارد. دوباره بررسی کنید."
TOO_MANY_LOOKUPS = "بررسی‌های زیادی پشت سر هم انجام شد. یک دقیقه بعد دوباره تلاش کنید."
UNKNOWN_REFUSAL = "این سرویس قابل افزودن نیست."
STAFF_REASON_LABELS: dict[str, str] = {
    **REASON_LABELS,
    Reason.WRONG_SHOP.value: "این کاربر مشتری این فروشگاه نیست.",
    Reason.BLOCKED.value: "این مشتری مسدود است؛ اول مسدودی را بردارید.",
    Reason.ALREADY_TAKEN.value: "این سرویس به مشتری دیگری وصل است.",
}


class AssignRefused(ValueError):
    """A refusal whose Persian message is safe to show the staff member."""


@dataclass(frozen=True, slots=True)
class ServiceLookup:
    """What staff entered: a subscription link (or bare token), or a panel username."""

    kind: str
    value: str


@dataclass(frozen=True, slots=True)
class AssignRequest:
    """An attach as staff confirmed it: the lookup, the previewed panel user, the reason, the notice choice."""

    lookup: ServiceLookup
    expected_panel_user_id: int
    reason: str
    notify_customer: bool = False


@dataclass(frozen=True, slots=True)
class ServicePreview:
    panel_user_id: int
    panel_username: str
    status: str
    used: str
    limit: str
    expire: str
    already_yours: bool

    def as_json(self) -> dict[str, Any]:
        return {
            "ok": True,
            "panel_user_id": self.panel_user_id,
            "panel_username": self.panel_username,
            "status": self.status,
            "used": self.used,
            "limit": self.limit,
            "expire": self.expire,
            "already_yours": self.already_yours,
        }


@dataclass(frozen=True, slots=True)
class AssignResult:
    outcome: ImportOutcome
    service_id: int
    panel_username: str
    shop_id: int | None

    @property
    def is_new(self) -> bool:
        return self.outcome in {ImportOutcome.ADDED, ImportOutcome.UPGRADED}


def may_assign_existing_services(staff: Mapping[str, Any] | None) -> bool:
    """Whether staff may attach existing panel services; they must also be able to see services."""
    return can_shop(authz_from_staff(staff), ASSIGN_PERMISSION) and may_manage_customer_services(staff)


def parse_lookup(kind: object, value: object) -> ServiceLookup:
    """Validate the form input before any panel call. Raises ``AssignRefused``."""
    lookup_kind = str(kind or "").strip()
    if lookup_kind not in LOOKUP_KINDS:
        raise AssignRefused(BAD_KIND)
    text = str(value or "").strip()
    if lookup_kind == KIND_LINK:
        if not text or len(text) > MAX_LINK_LENGTH:
            raise AssignRefused(BAD_LINK)
        return ServiceLookup(KIND_LINK, text)
    if not _PANEL_USERNAME.fullmatch(text):
        raise AssignRefused(BAD_USERNAME)
    return ServiceLookup(KIND_USERNAME, text)


def parse_panel_user_id(raw: object) -> int:
    """The previewed panel user id the form sends back. Raises ``AssignRefused``."""
    text = str(raw or "").strip()
    if not text.isascii() or not text.isdigit() or len(text) > 12 or int(text) <= 0:
        raise AssignRefused(BAD_PREVIEW)
    return int(text)


def _refused(reason: Reason) -> AssignRefused:
    return AssignRefused(STAFF_REASON_LABELS.get(reason.value) or UNKNOWN_REFUSAL)


async def _target_shop(session: AsyncSession, staff: Mapping[str, Any], user: BotUser) -> int | None:
    """The shop staff act for, after checking permission, customer scope and customer state.

    Raises ``PermissionError``, ``ShopScopeError`` or ``AssignRefused``.
    """
    if not may_assign_existing_services(staff):
        raise PermissionError(NO_PERMISSION)
    shop_id = resolve_shop_scope_id(dict(staff))
    await assert_customer_in_staff_scope(session, dict(staff), user)
    if not await is_shop_customer(session, user, shop_id):
        raise _refused(Reason.WRONG_SHOP)
    if user.is_blocked:
        raise _refused(Reason.BLOCKED)
    return shop_id


def _visible_reason(lookup: ServiceLookup, reason: Reason) -> Reason:
    """A guessed username under an admin this shop may not attach reads as unknown.

    A link is a secret its holder already has; a username is guessable, and a different answer
    would tell staff which usernames exist under other shops' admins.
    """
    if lookup.kind == KIND_USERNAME and reason is Reason.ADMIN_NOT_ALLOWED:
        return Reason.NOT_FOUND
    return reason


async def _verify(
    session: AsyncSession, lookup: ServiceLookup, shop_id: int | None, config: ImportSettings
) -> VerifiedService:
    """Panel checks for the lookup; only reads reach the panel."""
    if not lookup_budget_left(shop_id):
        raise AssignRefused(TOO_MANY_LOOKUPS)
    try:
        if lookup.kind == KIND_LINK:
            return await verify_link(session, raw_link=lookup.value, shop_id=shop_id, config=config)
        return await verify_panel_username(session, username=lookup.value, shop_id=shop_id, config=config)
    except ServiceImportError as exc:
        raise _refused(_visible_reason(lookup, exc.reason)) from None
    except (PasarGuardError, httpx.HTTPError, TimeoutError) as exc:
        if is_not_found_error(exc):
            raise _refused(Reason.NOT_FOUND) from None
        # No traceback or message: the panel error text carries the request URL, which holds the token.
        logger.warning(
            "service import assign: panel call failed (%s, status=%s)",
            type(exc).__name__, getattr(exc, "status_code", None),
        )
        raise _refused(Reason.PANEL_ERROR) from None


async def _checked_lookup(
    session: AsyncSession, staff: Mapping[str, Any], user: BotUser, lookup: ServiceLookup
) -> tuple[int | None, ImportSettings, VerifiedService]:
    shop_id = await _target_shop(session, staff, user)
    config = read_settings(await get_all_settings(session, reseller_id=shop_id))
    # End the read transaction before the panel calls; the session does not expire on commit.
    await session.commit()
    return shop_id, config, await _verify(session, lookup, shop_id, config)


def _limit_bytes(record: Mapping[str, Any]) -> int:
    try:
        return max(0, int(record.get("data_limit") or 0))
    except (TypeError, ValueError):
        return 0


def _preview(verified: VerifiedService, *, already_yours: bool) -> ServicePreview:
    record = verified.record
    status = record.get("status")
    limit = _limit_bytes(record)
    try:
        used = max(0, int(record.get("used_traffic") or 0))
    except (TypeError, ValueError):
        used = 0
    return ServicePreview(
        panel_user_id=verified.panel_id,
        panel_username=str(record.get("username") or "")[:128],
        status=status_label_plain(status),
        used=format_bytes(used),
        limit=format_bytes(limit) if limit else "نامحدود",
        expire=format_expire_short(
            record.get("expire"), status=status, expire_duration=on_hold_expire_duration_seconds(record)
        ),
        already_yours=already_yours,
    )


async def preview_existing_service(
    session: AsyncSession, *, staff: Mapping[str, Any], user: BotUser, lookup: ServiceLookup
) -> ServicePreview:
    """What the lookup would attach to ``user``, without attaching anything."""
    user_id = int(user.id)
    _shop_id, _config, verified = await _checked_lookup(session, staff, user, lookup)
    rows = await existing_rows(session, verified.panel_id, verified.token)
    if any(int(row.bot_user_id) != user_id for row in rows):
        raise _refused(Reason.ALREADY_TAKEN)
    already_yours = any((row.remark or "").strip() == REMARK_IMPORTED for row in rows)
    return _preview(verified, already_yours=already_yours)


async def assign_existing_service(
    session: AsyncSession,
    *,
    staff: Mapping[str, Any],
    user: BotUser,
    request: AssignRequest,
    actor: StaffActor | None = None,
) -> AssignResult:
    """Attach the previewed panel service to ``user``; safe to repeat.

    A repeat, or a simultaneous second click, returns the same service as ``ALREADY_YOURS`` and
    writes no second log record or customer notice. ``actor`` names the person in the staff log
    (the bot passes its Telegram label); by default it comes from the web session in ``staff``.
    """
    user_id, telegram_id = int(user.id), user.telegram_id
    shop_id, config, verified = await _checked_lookup(session, staff, user, request.lookup)
    if verified.panel_id != int(request.expected_panel_user_id):
        raise AssignRefused(CHANGED)
    try:
        result = await attach_verified(
            session, user, verified, shop_id=shop_id, renewable=True, config=config,
            ask_approval=False, enforce_cap=False,
        )
    except ServiceImportError as exc:
        await session.rollback()
        if exc.reason is Reason.ALREADY_TAKEN:
            mine = await _own_service_id(session, user_id, verified)
            if mine is not None:
                return AssignResult(ImportOutcome.ALREADY_YOURS, mine, _username(verified), shop_id)
        raise _refused(exc.reason) from None
    assigned = _result(result, verified, shop_id)
    if assigned.is_new:
        await _close_open_requests(session, user_id=user_id, panel_user_id=verified.panel_id, staff=staff)
        targets = (
            [NoticeTarget(user_id, int(telegram_id), shop_id, assigned.panel_username, assigned.service_id)]
            if request.notify_customer and telegram_id else []
        )
        await log_and_notify(
            session, _log_actor(actor, staff, shop_id), action=STAFF_ACTION,
            target_type="service", target_id=assigned.service_id, reason=request.reason, targets=targets,
        )
    return assigned


def _log_actor(actor: StaffActor | None, staff: Mapping[str, Any], shop_id: int | None) -> StaffActor:
    """The log record always belongs to the shop resolved here, whoever built ``actor``."""
    if actor is None:
        return actor_from_staff(dict(staff), shop_id=shop_id)
    return actor if actor.shop_id == shop_id else replace(actor, shop_id=shop_id)


def _username(verified: VerifiedService) -> str:
    return str(verified.record.get("username") or "")[:128]


def _result(result: ImportResult, verified: VerifiedService, shop_id: int | None) -> AssignResult:
    if result.service is None:
        # attach_verified only queues a request when asked to; staff attachments never are.
        raise AssignRefused(UNKNOWN_REFUSAL)
    return AssignResult(result.outcome, int(result.service.id), _username(verified), shop_id)


async def _own_service_id(session: AsyncSession, user_id: int, verified: VerifiedService) -> int | None:
    """The customer's own row when the unique index refused a simultaneous duplicate of it."""
    rows = await existing_rows(session, verified.panel_id, verified.token)
    if rows and all(int(row.bot_user_id) == user_id for row in rows):
        return int(rows[0].id)
    return None


async def _close_open_requests(
    session: AsyncSession, *, user_id: int, panel_user_id: int, staff: Mapping[str, Any]
) -> None:
    """Mark the customer's waiting request for this service as done, so it leaves the review queue."""
    who = staff.get("username") or staff.get("bot_user_id") or ""
    await session.execute(
        update(ServiceImportRequest)
        .where(
            ServiceImportRequest.bot_user_id == int(user_id),
            ServiceImportRequest.panel_user_id == int(panel_user_id),
            ServiceImportRequest.status == STATUS_PENDING,
        )
        .values(
            status=STATUS_APPROVED,
            decided_by=f"{staff.get('role') or 'staff'}:{who}"[:DECIDED_BY_MAX],
            decided_at=datetime.now(timezone.utc),
        ),
        execution_options={"synchronize_session": False},
    )
    await session.commit()
