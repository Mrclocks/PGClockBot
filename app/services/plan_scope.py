"""Which of a shop's plans a customer feature applies to: all plans, or a chosen list.

A scope is stored as one setting value: ``all``, or the chosen plan ids separated by commas
(empty = no plan). Saving keeps only plans of the saving shop's own catalog, so a shop can
never list another shop's plan; checking compares a service's plan id with the stored list.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Plan
from app.services.numbers import to_fa_digits

ALL = "all"
SCOPE_LIMIT = 200


@dataclass(frozen=True, slots=True)
class PlanScope:
    all_plans: bool
    plan_ids: frozenset[int]

    def allows(self, plan_id: int | None) -> bool:
        if self.all_plans:
            return True
        return plan_id is not None and int(plan_id) in self.plan_ids


@dataclass(frozen=True, slots=True)
class PlanChoice:
    plan_id: int
    name: str


def parse_plan_scope(raw: str | None) -> PlanScope:
    """``all`` (also the default for a value never saved), or the listed ids; junk ids are dropped."""
    text = (raw if raw is not None else ALL).strip().lower()
    if text == ALL:
        return PlanScope(True, frozenset())
    ids = {int(part) for part in text.split(",") if part.strip().isdigit()}
    return PlanScope(False, frozenset(ids))


def serialize_plan_scope(scope: PlanScope) -> str:
    return ALL if scope.all_plans else ",".join(str(i) for i in sorted(scope.plan_ids))


def _owner_clause(shop_id: int | None):
    return Plan.owner_reseller_id.is_(None) if shop_id is None else Plan.owner_reseller_id == int(shop_id)


async def plan_choices(session: AsyncSession, shop_id: int | None) -> list[PlanChoice]:
    """The shop's own sellable plans (not trials), in catalog order."""
    rows = await session.execute(
        select(Plan.id, Plan.name).where(_owner_clause(shop_id), Plan.is_trial.is_(False))
        .order_by(Plan.sort_order, Plan.id).limit(SCOPE_LIMIT)
    )
    return [PlanChoice(int(pid), str(name or f"#{pid}")) for pid, name in rows.all()]


async def normalize_plan_scope(session: AsyncSession, selected: Iterable[str], *, shop_id: int | None) -> str:
    """The value to store for checked boxes ``selected``; ids outside the shop's catalog are dropped."""
    values = {str(v).strip().lower() for v in selected}
    if ALL in values:
        return ALL
    wanted = {int(v) for v in values if v.isdigit()}
    own = {choice.plan_id for choice in await plan_choices(session, shop_id)}
    return serialize_plan_scope(PlanScope(False, frozenset(wanted & own)))


async def normalize_plan_scopes(
    session: AsyncSession, submitted: Mapping[str, Iterable[str]], *, shop_id: int | None
) -> dict[str, str]:
    """Stored values for every submitted plan-scope field (key -> its checked boxes)."""
    return {key: await normalize_plan_scope(session, values, shop_id=shop_id) for key, values in submitted.items()}


def toggle_plan_scope(raw: str | None, choice: str, own_plan_ids: Iterable[int]) -> str:
    """The value after tapping ``choice`` (``all`` or a plan id) in the bot's plan picker."""
    scope, own = parse_plan_scope(raw), set(own_plan_ids)
    if choice == ALL:
        return serialize_plan_scope(PlanScope(not scope.all_plans, frozenset()))
    if not choice.isdigit() or int(choice) not in own:
        raise ValueError("پلن نامعتبر است")
    current = set(own) if scope.all_plans else set(scope.plan_ids) & own
    current ^= {int(choice)}
    return serialize_plan_scope(PlanScope(False, frozenset(current)))


def scope_summary(raw: str | None, choices: list[PlanChoice] | None = None) -> str:
    """A short Persian label for the field button: «همه» or the number of plans."""
    scope = parse_plan_scope(raw)
    if scope.all_plans:
        return "همهٔ پلن‌ها"
    ids = scope.plan_ids if choices is None else scope.plan_ids & {c.plan_id for c in choices}
    count = len(ids)
    return "هیچ پلنی" if count == 0 else f"{to_fa_digits(count)} پلن"
