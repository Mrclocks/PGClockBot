"""Who a group message goes to: one segment plus optional filters, for the platform or one shop.

A broadcast stores its audience as an ``AudienceSpec`` (JSON on ``broadcast_logs.audience_spec``). The worker turns it
into SQL on every batch with the same reference time (``as_of``), so time windows such as «expires within 7 days» do
not drift while a long broadcast is being sent. Every clause is scoped to one tenant: the platform (``shop_id`` None)
or one shop, using the same membership, block and service rules as the rest of the shop code. Customers who turned
off promotional messages are left out unless the platform marks the message as a service notice; a shop cannot.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

from sqlalchemy import ColumnElement, and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, MarketingPreference, Order, Plan, Role, ShopWallet, UserService
from app.services.broadcast_failures import unreachable_clause
from app.services.formatting import format_number
from app.services.service_scope import shop_services_clause
from app.services.shop_customer_block import not_blocked_in_shop_clause
from app.services.shop_membership import shop_member_clause

DAYS_MIN = 1
DAYS_MAX = 365
# Money is stored in an Integer column; a filter above it can never match and only hides a typo.
WALLET_MAX = 2_000_000_000
LOYAL_MIN_ORDERS = 3
PLAN_CHOICES_LIMIT = 200
CUSTOMER_ROLES = (Role.USER.value, Role.RESELLER.value)
# A synced status that means the service cannot be used, whatever its dates say.
ENDED_STATUSES = ("expired", "limited", "disabled")
# Shop segments that reach most of a shop's customers share the shop's one-a-day broad message slot.
BROAD_SEGMENTS = frozenset({"all", "active"})


@dataclass(frozen=True, slots=True)
class Segment:
    key: str
    label: str
    hint: str
    group: str
    days: int = 0
    platform_only: bool = False
    customers_only: bool = True


STAFF_GROUP = "staff"
# Short labels: they share one segmented bar on a phone.
GROUPS: tuple[tuple[str, str], ...] = (
    ("everyone", "همه"),
    ("service", "سرویس"),
    ("purchase", "سابقه"),
    (STAFF_GROUP, "کادر"),
)
_SEGMENT_LIST: tuple[Segment, ...] = (
    Segment("all", "همه", "همهٔ کاربران ربات", "everyone", customers_only=False),
    Segment("users", "مشتریان", "همه به‌جز ادمین‌ها", "everyone", platform_only=True),
    Segment("active", "سرویس فعال", "حداقل یک سرویس قابل استفاده دارند", "service"),
    Segment("expiring", "نزدیک انقضا", "سرویس فعالشان تا چند روز دیگر تمام می‌شود", "service", days=3),
    Segment("expired", "تازه منقضی‌شده", "سرویسشان در چند روز اخیر تمام شده و تمدید نکرده‌اند", "service", days=7),
    Segment("no_service", "بدون سرویس فعال", "هیچ سرویس قابل استفاده‌ای ندارند", "service"),
    Segment("buyers", "خریداران", "حداقل یک خرید پولی داشته‌اند", "purchase"),
    Segment("loyal", "مشتریان وفادار", f"{LOYAL_MIN_ORDERS} خرید پولی یا بیشتر", "purchase"),
    Segment("never_bought", "بدون خرید", "هنوز خرید پولی نداشته‌اند", "purchase"),
    Segment("trial", "فقط تست", "سرویس تست گرفته‌اند ولی نخریده‌اند", "purchase"),
    Segment("inactive", "خرید نکرده اخیراً", "در چند روز اخیر خریدی نداشته‌اند", "purchase", days=30),
    Segment("new", "عضو جدید", "در چند روز اخیر به ربات آمده‌اند", "purchase", days=7),
    Segment("resellers", "نمایندگان", "فقط نمایندگان", STAFF_GROUP, platform_only=True, customers_only=False),
    Segment("admins", "ادمین‌ها", "فقط ادمین‌ها", STAFF_GROUP, platform_only=True, customers_only=False),
)
SEGMENTS: dict[str, Segment] = {segment.key: segment for segment in _SEGMENT_LIST}
SEGMENT_LABELS: dict[str, str] = {segment.key: segment.label for segment in _SEGMENT_LIST}


class StoredAudienceError(ValueError):
    """A queued broadcast's saved audience cannot be read; it must not be widened to a default audience."""


@dataclass(frozen=True, slots=True)
class AudienceSpec:
    """One audience: a segment, its day window, optional plan and wallet filters, and the notice flag."""

    segment: str = "all"
    days: int = 0
    plan_id: int | None = None
    min_wallet: int | None = None
    notice: bool = False
    as_of: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    label: str = ""

    def to_json(self) -> str:
        return json.dumps(
            {
                "segment": self.segment,
                "days": self.days,
                "plan_id": self.plan_id,
                "min_wallet": self.min_wallet,
                "notice": self.notice,
                "as_of": self.as_of.isoformat(),
                "label": self.label,
            },
            ensure_ascii=False,
        )


def takes_filters(segment: Segment) -> bool:
    """Plan and wallet filters narrow every segment but the staff ones (they have no services or wallet)."""
    return segment.group != STAFF_GROUP


def segments_for(shop_id: int | None) -> tuple[Segment, ...]:
    """The segments this tenant may target, in display order."""
    return tuple(s for s in _SEGMENT_LIST if shop_id is None or not s.platform_only)


def _int_field(raw: object, *, name: str, low: int, high: int) -> int | None:
    """A whole number in ``[low, high]`` from a form or JSON value; None when blank. Rejects bool, NaN and fractions."""
    if raw is None:
        return None
    if isinstance(raw, bool) or (isinstance(raw, float) and not (math.isfinite(raw) and raw.is_integer())):
        raise ValueError(f"{name} نامعتبر است")
    text = str(int(raw) if isinstance(raw, float) else raw).strip()
    if not text:
        return None
    if not text.isdigit():
        raise ValueError(f"{name} نامعتبر است")
    value = int(text)
    if not low <= value <= high:
        raise ValueError(f"{name} باید بین {low} و {high} باشد")
    return value


def make_spec(
    segment: str,
    *,
    shop_id: int | None,
    days: object = None,
    plan_id: object = None,
    min_wallet: object = None,
    notice: bool = False,
    as_of: datetime | None = None,
) -> AudienceSpec:
    """A validated audience for this tenant; ``ValueError`` with a message staff can act on."""
    chosen = SEGMENTS.get(str(segment or "").strip())
    if chosen is None or (chosen.platform_only and shop_id is not None):
        raise ValueError("گروه مخاطبان نامعتبر است")
    window = 0
    if chosen.days:
        window = _int_field(days, name="تعداد روز", low=DAYS_MIN, high=DAYS_MAX) or chosen.days
    filtered = takes_filters(chosen)
    plan = _int_field(plan_id, name="پلن", low=1, high=2**31 - 1) if filtered else None
    wallet = _int_field(min_wallet, name="موجودی کیف پول", low=1, high=WALLET_MAX) if filtered else None
    if notice and shop_id is not None:
        raise ValueError("فقط مدیر ربات می‌تواند پیام را اطلاعیهٔ سرویس علامت بزند")
    return AudienceSpec(
        segment=chosen.key,
        days=window,
        plan_id=plan,
        min_wallet=wallet,
        notice=bool(notice),
        as_of=as_of or datetime.now(timezone.utc),
    )


def spec_from_form(form: Mapping, *, shop_id: int | None) -> AudienceSpec:
    """The audience fields of the web form (``segment``, ``days``, ``plan_id``, ``min_wallet``, ``notice``)."""
    return make_spec(
        str(form.get("segment") or "all"),
        shop_id=shop_id,
        days=form.get("days"),
        plan_id=form.get("plan_id"),
        min_wallet=form.get("min_wallet"),
        notice=str(form.get("notice") or "") in {"1", "on", "true"},
    )


def legacy_spec(audience: str, *, shop_id: int | None, as_of: datetime | None = None) -> AudienceSpec:
    """A row queued before audiences had filters: same people as then (no promotional opt-out on the platform)."""
    key = audience if audience in SEGMENTS else "all"
    if shop_id is not None:
        key = "all"
    return AudienceSpec(segment=key, notice=shop_id is None, as_of=as_of or datetime.now(timezone.utc))


def load_spec(stored: str | None, audience: str, *, shop_id: int | None) -> AudienceSpec:
    """The audience of a queued broadcast. A row without a spec was queued before specs existed and keeps its legacy
    key; a spec that cannot be read raises ``StoredAudienceError`` instead of falling back to a wider audience."""
    if not stored:
        return legacy_spec(audience, shop_id=shop_id)
    try:
        data = json.loads(stored)
        if not isinstance(data, dict):
            raise StoredAudienceError("saved audience is not an object")
        as_of = datetime.fromisoformat(str(data["as_of"]))
        spec = make_spec(
            str(data.get("segment")),
            shop_id=shop_id,
            days=data.get("days"),
            plan_id=data.get("plan_id"),
            min_wallet=data.get("min_wallet"),
            notice=bool(data.get("notice")) and shop_id is None,
            as_of=as_of if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc),
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise StoredAudienceError("saved audience cannot be read") from exc
    return replace(spec, label=str(data.get("label") or "")[:200])


def _paid_orders(shop_id: int | None):
    return and_(
        Order.user_id == BotUser.id,
        Order.reseller_id == shop_id if shop_id is not None else Order.reseller_id.is_(None),
        Order.status == "delivered",
        Order.amount > 0,
    )


def _usable_service(shop_id: int | None, now: datetime):
    return and_(
        UserService.bot_user_id == BotUser.id,
        shop_services_clause(shop_id),
        UserService.is_cancelled.is_(False),
        UserService.cancellation_pending.is_(False),
        or_(UserService.quota_status.is_(None), UserService.quota_status.not_in(ENDED_STATUSES)),
        or_(UserService.quota_expire_at.is_(None), UserService.quota_expire_at >= now),
    )


def _has_usable(shop_id: int | None, now: datetime) -> ColumnElement[bool]:
    return exists(select(UserService.id).where(_usable_service(shop_id, now)))


def _segment_expiring(spec: AudienceSpec, shop_id: int | None) -> list[ColumnElement[bool]]:
    soon = spec.as_of + timedelta(days=spec.days)
    return [exists(select(UserService.id).where(
        _usable_service(shop_id, spec.as_of), UserService.quota_expire_at.is_not(None), UserService.quota_expire_at <= soon
    ))]


def _segment_expired(spec: AudienceSpec, shop_id: int | None) -> list[ColumnElement[bool]]:
    since = spec.as_of - timedelta(days=spec.days)
    ended = or_(
        and_(UserService.quota_expire_at >= since, UserService.quota_expire_at < spec.as_of),
        and_(UserService.quota_status.in_(ENDED_STATUSES), UserService.quota_synced_at >= since),
    )
    recent = exists(select(UserService.id).where(
        UserService.bot_user_id == BotUser.id, shop_services_clause(shop_id), UserService.is_cancelled.is_(False), ended
    ))
    return [recent, ~_has_usable(shop_id, spec.as_of)]


def _segment_loyal(_spec: AudienceSpec, shop_id: int | None) -> list[ColumnElement[bool]]:
    count = select(func.count(Order.id)).where(_paid_orders(shop_id)).scalar_subquery()
    return [count >= LOYAL_MIN_ORDERS]


def _segment_trial(_spec: AudienceSpec, shop_id: int | None) -> list[ColumnElement[bool]]:
    trial = and_(
        Order.user_id == BotUser.id,
        Order.reseller_id == shop_id if shop_id is not None else Order.reseller_id.is_(None),
        Order.status == "delivered",
        Plan.is_trial.is_(True),
    )
    return [~exists(select(Order.id).where(_paid_orders(shop_id))),
            exists(select(Order.id).join(Plan, Plan.id == Order.plan_id).where(trial))]


def _segment_inactive(spec: AudienceSpec, shop_id: int | None) -> list[ColumnElement[bool]]:
    cutoff = spec.as_of - timedelta(days=spec.days)
    recent = and_(_paid_orders(shop_id), Order.created_at > cutoff)
    return [BotUser.created_at <= cutoff, ~exists(select(Order.id).where(recent))]


_SEGMENT_CLAUSES: dict[str, Callable[[AudienceSpec, int | None], list[ColumnElement[bool]]]] = {
    "all": lambda spec, shop: [],
    "users": lambda spec, shop: [],
    "active": lambda spec, shop: [_has_usable(shop, spec.as_of)],
    "expiring": _segment_expiring,
    "expired": _segment_expired,
    "no_service": lambda spec, shop: [~_has_usable(shop, spec.as_of)],
    "buyers": lambda spec, shop: [exists(select(Order.id).where(_paid_orders(shop)))],
    "loyal": _segment_loyal,
    "never_bought": lambda spec, shop: [~exists(select(Order.id).where(_paid_orders(shop)))],
    "trial": _segment_trial,
    "inactive": _segment_inactive,
    "new": lambda spec, shop: [BotUser.created_at >= spec.as_of - timedelta(days=spec.days)],
    "resellers": lambda spec, shop: [BotUser.role == Role.RESELLER.value],
    "admins": lambda spec, shop: [BotUser.role == Role.ADMIN.value],
}


def _members(shop_id: int | None) -> ColumnElement[bool]:
    """Customers of the tenant: the platform's own users, or a shop's attributed, recorded or buying customers."""
    if shop_id is None:
        return shop_member_clause(None)
    placed = exists(select(Order.id).where(Order.user_id == BotUser.id, Order.reseller_id == int(shop_id)))
    return or_(shop_member_clause(int(shop_id)), placed)


def _opted_out(shop_id: int | None) -> ColumnElement[bool]:
    return exists(select(MarketingPreference.user_id).where(
        MarketingPreference.user_id == BotUser.id,
        MarketingPreference.shop_key == (int(shop_id) if shop_id is not None else 0),
        MarketingPreference.enabled.is_(False),
    ))


def _filters(spec: AudienceSpec, shop_id: int | None) -> list[ColumnElement[bool]]:
    clauses: list[ColumnElement[bool]] = []
    if spec.plan_id is not None:
        clauses.append(exists(select(UserService.id).where(
            UserService.bot_user_id == BotUser.id, shop_services_clause(shop_id),
            UserService.is_cancelled.is_(False), UserService.plan_id == int(spec.plan_id),
        )))
    if spec.min_wallet is not None:
        if shop_id is None:
            clauses.append(BotUser.wallet_balance >= int(spec.min_wallet))
        else:
            clauses.append(exists(select(ShopWallet.id).where(
                ShopWallet.user_id == BotUser.id, ShopWallet.reseller_id == int(shop_id),
                ShopWallet.balance >= int(spec.min_wallet),
            )))
    return clauses


def audience_clause(spec: AudienceSpec, *, shop_id: int | None) -> list[ColumnElement[bool]]:
    """WHERE clauses over ``BotUser`` for the people ``spec`` reaches in this tenant."""
    segment = SEGMENTS[spec.segment]
    clauses = [
        BotUser.is_blocked.is_(False), _members(shop_id), not_blocked_in_shop_clause(shop_id),
        # Someone who blocked this bot or never started it only fails again; their next /start includes them.
        ~unreachable_clause(shop_id),
    ]
    if shop_id is not None or segment.customers_only:
        clauses.append(BotUser.role.in_(CUSTOMER_ROLES))
    if not spec.notice:
        # Staff never opt out of staff messages; a customer's choice holds for every segment.
        clauses.append(or_(BotUser.role.not_in(CUSTOMER_ROLES), ~_opted_out(shop_id)))
    clauses.extend(_SEGMENT_CLAUSES[segment.key](spec, shop_id))
    clauses.extend(_filters(spec, shop_id))
    return clauses


async def count_audience(session: AsyncSession, spec: AudienceSpec, *, shop_id: int | None) -> int:
    query = select(func.count()).select_from(BotUser).where(*audience_clause(spec, shop_id=shop_id))
    return int(await session.scalar(query) or 0)


def _scope_plans(shop_id: int | None) -> ColumnElement[bool]:
    owner = Plan.owner_reseller_id.is_(None) if shop_id is None else Plan.owner_reseller_id == int(shop_id)
    return and_(owner, Plan.is_trial.is_(False))


async def plan_choices(session: AsyncSession, *, shop_id: int | None) -> list[tuple[int, str]]:
    """(id, name) of this tenant's sellable plans, for the plan filter; never another shop's."""
    rows = await session.execute(
        select(Plan.id, Plan.name).where(_scope_plans(shop_id)).order_by(Plan.sort_order, Plan.id).limit(PLAN_CHOICES_LIMIT)
    )
    return [(int(plan_id), str(name)) for plan_id, name in rows.all()]


async def checked_spec(session: AsyncSession, spec: AudienceSpec, *, shop_id: int | None) -> AudienceSpec:
    """``spec`` with its plan checked against this tenant and its summary label filled in."""
    plan_name = ""
    if spec.plan_id is not None:
        plan_name = await session.scalar(select(Plan.name).where(Plan.id == int(spec.plan_id), _scope_plans(shop_id))) or ""
        if not plan_name:
            raise ValueError("پلن انتخاب‌شده متعلق به این فروشگاه نیست")
    return replace(spec, label=describe(spec, plan_name=plan_name))


def describe(spec: AudienceSpec, *, plan_name: str = "") -> str:
    """A short Persian summary of the audience for history rows and confirm cards."""
    parts = [SEGMENT_LABELS.get(spec.segment, spec.segment)]
    if spec.days:
        parts.append(f"{spec.days} روز")
    if spec.plan_id is not None:
        parts.append(f"پلن {plan_name or spec.plan_id}")
    if spec.min_wallet is not None:
        parts.append(f"کیف پول از {format_number(spec.min_wallet)}")
    if spec.notice:
        parts.append("اطلاعیهٔ سرویس")
    return " · ".join(parts)[:200]


def stored_label(stored: str | None, audience: str, *, shop_id: int | None) -> str:
    """The audience summary of a queued broadcast: its saved label, else the segment name."""
    try:
        label = load_spec(stored, audience, shop_id=shop_id).label
    except StoredAudienceError:
        label = ""
    return label or SEGMENT_LABELS.get(audience, audience)


def is_broad(spec: AudienceSpec) -> bool:
    """A shop message to (nearly) all its customers: counted against the shop's daily broad slot."""
    return spec.segment in BROAD_SEGMENTS and spec.plan_id is None and spec.min_wallet is None
