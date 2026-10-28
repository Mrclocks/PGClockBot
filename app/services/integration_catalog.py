"""Materialize supported technical plans with stable, integration-scoped catalog handles."""

from __future__ import annotations

import json
import asyncio
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.integration_read_models import IntegrationCatalogPlan
from app.db.models import Plan, ResellerSetting, Setting
from app.services.integration_authority import IntegrationAuthority, require_pg_evidence
from app.services.integration_observations import MAX_DURATION_SECONDS, MAX_PUBLIC_BYTES, strict_integer
from app.services.integration_pagination import CursorBinding, IntegrationCursors, Keyset, page_limit
from app.services.integration_pg import integration_pg_client, principal_pg_staff
from app.services.pg_quota import hwid_bounds, merge_role_limits
from app.services.plans_catalog import groups_allowed_for_staff
from app.services.integration_policy import IntegrationError
from app.services.integration_rate_limits import conflict_insert
from app.services.integration_request_keys import canonical_digest
from app.services.integration_service_reads import ReadAccess, read_authority
from app.services.integrations import eligible_plans_query
from app.services.orders import resolve_username_naming
from app.services.pasarguard import parse_group_ids
from app.services.pg_read_resilience import web_read_budget

NAMING_KEYS = ("pg_username_prefix", "pg_username_suffix", "pg_username_pattern", "pg_username_vars")
GROUP_PATTERN = re.compile(r"[0-9]+(?: *, *[0-9]+)*", re.ASCII)
NAME_PART = re.compile(r"[A-Za-z0-9._-]{0,32}", re.ASCII)
SUPPORTED_NAMING = frozenset({"{prefix}_{random}{suffix}", "{prefix}{random}{suffix}"})


@dataclass(frozen=True, slots=True)
class TechnicalTerms:
    data_limit_bytes: int | None
    duration_seconds: int | None
    activation: str
    device_limit: int | None
    terms_revision: str


@dataclass(frozen=True, slots=True)
class PlanView:
    id: str
    name: str
    technical_terms: TechnicalTerms
    can_provision: bool
    can_renew: bool


@dataclass(frozen=True, slots=True)
class PlanPage:
    items: tuple[PlanView, ...]
    next_cursor: str | None
    has_more: bool


@dataclass(frozen=True, slots=True)
class CatalogSelection:
    cursor: str = ""
    limit: int | str = 25


@dataclass(frozen=True, slots=True)
class ResolvedPlan:
    local_id: int
    name: str
    data_limit_bytes: int | None
    duration_seconds: int | None
    activation: str
    device_limit: int | None
    group_ids: tuple[int, ...]
    username_prefix: str
    username_suffix: str
    username_pattern: str

    def revision(self) -> str:
        data = asdict(self)
        data.pop("name")
        return canonical_digest(data)

    def public_terms(self) -> TechnicalTerms:
        return TechnicalTerms(self.data_limit_bytes, self.duration_seconds, self.activation, self.device_limit, self.revision())


def _plan_bytes(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise IntegrationError("unsupported_terms")
    try:
        amount = Decimal(str(value)) * 1024**3
        if not amount.is_finite() or amount < 0 or amount > MAX_PUBLIC_BYTES or amount != amount.to_integral_value():
            raise ValueError
        return int(amount) or None
    except (InvalidOperation, ValueError):
        raise IntegrationError("unsupported_terms") from None


def materialize_plan(plan: Plan, settings: dict[str, str], available_groups: frozenset[int],
                     *, device_limit: int | None = None) -> ResolvedPlan:
    # API v1 initially accepts explicit custom-create terms only. A mutable template
    # cannot be invoked without a complete reproducible snapshot of its semantics.
    if plan.pg_template_id is not None or not isinstance(plan.pg_group_ids, str) or not GROUP_PATTERN.fullmatch(plan.pg_group_ids):
        raise IntegrationError("unsupported_terms")
    groups = tuple(sorted(set(parse_group_ids(plan.pg_group_ids))))
    if not groups or len(groups) > 100 or any(not 0 < value <= 2**31-1 for value in groups) or not set(groups) <= available_groups:
        raise IntegrationError("unsupported_terms")
    days = strict_integer(plan.duration_days, maximum=MAX_DURATION_SECONDS // 86400)
    prefix, suffix, pattern = resolve_username_naming(settings, plan_prefix=plan.pg_username_prefix,
        plan_suffix=plan.pg_username_suffix, plan_pattern=plan.pg_username_pattern)
    if (not NAME_PART.fullmatch(prefix) or not NAME_PART.fullmatch(suffix) or pattern not in SUPPORTED_NAMING
            or len(prefix) + len(suffix) + 25 > 64):
        raise IntegrationError("unsupported_terms")
    return ResolvedPlan(plan.id, plan.name, _plan_bytes(plan.data_limit_gb), days * 86400 or None,
                        "immediate", device_limit, groups, prefix, suffix, pattern)


def _eligible_query(authority: IntegrationAuthority) -> Select[tuple[Plan]]:
    return select(Plan).where(Plan.id.in_(authority.allowed_plan_ids), Plan.id.in_(eligible_plans_query(authority.shop)))


def _can_use_plan(authority: IntegrationAuthority, scope: str) -> bool:
    if scope not in authority.local_scopes:
        return False
    try:
        require_pg_evidence(authority, frozenset({scope}), authority.pg_evidence)
        return True
    except IntegrationError:
        return False


async def list_integration_plans(session: AsyncSession, access: ReadAccess, selection: CatalogSelection,
                                 cursors: IntegrationCursors) -> PlanPage:
    limit = page_limit(selection.limit)
    authority = await read_authority(session, access, frozenset({"plans:read"}))
    binding = CursorBinding(authority.integration_id, authority.shop.shop_id, None, "plans")
    query = _eligible_query(authority)
    if selection.cursor:
        key = cursors.decode(selection.cursor, binding)
    else:
        high = await session.scalar(query.with_only_columns(Plan.id).order_by(Plan.id.desc()).limit(1))
        key = Keyset((high or 0) + 1, high or 0)
    plans = list(await session.scalars(query.where(Plan.id <= key.high, Plan.id < key.last)
        .order_by(Plan.id.desc()).limit(limit + 1)))
    if authority.shop.shop_id is None:
        naming_query = select(Setting.key, Setting.value).where(Setting.key.in_(NAMING_KEYS))
    else:
        naming_query = select(ResellerSetting.key, ResellerSetting.value).where(
            ResellerSetting.reseller_user_id == authority.shop.shop_id, ResellerSetting.key.in_(NAMING_KEYS),
        )
    naming = dict((await session.execute(naming_query)).all())
    # Detach selected definitions before ending the read transaction and waiting on PG.
    for plan in plans:
        session.expunge(plan)
    client = await integration_pg_client(session, authority)
    await session.rollback()
    with web_read_budget(seconds=3):
        groups, admin = await asyncio.gather(client.get_groups_simple(), client.request("GET", "/api/admin", use_cache=False))
    if not isinstance(admin, dict) or admin.get("username") != authority.owner.context.pg_admin_username:
        raise IntegrationError("authorization_unavailable")
    staff = principal_pg_staff(authority, admin)
    minimum_devices, maximum_devices = hwid_bounds(merge_role_limits(admin, admin.get("role")))
    device_limit = maximum_devices or minimum_devices
    if device_limit is not None:
        device_limit = strict_integer(device_limit, minimum=1, maximum=1000)
    if not isinstance(groups, list) or len(groups) > 5000:
        raise IntegrationError("authorization_unavailable")
    group_ids = frozenset(row["id"] for row in groups if isinstance(row, dict) and type(row.get("id")) is int and row["id"] > 0)
    resolved = []
    for plan in plans[:limit]:
        try:
            item = materialize_plan(plan, naming, group_ids, device_limit=device_limit)
            if staff.get("pg_access", {}).get("require_template") or not groups_allowed_for_staff(staff, list(item.group_ids)):
                continue
            resolved.append(item)
        except IntegrationError:
            continue
    authority = await read_authority(session, access, frozenset({"plans:read"}))
    current = set(await session.scalars(_eligible_query(authority).with_only_columns(Plan.id).where(
        Plan.id.in_([item.local_id for item in resolved]),
    )))
    resolved = [item for item in resolved if item.local_id in current]
    if resolved:
        await session.execute(conflict_insert(session, IntegrationCatalogPlan.__table__).values([
            {"integration_id": authority.integration_id, "plan_id": item.local_id, "public_id": f"pln_{uuid4()}",
             "terms_json": json.dumps(asdict(item), sort_keys=True), "terms_revision": item.revision(),
             "resolved_at": datetime.now(timezone.utc)} for item in resolved
        ]).on_conflict_do_update(index_elements=["integration_id", "plan_id"], set_={
            "terms_json": conflict_insert(session, IntegrationCatalogPlan.__table__).excluded.terms_json,
            "terms_revision": conflict_insert(session, IntegrationCatalogPlan.__table__).excluded.terms_revision,
            "resolved_at": datetime.now(timezone.utc),
        }))
        await session.commit()
    mappings = dict((await session.execute(select(IntegrationCatalogPlan.plan_id, IntegrationCatalogPlan.public_id).where(
        IntegrationCatalogPlan.integration_id == authority.integration_id,
        IntegrationCatalogPlan.plan_id.in_([item.local_id for item in resolved]),
    ))).all())
    more = len(plans) > limit
    cursor = cursors.encode(binding, Keyset(plans[limit-1].id, key.high)) if more else None
    return PlanPage(tuple(PlanView(mappings[item.local_id], item.name, item.public_terms(),
        _can_use_plan(authority, "services:provision"), _can_use_plan(authority, "services:renew")) for item in resolved), cursor, more)
