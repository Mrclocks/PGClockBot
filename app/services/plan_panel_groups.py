"""Sale plans that let PasarGuard pick the groups of a reseller's users.

A reseller whose PasarGuard role shows no groups can still sell: PasarGuard
attaches the groups the panel owner gave that admin when a user is created
without ``group_ids``. Such a plan stores no groups and sets
``Plan.pg_panel_groups``. The mode is offered only when the role was read
successfully and is known to show no groups; delivery then checks that the
created user really received a group before handing it to the customer.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Plan
from app.services.pasarguard import user_group_ids
from app.services.plans_catalog import (
    load_pg_plan_options,
    plan_groups_denial,
    role_requires_template,
)
from app.services.shop_scope import is_platform_admin, shop_owner_id

PICK_A_GROUP_MESSAGE = "حداقل یک گروه پاسارگارد انتخاب کنید"
PANEL_GROUPS_LABEL = "گروه‌های پیش‌فرض پنل"
NO_PANEL_GROUP_MESSAGE = (
    "پاسارگارد هیچ گروهی به کاربر جدید نداد؛ سرویس تحویل نشد. "
    "از مدیر پنل بخواهید به حساب شما گروه بدهد یا پلن را با تمپلیت بسازید."
)


@dataclass(slots=True, frozen=True)
class PlanGroupTarget:
    """Where a group-mode plan gets its PasarGuard groups from."""

    group_csv: str | None
    panel_groups: bool


def staff_may_use_panel_groups(staff: dict | None) -> bool:
    """Static part of the rule: a shop's own PasarGuard admin that may create users without a template."""
    if not staff or is_platform_admin(staff) or shop_owner_id(staff) is None:
        return False
    return not role_requires_template(staff)


def panel_groups_offered(
    staff: dict | None,
    *,
    visible_groups: Sequence[Any] | None,
    pg_error: str | None,
) -> bool:
    """True when the plan form may save a group-mode plan with no group selected.

    ``visible_groups`` must be the staff-filtered list of a read that succeeded:
    an empty list during an outage says nothing about the role.
    """
    if pg_error or visible_groups:
        return False
    return staff_may_use_panel_groups(staff)


async def resolve_plan_group_target(
    session: AsyncSession,
    staff: dict | None,
    selected_ids: Sequence[int],
) -> PlanGroupTarget:
    """Validate a group-mode plan write and say which groups it stores.

    Raises ``ValueError`` with a safe Persian message when the selection is
    not allowed. An empty selection is accepted only after a fresh read of the
    role confirms it shows no groups.
    """
    ids = [int(i) for i in selected_ids]
    if ids:
        denial = plan_groups_denial(staff, ids)
        if denial:
            raise ValueError(denial)
        return PlanGroupTarget(group_csv=",".join(str(i) for i in ids), panel_groups=False)
    if not staff_may_use_panel_groups(staff):
        raise ValueError(PICK_A_GROUP_MESSAGE)
    _templates, groups, pg_error = await load_pg_plan_options(staff, session=session)
    if pg_error:
        raise ValueError(pg_error)
    if groups:
        raise ValueError(PICK_A_GROUP_MESSAGE)
    return PlanGroupTarget(group_csv=None, panel_groups=True)


def apply_plan_group_target(plan: Plan, target: PlanGroupTarget) -> None:
    plan.pg_template_id = None
    plan.pg_group_ids = target.group_csv
    plan.pg_panel_groups = target.panel_groups


def plan_uses_panel_groups(plan: Plan | Any) -> bool:
    """True for a group-mode plan whose groups PasarGuard assigns."""
    return bool(getattr(plan, "pg_panel_groups", False)) and not getattr(plan, "pg_template_id", None)


def plan_has_pg_link(plan: Plan | Any) -> bool:
    """True when a plan can be delivered: a template, chosen groups, or panel groups."""
    if getattr(plan, "pg_template_id", None):
        return True
    if (getattr(plan, "pg_group_ids", None) or "").strip():
        return True
    return plan_uses_panel_groups(plan)


def assert_panel_assigned_group(pg_user: dict | None) -> None:
    """Refuse a user PasarGuard created without any group: it would carry no inbound."""
    if not user_group_ids(pg_user):
        raise ValueError(NO_PANEL_GROUP_MESSAGE)
