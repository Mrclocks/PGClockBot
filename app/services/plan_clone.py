"""Copy a sales plan, inside the same catalog."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Plan,
)
from app.services import bot_feedback_texts as feedback
from app.services.renewal_carry import CARRY_TIME, CARRY_VOLUME, plan_carries, set_plan_carry


async def clone_plan(session: AsyncSession, plan_id: int, *, owner_reseller_id: int | None) -> Plan:
    src = (
        await session.execute(select(Plan).where(Plan.id == int(plan_id)))
    ).scalar_one_or_none()
    if not src:
        raise ValueError("پلن پیدا نشد")
    if owner_reseller_id is None:
        if src.owner_reseller_id is not None:
            raise ValueError(feedback.NO_ACCESS)
    else:
        if int(src.owner_reseller_id or 0) != int(owner_reseller_id):
            raise ValueError(feedback.NO_ACCESS)
    copy = Plan(
        name=f"{src.name} (کپی)",
        description=src.description,
        price=int(src.price or 0),
        duration_days=int(src.duration_days or 30),
        data_limit_gb=src.data_limit_gb,
        pg_template_id=src.pg_template_id,
        pg_group_ids=src.pg_group_ids,
        pg_username_prefix=src.pg_username_prefix,
        pg_username_suffix=src.pg_username_suffix,
        pg_username_pattern=src.pg_username_pattern,
        owner_reseller_id=src.owner_reseller_id,
        is_active=False,
        is_trial=bool(src.is_trial),
        sort_order=int(src.sort_order or 0),
    )
    set_plan_carry(copy, volume=plan_carries(src, CARRY_VOLUME), time=plan_carries(src, CARRY_TIME))
    session.add(copy)
    await session.commit()
    await session.refresh(copy)
    return copy
