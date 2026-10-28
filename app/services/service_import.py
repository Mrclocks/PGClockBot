"""Add a service created outside the bot to a customer's «سرویس‌های من».

A subscription link is a bearer secret that people share, so possession of it is not
enough. Before a service is attached the panel user behind the link must

* be reached through a subscription path the Owner allowed (the panel's own path by default);
* belong to a panel admin the shop may attach: the shop's own admin, or for the platform the
  admin the bot is signed in as plus the admins the Owner listed (never a shop's admin), checked
  against the authenticated panel record;
* not be attached to any other customer, which is also enforced by a partial unique
  index so two simultaneous claims cannot both win.

Customers get one generic message for every refusal that could reveal who owns what; the exact
reason is kept for the operator (attempt log and the settings tab). Failed tries are limited per
customer and logged with a hash of the token only. Nothing is written to the panel.

An imported service is renewable and can buy add-on packs like any other service, but it
was not sold by the bot: it can never be cancelled for a refund, and removing it from the
list only detaches it, never deleting the panel user.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.customer_identity import optional_legacy_customer
from app.config import get_settings
from app.db.models import BotUser, ResellerProfile, ServiceImportAttempt, UserService
from app.py_compat import StrEnum
from app.services.pasarguard import (
    PasarGuardError,
    absolutize_subscription_url,
    extract_sub_token,
    get_pg,
    get_pg_for_reseller,
    subscription_path_from_url,
    user_subscription_url,
)
from app.services.pg_user_scope import pg_user_owner_username
from app.services.service_import_config import (
    KEY_ENABLED,
    ImportSettings,
    allowed_paths,
    read_settings,
)
from app.services.service_import_store import TooManyRequests, open_request
from app.services.shop_membership import ensure_member, is_shop_customer
from app.services.users import get_all_settings

logger = logging.getLogger(__name__)

REMARK_IMPORTED = "imported"
REMARK_LINKED = "linked"
SETTING_KEY = KEY_ENABLED
PENDING = "pending"

MAX_LINK_LENGTH = 2048
MAX_FAILED_ATTEMPTS = 5
ATTEMPT_WINDOW = timedelta(hours=1)
ATTEMPT_RETENTION = timedelta(days=7)
IDENTITY_TTL_SECONDS = 120.0
_BARE_TOKEN = re.compile(r"^[A-Za-z0-9._~-]{8,1024}$")
_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_URL_TRAILING = ".,;:!?)»،"

GENERIC_REFUSAL = "این لینک قابل افزودن نیست. لینک اشتراک را بررسی کنید یا با پشتیبانی تماس بگیرید."
INVALID_LINK = "لینک اشتراک معتبر نیست. لینک را کامل و بدون متن اضافه بفرستید."
RATE_LIMITED = "تعداد تلاش‌های ناموفق زیاد بود. کمی بعد دوباره تلاش کنید."
PANEL_UNAVAILABLE = "اتصال به پنل برقرار نشد. کمی بعد دوباره تلاش کنید."
REQUEST_LIMIT = "چند درخواست شما در انتظار تأیید است. بعد از بررسی دوباره تلاش کنید."
LIMIT_REACHED = "به سقف تعداد سرویس‌های قابل افزودن رسیده‌اید."
PENDING_NOTICE = "درخواست شما ثبت شد. بعد از تأیید ادمین سرویس به لیست شما اضافه می‌شود و پیام می‌گیرید."
NOT_ENABLED = "افزودن سرویس قبلی در این فروشگاه فعال نیست."


class ImportOutcome(StrEnum):
    ADDED = "added"
    UPGRADED = "upgraded"
    ALREADY_YOURS = "already_yours"
    PENDING_APPROVAL = "pending_approval"


class Reason(StrEnum):
    """Why a try was refused; shown to the operator, never to the customer."""

    INVALID_LINK = "invalid_link"
    PATH_NOT_ALLOWED = "path_not_allowed"
    NOT_FOUND = "not_found"
    ADMIN_NOT_ALLOWED = "admin_not_allowed"
    ALREADY_TAKEN = "already_taken"
    WRONG_SHOP = "wrong_shop"
    BLOCKED = "blocked"
    LIMIT_REACHED = "limit_reached"
    REQUEST_LIMIT = "request_limit"
    RATE_LIMITED = "rate_limited"
    PANEL_ERROR = "panel_error"
    IDENTITY_UNKNOWN = "identity_unknown"


@dataclass(frozen=True, slots=True)
class ParsedLink:
    prefix: str | None  # subscription path of the link; ``None`` for a bare token
    token: str


@dataclass(frozen=True, slots=True)
class VerifiedService:
    record: dict[str, Any]
    token: str
    prefix: str
    panel_id: int


@dataclass(frozen=True, slots=True)
class ImportResult:
    outcome: ImportOutcome
    service: UserService | None  # ``None`` while the request waits for an admin
    info: dict[str, Any]
    request_id: int | None = None  # the waiting request, when the outcome is PENDING_APPROVAL
    new_request: bool = False


class ServiceImportError(ValueError):
    """Refusal with a customer-safe Persian message and the operator-facing reason."""

    def __init__(self, message: str, reason: Reason = Reason.NOT_FOUND, *, detail: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.detail = detail or {}


def is_external_service(service: UserService | None) -> bool:
    """Attached from outside the bot (view-only link or imported): no cancellation, never delete the panel user."""
    return bool(service) and (service.remark or "").strip() in {REMARK_LINKED, REMARK_IMPORTED}


def admin_delete_pg_flag(service: UserService, *, explicit: bool) -> bool:
    """Whether an operator's delete also removes the panel user.

    A service sold by the bot always does. A linked or imported service was created elsewhere, so
    its panel user is removed only when the operator asked for that explicitly.
    """
    return explicit if is_external_service(service) else True


def parse_subscription_link(raw: str | None) -> ParsedLink | None:
    """Prefix and token of a subscription link (found anywhere in the message) or of a bare token."""
    text = (raw or "").strip()
    if not text or len(text) > MAX_LINK_LENGTH:
        return None
    found = _URL_IN_TEXT.search(text)
    candidate = found.group(0).rstrip(_URL_TRAILING) if found else text
    if "/" in candidate or "://" in candidate:
        token = extract_sub_token(candidate)
        prefix = subscription_path_from_url(candidate)
        if token and prefix and _BARE_TOKEN.match(token):
            return ParsedLink(prefix, token)
        return None
    return ParsedLink(None, candidate) if _BARE_TOKEN.match(candidate) else None


def parse_subscription_token(raw: str | None) -> str | None:
    """Token from a subscription link or a bare token; ``None`` for anything else."""
    link = parse_subscription_link(raw)
    return link.token if link else None


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def _record_attempt(
    session: AsyncSession, user_id: int, token: str, outcome: str, reason: str | None = None
) -> int:
    """Persist one try (own commit, so a refused claim still counts), prune old rows, return its id."""
    now = datetime.now(timezone.utc)
    row = ServiceImportAttempt(
        bot_user_id=int(user_id), token_hash=_token_hash(token), outcome=outcome, reason=reason
    )
    session.add(row)
    await session.flush()
    attempt_id = int(row.id)
    await session.execute(
        delete(ServiceImportAttempt).where(
            ServiceImportAttempt.bot_user_id == int(user_id),
            ServiceImportAttempt.created_at < now - ATTEMPT_RETENTION,
        ),
        execution_options={"synchronize_session": False},
    )
    await session.commit()
    return attempt_id


async def _finish_attempt(
    session: AsyncSession, attempt_id: int, outcome: str, reason: str | None = None
) -> None:
    await session.execute(
        update(ServiceImportAttempt)
        .where(ServiceImportAttempt.id == int(attempt_id))
        .values(outcome=outcome, reason=reason),
        execution_options={"synchronize_session": False},
    )
    await session.commit()


async def _discard_attempt(session: AsyncSession, attempt_id: int) -> None:
    await session.execute(
        delete(ServiceImportAttempt).where(ServiceImportAttempt.id == int(attempt_id)),
        execution_options={"synchronize_session": False},
    )
    await session.commit()


async def _failed_attempts(session: AsyncSession, user_id: int) -> int:
    """Tries in the window that did not succeed; an unfinished («pending») try counts as failed."""
    since = datetime.now(timezone.utc) - ATTEMPT_WINDOW
    ok = {ImportOutcome.ADDED.value, ImportOutcome.UPGRADED.value, ImportOutcome.ALREADY_YOURS.value, ImportOutcome.PENDING_APPROVAL.value, "error"}
    return int(
        await session.scalar(
            select(func.count()).select_from(ServiceImportAttempt).where(
                ServiceImportAttempt.bot_user_id == int(user_id),
                ServiceImportAttempt.created_at >= since,
                ServiceImportAttempt.outcome.not_in(ok),
            )
        )
        or 0
    )


_identity_cache: dict[str, tuple[float, str]] = {}


def clear_identity_cache() -> None:
    _identity_cache.clear()


async def own_admin_identity(pg: Any, *, refresh: bool = False) -> str:
    """Username of the panel admin ``pg`` is signed in as, for password and API-key logins alike.

    Asked from the panel (``GET /api/admin``) instead of trusting a setting. A failure is not
    cached, so the next try asks again.
    """
    key = str(getattr(pg, "base_url", "") or "")
    hit = _identity_cache.get(key)
    now = time.monotonic()
    if hit and not refresh and now - hit[0] < IDENTITY_TTL_SECONDS:
        return hit[1]
    try:
        admin = await pg.get_current_admin()
    except (PasarGuardError, httpx.HTTPError, TimeoutError):
        logger.warning("service import: could not read the panel admin identity")
        return ""
    name = str((admin or {}).get("username") or "").strip().lower()
    if name:
        _identity_cache[key] = (now, name)
    return name


async def panel_client(session: AsyncSession, shop_id: int | None) -> Any:
    return get_pg() if shop_id is None else await get_pg_for_reseller(session, int(shop_id))


async def allowed_admins(
    session: AsyncSession, pg: Any, shop_id: int | None, config: ImportSettings, *, refresh: bool = False
) -> frozenset[str]:
    """Panel admins whose users may be attached.

    A shop: its own admin only. The platform: the admin the bot is signed in as, the admin in
    ``PG_USERNAME`` and the admins the Owner listed, minus every shop's admin (their customers
    belong to the shop, not to the platform bot).
    """
    if shop_id is not None:
        profile = await session.scalar(select(ResellerProfile).where(ResellerProfile.user_id == int(shop_id)))
        name = ((profile.pg_admin_username if profile else "") or "").strip().lower()
        return frozenset({name} if name else ())
    names = {
        await own_admin_identity(pg, refresh=refresh),
        (get_settings().pg_username or "").strip().lower(),
        *config.extra_admins,
    }
    names.discard("")
    shop_admins = await session.scalars(
        select(ResellerProfile.pg_admin_username).where(ResellerProfile.pg_admin_username.is_not(None)).limit(5000)
    )
    return frozenset(names - {str(a).strip().lower() for a in shop_admins.all() if a})


def _is_not_found(exc: Exception) -> bool:
    return getattr(exc, "status_code", None) in {401, 403, 404}


async def _subscription_info(pg: Any, link: ParsedLink, paths: tuple[str, ...]) -> tuple[dict[str, Any], str]:
    """Info of the user behind ``link`` and the subscription path that answered.

    A link carries its path and only an allowed path is asked; a bare token is tried on every
    allowed path. Only «not found» moves on to the next path.
    """
    if link.prefix is not None and link.prefix not in paths:
        raise ServiceImportError(GENERIC_REFUSAL, Reason.PATH_NOT_ALLOWED)
    for prefix in ([link.prefix] if link.prefix else list(paths)):
        try:
            info = await pg.subscription_info(link.token, subscription_url=f"{prefix}/{link.token}")
        except (PasarGuardError, httpx.HTTPError, TimeoutError) as exc:
            if _is_not_found(exc):
                continue
            raise
        return info, prefix
    raise ServiceImportError(GENERIC_REFUSAL, Reason.NOT_FOUND)


async def _panel_record(pg: Any, info: dict[str, Any]) -> dict[str, Any]:
    """Authenticated panel record of the user named by the subscription info."""
    username = str((info or {}).get("username") or "").strip()
    if not username:
        raise ServiceImportError(GENERIC_REFUSAL, Reason.NOT_FOUND)
    record = await pg.get_user_by_username(username)
    if not isinstance(record, dict) or str(record.get("username") or "").strip().lower() != username.lower():
        raise ServiceImportError(GENERIC_REFUSAL, Reason.NOT_FOUND)
    return record


def panel_user_id(record: dict[str, Any]) -> int:
    value = record.get("id")
    if type(value) is not int or value <= 0:
        raise ServiceImportError(GENERIC_REFUSAL, Reason.NOT_FOUND)
    return value


async def verify_link(
    session: AsyncSession, *, raw_link: str, shop_id: int | None, config: ImportSettings, refresh: bool = False
) -> VerifiedService:
    """Everything about the link and the panel user, short of the customer: raise a refusal or return it."""
    link = parse_subscription_link(raw_link)
    if link is None:
        raise ServiceImportError(INVALID_LINK, Reason.INVALID_LINK)
    pg = await panel_client(session, shop_id)
    admins = await allowed_admins(session, pg, shop_id, config, refresh=refresh)
    if not admins:
        raise ServiceImportError(PANEL_UNAVAILABLE, Reason.IDENTITY_UNKNOWN)
    info, prefix = await _subscription_info(pg, link, allowed_paths(config))
    record = await _panel_record(pg, info)
    owner = pg_user_owner_username(record)
    if owner not in admins:
        raise ServiceImportError(GENERIC_REFUSAL, Reason.ADMIN_NOT_ALLOWED, detail={"owner_admin": owner})
    return VerifiedService(record=record, token=link.token, prefix=prefix, panel_id=panel_user_id(record))


async def existing_rows(session: AsyncSession, pg_user_id: int, token: str) -> list[UserService]:
    rows = await session.scalars(
        select(UserService).where(
            (UserService.pg_user_id == pg_user_id) | (UserService.subscription_token == token)
        ).limit(20)
    )
    return list(rows.all())


async def import_existing_service(
    session: AsyncSession, *, user: BotUser, raw_link: str, shop_id: int | None, renewable: bool = True
) -> ImportResult:
    """Attach the panel service behind ``raw_link`` to ``user`` or raise ``ServiceImportError``.

    ``renewable=False`` keeps the older view-only attachment (used by the deep link while
    importing is switched off); both go through the same checks.
    """
    user_id = int(user.id)  # a rollback below expires ``user``; keep the id
    link = parse_subscription_link(raw_link)
    if link is None:
        await _record_attempt(session, user_id, raw_link[:64] or "-", "invalid", Reason.INVALID_LINK.value)
        raise ServiceImportError(INVALID_LINK, Reason.INVALID_LINK)
    # The try is stored as «pending» before anything else, so simultaneous messages from one
    # customer see each other and cannot all pass the failed-attempt limit.
    attempt_id = await _record_attempt(session, user_id, link.token, PENDING)
    if await _failed_attempts(session, user_id) > MAX_FAILED_ATTEMPTS:
        await _discard_attempt(session, attempt_id)
        raise ServiceImportError(RATE_LIMITED, Reason.RATE_LIMITED)
    try:
        config = read_settings(await get_all_settings(session, reseller_id=shop_id))
        result = await _attach(session, user, raw_link, shop_id, renewable, config)
    except ServiceImportError as exc:
        await session.rollback()
        await _finish_attempt(session, attempt_id, "refused", exc.reason.value)
        raise
    except (PasarGuardError, httpx.HTTPError, TimeoutError) as exc:
        await session.rollback()
        if _is_not_found(exc):
            # An unknown or out-of-scope link is a failed guess and counts against the budget.
            await _finish_attempt(session, attempt_id, "refused", Reason.NOT_FOUND.value)
            raise ServiceImportError(GENERIC_REFUSAL, Reason.NOT_FOUND) from None
        # No traceback or message: the panel error text carries the request URL, which holds the token.
        logger.warning(
            "service import: panel call failed (%s, status=%s)", type(exc).__name__, getattr(exc, "status_code", None)
        )
        await _finish_attempt(session, attempt_id, "error", Reason.PANEL_ERROR.value)
        raise ServiceImportError(PANEL_UNAVAILABLE, Reason.PANEL_ERROR) from None
    await _finish_attempt(session, attempt_id, result.outcome.value)
    return result


async def _attach(
    session: AsyncSession, user: BotUser, raw_link: str, shop_id: int | None, renewable: bool, config: ImportSettings
) -> ImportResult:
    if not await is_shop_customer(session, user, shop_id):
        raise ServiceImportError(GENERIC_REFUSAL, Reason.WRONG_SHOP)
    if user.is_blocked:
        raise ServiceImportError(GENERIC_REFUSAL, Reason.BLOCKED)
    verified = await verify_link(session, raw_link=raw_link, shop_id=shop_id, config=config)
    return await attach_verified(
        session, user, verified, shop_id=shop_id, renewable=renewable, config=config, ask_approval=True
    )


async def attach_verified(
    session: AsyncSession,
    user: BotUser,
    verified: VerifiedService,
    *,
    shop_id: int | None,
    renewable: bool,
    config: ImportSettings,
    ask_approval: bool,
) -> ImportResult:
    """Attach a verified panel user to ``user``; with approval on, queue a request instead."""
    # Serialise this customer's imports (no effect on SQLite) so the per-user cap below holds.
    await session.scalar(select(BotUser.id).where(BotUser.id == user.id).with_for_update())
    mine: UserService | None = None
    for row in await existing_rows(session, verified.panel_id, verified.token):
        if row.bot_user_id != user.id:
            raise ServiceImportError(GENERIC_REFUSAL, Reason.ALREADY_TAKEN)
        mine = mine or row
    wanted = REMARK_IMPORTED if renewable else REMARK_LINKED
    if mine is not None:
        if (mine.remark or "").strip() == REMARK_LINKED and renewable:
            mine.remark = REMARK_IMPORTED
            return await _commit(session, mine, verified.record, ImportOutcome.UPGRADED)
        return ImportResult(ImportOutcome.ALREADY_YOURS, mine, verified.record)

    if renewable and await _imported_count(session, user.id) >= config.max_per_user:
        raise ServiceImportError(LIMIT_REACHED, Reason.LIMIT_REACHED)
    if ask_approval and renewable and config.needs_approval:
        return await _queue_request(session, user, verified, shop_id)
    sub_url = user_subscription_url(verified.record) or absolutize_subscription_url(
        f"{verified.prefix}/{verified.token}"
    )
    service = UserService(
        bot_user_id=user.id,
        customer_id=await optional_legacy_customer(session, user.id),
        pg_user_id=verified.panel_id,
        pg_username=str(verified.record.get("username") or "")[:128],
        subscription_url=sub_url,
        subscription_token=verified.token,
        remark=wanted,
        shop_id=shop_id,
    )
    session.add(service)
    result = await _commit(session, service, verified.record, ImportOutcome.ADDED)
    # After the commit: querying earlier would flush the row and turn the unique-link
    # refusal handled by _commit into a raw database error.
    await ensure_member(session, int(user.id), shop_id)
    await session.commit()
    return result


async def _queue_request(
    session: AsyncSession, user: BotUser, verified: VerifiedService, shop_id: int | None
) -> ImportResult:
    try:
        request, created = await open_request(
            session, user_id=int(user.id), shop_id=shop_id, panel_user_id=verified.panel_id,
            panel_username=str(verified.record.get("username") or ""),
        )
    except TooManyRequests:
        raise ServiceImportError(REQUEST_LIMIT, Reason.REQUEST_LIMIT) from None
    request_id = int(request.id)
    result = await _commit(session, None, verified.record, ImportOutcome.PENDING_APPROVAL)
    return ImportResult(result.outcome, None, result.info, request_id=request_id, new_request=created)


async def _commit(session: AsyncSession, service: UserService | None, record: dict[str, Any], outcome: ImportOutcome) -> ImportResult:
    try:
        await session.commit()
    except IntegrityError:
        # A concurrent claim of the same panel user won the unique index.
        raise ServiceImportError(GENERIC_REFUSAL, Reason.ALREADY_TAKEN) from None
    if service is not None:
        await session.refresh(service)
    return ImportResult(outcome, service, record)


async def _imported_count(session: AsyncSession, user_id: int) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(UserService).where(
                UserService.bot_user_id == int(user_id), UserService.remark == REMARK_IMPORTED
            )
        )
        or 0
    )
