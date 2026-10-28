"""Bound panel lists to the current integration owner and project secret-free metadata."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.integration_models import Integration, IntegrationCredential, IntegrationPlan
from app.db.models import Plan
from app.services.integration_credentials import CredentialView
from app.services.integration_management_forms import ID_PATTERN, SELECTOR_PATTERN
from app.services.integration_owners import IntegrationOwner, management_owner
from app.services.integration_policy import IntegrationError, SCOPE_FEATURES, stored_scopes, utc_time
from app.services.integrations import IntegrationView, eligible_plans_query, integration_view, managed_integration
from app.services.authz import can_shop

MANAGEMENT_PAGE_SIZE = 25
STATUS_LABELS = {"active": "فعال", "disabled": "غیرفعال", "retired": "بازنشسته", "revoked": "باطل‌شده"}
ACTION_LABELS = {
    "create": "ایجاد یکپارچه‌سازی", "configure": "تغییر وضعیت و دسترسی‌ها", "plans": "تغییر پلن‌های مجاز",
    "issue": "صدور اعتبارنامه", "rotate": "جایگزینی اعتبارنامه", "revoke": "ابطال اعتبارنامه",
}
SCOPE_LABELS = {
    "capabilities:read": "مشاهده قابلیت‌ها", "plans:read": "مشاهده پلن‌ها", "customers:read": "مشاهده مشتریان",
    "customers:write": "ثبت مشتریان", "services:read": "مشاهده سرویس‌ها", "services:provision": "ایجاد سرویس",
    "services:renew": "تمدید سرویس", "services:addons": "افزونه سرویس", "services:access": "دریافت خصوصی اطلاعات اتصال",
    "operations:read": "مشاهده عملیات", "telegram:link": "اتصال تلگرام",
}


@dataclass(frozen=True, slots=True)
class PlanChoice:
    id: int
    name: str


@dataclass(frozen=True, slots=True)
class ManagementDetail:
    integration: IntegrationView
    owner: IntegrationOwner
    selected_plan_ids: tuple[int, ...]
    available_plans: tuple[PlanChoice, ...]
    credentials: tuple[CredentialView, ...]


def grant_choices(owner: IntegrationOwner) -> tuple[str, ...]:
    return tuple(scope for scope, features in SCOPE_FEATURES.items() if all(can_shop(owner.context, key) for key in features))


async def integration_management_list(
    session: AsyncSession, staff: dict, *, after: str = "",
) -> tuple[IntegrationOwner, tuple[IntegrationView, ...]]:
    if after and not ID_PATTERN.fullmatch(after):
        raise IntegrationError("invalid_request")
    owner = await management_owner(session, staff)
    rows = await session.scalars(select(Integration).where(
        Integration.owner_principal_id == owner.principal_id, Integration.scope_kind == owner.shop.kind.value,
        Integration.shop_id == owner.shop.shop_id, Integration.id > after,
    ).order_by(Integration.id).limit(MANAGEMENT_PAGE_SIZE))
    return owner, tuple(integration_view(row) for row in rows)


async def integration_management_detail(
    session: AsyncSession, staff: dict, integration_id: str, *, credential_after: str = "", plan_after: int = 0,
) -> ManagementDetail:
    if credential_after and not SELECTOR_PATTERN.fullmatch(credential_after):
        raise IntegrationError("invalid_request")
    if type(plan_after) is not int or not 0 <= plan_after <= 2**31-1:
        raise IntegrationError("invalid_request")
    row, owner = await managed_integration(session, staff, integration_id)
    selected = await session.scalars(select(IntegrationPlan.plan_id).where(
        IntegrationPlan.integration_id == row.id,
    ).order_by(IntegrationPlan.plan_id).limit(100))
    available = await session.execute(select(Plan.id, Plan.name).where(
        Plan.id.in_(eligible_plans_query(owner.shop)), Plan.id > plan_after,
    ).order_by(Plan.id).limit(MANAGEMENT_PAGE_SIZE))
    credentials = await session.execute(select(
        IntegrationCredential.selector, IntegrationCredential.integration_id, IntegrationCredential.scopes,
        IntegrationCredential.status, IntegrationCredential.expires_at, IntegrationCredential.rotated_at,
    ).where(IntegrationCredential.integration_id == row.id, IntegrationCredential.selector > credential_after)
        .order_by(IntegrationCredential.selector).limit(MANAGEMENT_PAGE_SIZE))
    return ManagementDetail(
        integration_view(row), owner, tuple(selected), tuple(PlanChoice(*plan) for plan in available),
        tuple(CredentialView(key.selector, key.integration_id, stored_scopes(key.scopes), key.status,
                             utc_time(key.expires_at), utc_time(key.rotated_at) if key.rotated_at else None)
              for key in credentials),
    )
