"""Manage integrations and local plan allowlists within immutable owner/shop scope."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import Select, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.integration_models import Integration, IntegrationPlan
from app.db.models import Plan
from app.services.authz import can_shop
from app.services.integration_transactions import finish_integration_write
from app.services.integration_owners import IntegrationOwner, management_owner
from app.services.integration_policy import IntegrationError, IntegrationShop, SCOPE_FEATURES, ScopeKind, stored_scopes, validate_scopes

MAX_ALLOWED_PLANS = 100
MAX_PLAN_DURATION_DAYS = 3650
MAX_PLAN_DATA_GB = (2**53 - 1) / 1024**3


@dataclass(frozen=True, slots=True)
class IntegrationView:
    id: str
    owner_principal_id: int
    shop: IntegrationShop
    status: str
    grants: frozenset[str]
    customer_entry_policy: str = "denied"


def integration_shop(row: Integration) -> IntegrationShop:
    try:
        return IntegrationShop(ScopeKind(row.scope_kind), row.shop_id)
    except ValueError:
        raise IntegrationError("permission_denied") from None


def validate_owner_grants(owner: IntegrationOwner, grants: frozenset[str]) -> None:
    for scope in validate_scopes(grants):
        if not all(can_shop(owner.context, key) for key in SCOPE_FEATURES[scope]):
            raise IntegrationError("permission_denied")


async def managed_integration(
    session: AsyncSession, authenticated_staff: dict, integration_id: str, *, lock: bool = False,
) -> tuple[Integration, IntegrationOwner]:
    owner = await management_owner(session, authenticated_staff)
    condition = (
        Integration.id == integration_id,
        Integration.owner_principal_id == owner.principal_id,
        Integration.scope_kind == owner.shop.kind.value,
        Integration.shop_id == owner.shop.shop_id,
    )
    if lock:
        result = await session.execute(
            update(Integration).where(*condition).values(revision=Integration.revision + 1)
        )
        if result.rowcount != 1:
            raise IntegrationError("not_found")
    row = await session.scalar(select(Integration).where(*condition).execution_options(populate_existing=True))
    if row is None:
        raise IntegrationError("not_found")
    return row, owner


def integration_view(row: Integration) -> IntegrationView:
    return IntegrationView(row.id, row.owner_principal_id, integration_shop(row), row.status, stored_scopes(row.grants), row.customer_entry_policy)


async def create_integration(
    session: AsyncSession, authenticated_staff: dict, *, grants: frozenset[str] = frozenset(), commit: bool = True,
) -> IntegrationView:
    owner = await management_owner(session, authenticated_staff)
    validate_owner_grants(owner, grants)
    row = Integration(
        id=f"intg_{uuid4()}", owner_principal_id=owner.principal_id,
        scope_kind=owner.shop.kind.value, shop_id=owner.shop.shop_id, grants=",".join(sorted(grants)),
    )
    session.add(row)
    await finish_integration_write(session, commit=commit)
    return integration_view(row)


async def configure_integration(
    session: AsyncSession, authenticated_staff: dict, integration_id: str, *,
    status: str, grants: frozenset[str], customer_entry_policy: str | None = None, commit: bool = True,
) -> IntegrationView:
    if status not in {"active", "disabled", "retired"}:
        raise IntegrationError("invalid_request")
    if customer_entry_policy is not None and (
        not isinstance(customer_entry_policy, str) or customer_entry_policy not in {"denied", "website"}
    ):
        raise IntegrationError("invalid_request")
    row, owner = await managed_integration(session, authenticated_staff, integration_id, lock=True)
    validate_owner_grants(owner, grants)
    if row.status == "retired" and status != "retired":
        raise IntegrationError("integration_disabled")
    row.status, row.grants = status, ",".join(sorted(grants))
    if customer_entry_policy is not None:
        row.customer_entry_policy = customer_entry_policy
    await finish_integration_write(session, commit=commit)
    return integration_view(row)


def eligible_plans_query(shop: IntegrationShop) -> Select[tuple[int]]:
    return select(Plan.id).where(
        Plan.owner_reseller_id == shop.shop_id, Plan.is_active.is_(True), Plan.is_trial.is_(False),
        Plan.duration_days.between(0, MAX_PLAN_DURATION_DAYS),
        Plan.data_limit_gb.is_(None) | Plan.data_limit_gb.between(0, MAX_PLAN_DATA_GB),
        Plan.pg_template_id.is_(None) | (Plan.pg_template_id > 0),
    )


async def set_integration_plans(
    session: AsyncSession, authenticated_staff: dict, integration_id: str, *, plan_ids: frozenset[int], commit: bool = True,
) -> None:
    if not isinstance(plan_ids, frozenset) or len(plan_ids) > MAX_ALLOWED_PLANS:
        raise IntegrationError("invalid_request")
    if any(type(plan_id) is not int or not 0 < plan_id <= 2**31 - 1 for plan_id in plan_ids):
        raise IntegrationError("invalid_request")
    row, owner = await managed_integration(session, authenticated_staff, integration_id, lock=True)
    allowed = set(await session.scalars(eligible_plans_query(owner.shop).where(Plan.id.in_(plan_ids)).limit(MAX_ALLOWED_PLANS)))
    if allowed != plan_ids:
        raise IntegrationError("plan_ineligible")
    await session.execute(delete(IntegrationPlan).where(IntegrationPlan.integration_id == row.id))
    session.add_all(IntegrationPlan(integration_id=row.id, plan_id=plan_id) for plan_id in sorted(allowed))
    await finish_integration_write(session, commit=commit)


async def allowed_integration_plans(session: AsyncSession, row: Integration) -> frozenset[int]:
    ids = await session.scalars(
        eligible_plans_query(integration_shop(row)).join(IntegrationPlan, IntegrationPlan.plan_id == Plan.id)
        .where(IntegrationPlan.integration_id == row.id).limit(MAX_ALLOWED_PLANS)
    )
    return frozenset(ids)
