"""Recheck accepted intents against current local policy and live own-panel limits."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.integration_models import Integration
from app.db.models import OrgPrincipal, Plan, ResellerSetting, Setting
from app.services.authz import can_shop
from app.services.billing import BillingError, assert_billing_allows_provision
from app.services.integration_authority import IntegrationAuthority, require_local_scopes
from app.services.integration_catalog import NAMING_KEYS, ResolvedPlan, materialize_plan
from app.services.integration_observations import strict_integer
from app.services.integration_operations import KIND_SCOPES
from app.services.integration_owners import load_integration_owner
from app.services.integration_pg import principal_pg_staff
from app.services.integration_policy import IntegrationError, SCOPE_FEATURES, stored_scopes
from app.services.integrations import allowed_integration_plans, eligible_plans_query, integration_shop
from app.services.pg_quota import (PgQuotaError, assert_can_create_user, assert_can_modify_user, hwid_bounds, merge_role_limits)
from app.services.plans_catalog import groups_allowed_for_staff
from app.services.volume_allocation import VolumeAllocationError, assert_volume_delta_allowed


async def accepted_authority(session: AsyncSession, integration_id: str, kind: str, *, execution: bool = True) -> IntegrationAuthority:
    integration = await session.get(Integration, integration_id, populate_existing=True)
    if integration is None:
        raise IntegrationError("not_found")
    owner = await load_integration_owner(session, integration.owner_principal_id)
    shop = integration_shop(integration)
    if owner.shop != shop:
        raise IntegrationError("permission_denied")
    scopes = frozenset(scope for scope in stored_scopes(integration.grants)
        if all(can_shop(owner.context, permission) for permission in SCOPE_FEATURES[scope]))
    authority = IntegrationAuthority(integration.id, "", owner, shop, scopes, await allowed_integration_plans(session, integration))
    require_local_scopes(authority, frozenset({"operations:read"}) if execution else frozenset({"services:read", "operations:read"}))
    if execution:
        enabled = await session.scalar(select(Setting.value).where(Setting.key == "integration_api_enabled"))
        if enabled != "1":
            raise IntegrationError("api_disabled")
        if integration.status != "active":
            raise IntegrationError("integration_disabled")
        require_local_scopes(authority, frozenset({KIND_SCOPES[kind]}))
        if integration.customer_entry_policy != "website":
            raise IntegrationError("website_entry_not_enabled")
        matches = await session.scalar(select(func.count()).select_from(OrgPrincipal).where(
            func.lower(OrgPrincipal.pg_username) == (owner.context.pg_admin_username or "").lower()))
        if matches != 1:
            raise IntegrationError("principal_inactive")
    return authority


async def current_plan(session: AsyncSession, authority: IntegrationAuthority, plan_id: int, admin: dict, groups: object) -> ResolvedPlan:
    plan = await session.scalar(select(Plan).where(Plan.id == plan_id, Plan.id.in_(authority.allowed_plan_ids),
        Plan.id.in_(eligible_plans_query(authority.shop))))
    if plan is None:
        raise IntegrationError("plan_ineligible")
    if not isinstance(groups, list) or len(groups) > 5000:
        raise IntegrationError("authorization_unavailable")
    group_ids = frozenset(row["id"] for row in groups if isinstance(row, dict) and type(row.get("id")) is int and row["id"] > 0)
    query = select(Setting.key, Setting.value).where(Setting.key.in_(NAMING_KEYS)) if authority.shop.shop_id is None else select(
        ResellerSetting.key, ResellerSetting.value).where(ResellerSetting.reseller_user_id == authority.shop.shop_id,
            ResellerSetting.key.in_(NAMING_KEYS))
    naming = dict((await session.execute(query)).all())
    minimum, maximum = hwid_bounds(merge_role_limits(admin, admin.get("role")))
    device_limit = maximum or minimum
    if device_limit is not None:
        device_limit = strict_integer(device_limit, minimum=1, maximum=1000)
    terms = materialize_plan(plan, naming, group_ids, device_limit=device_limit)
    staff = principal_pg_staff(authority, admin)
    if staff.get("pg_access", {}).get("require_template") or not groups_allowed_for_staff(staff, list(terms.group_ids)):
        raise IntegrationError("plan_ineligible")
    return terms


@dataclass(frozen=True, slots=True)
class ObservedQuotaClient:
    """Reuse existing quota arithmetic without any network inside the transaction."""
    admin: dict

    async def get_current_admin(self) -> dict:
        return self.admin

    async def get_admin_role(self, role_id: int) -> dict:
        raise PgQuotaError("حدود نقش در پاسخ معتبر موجود نیست")


@dataclass(frozen=True, slots=True)
class ExecutionLimits:
    authority: IntegrationAuthority
    admin: dict
    payload: dict
    delta_bytes: int | None
    creates_user: bool


async def assert_execution_capacity(session: AsyncSession, limits: ExecutionLimits) -> None:
    authority, admin, payload = limits.authority, limits.admin, limits.payload
    if admin.get("username") != authority.owner.context.pg_admin_username:
        raise IntegrationError("authorization_unavailable")
    if admin.get("status") not in {"active", "disabled", "limited"}:
        raise IntegrationError("authorization_unavailable")
    role_limits = merge_role_limits(admin, admin.get("role"))
    if limits.creates_user and (role_limits.get("max_users") or admin.get("max_users")):
        strict_integer(admin.get("total_users", admin.get("users_count")), maximum=2**31-1)
    staff = principal_pg_staff(authority, admin)
    expire = payload.get("expire")
    if isinstance(expire, str):
        expire = int(datetime.fromisoformat(expire.replace("Z", "+00:00")).timestamp())
    hold = payload.get("on_hold_expire_duration")
    if hold is not None:
        expire = int(datetime.now(timezone.utc).timestamp())+hold
    kwargs: dict[str, Any] = dict(data_limit=payload.get("data_limit"), expire_ts=expire,
        hwid_limit=payload.get("hwid_limit"), session=session, client=ObservedQuotaClient(admin))
    try:
        await assert_billing_allows_provision(session, reseller_user_id=authority.shop.shop_id, suspend=False)
        if limits.creates_user:
            await assert_can_create_user(staff, **kwargs)
        else:
            await assert_can_modify_user(staff, **kwargs, hwid_changed="hwid_limit" in payload)
        await assert_volume_delta_allowed(session, shop_id=authority.shop.shop_id, delta_bytes=limits.delta_bytes,
            audience="admin", lock=True)
    except BillingError:
        raise IntegrationError("billing_blocked") from None
    except (PgQuotaError, VolumeAllocationError):
        raise IntegrationError("quota_exceeded") from None
