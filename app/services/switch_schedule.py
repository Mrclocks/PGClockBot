"""Timed quick switches and scenarios: change switches now, put them back later or by hand.

Each changed switch gets one ``switch_reverts`` row, written in the same commit as the change.
A revert is claimed by a conditional UPDATE and applied in that same commit, and it puts the old
value back only while the switch still holds the value the change set: a later change by a
person always wins, and a revert never runs twice.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SwitchRevert
from app.services.quick_switches import QuickSwitch, set_switch, switch_by_code, switches_for
from app.services.staff_audit import ACTOR_SYSTEM, StaffActor
from app.services.users import get_setting, on

logger = logging.getLogger(__name__)

PENDING = "pending"
DONE = "done"
SKIPPED = "skipped"
CANCELLED = "cancelled"
DISPLAY_ZONE = ZoneInfo("Asia/Tehran")
MORNING = time(8, 0)
DUE_BATCH = 50
SYSTEM_LABEL = "سیستم (زمان‌بندی)"
# Duration code in callback data → minutes, or None for «until 08:00 Tehran».
DURATIONS: dict[str, int | None] = {"1h": 60, "3h": 180, "am": None}


@dataclass(frozen=True, slots=True)
class Scenario:
    code: str
    title: str
    description: str
    # (switch code, value to set); switches a viewer lacks are left out for that viewer.
    actions: tuple[tuple[str, bool], ...]
    # True: put back automatically at 08:00 Tehran; False: only by hand.
    until_morning: bool = False


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        "down",
        "🚨 پنل قطع است",
        "خرید جدید و تست رایگان بسته می‌شوند تا مشتری پولی نپردازد که سرویسش ساخته نشود.",
        (("mt", True), ("tr", False)),
    ),
    Scenario(
        "night",
        "🌙 حالت شب",
        "کارت به کارت دستی تا ساعت ۸ صبح خاموش می‌شود؛ درگاه، کیف پول و کارت خودکار باز می‌مانند.",
        (("pc", False),),
        until_morning=True,
    ),
    Scenario(
        "abuse",
        "🛡 سوءاستفاده",
        "تست رایگان خاموش و تأیید ورود و عضویت اجباری روشن می‌شوند.",
        (("tr", False), ("ea", True), ("fj", True)),
    ),
)


def scenario_by_code(code: str) -> Scenario | None:
    return next((scenario for scenario in SCENARIOS if scenario.code == code), None)


def _shop_key(shop_id: int | None) -> int:
    return 0 if shop_id is None else int(shop_id)


def system_actor(shop_id: int | None) -> StaffActor:
    return StaffActor(shop_id=shop_id, kind=ACTOR_SYSTEM, user_id=None, label=SYSTEM_LABEL)


def next_morning(now: datetime) -> datetime:
    """The next 08:00 in Tehran, as UTC."""
    local = now.astimezone(DISPLAY_ZONE)
    target = datetime.combine(local.date(), MORNING, tzinfo=DISPLAY_ZONE)
    if target <= local:
        target += timedelta(days=1)
    return target.astimezone(timezone.utc)


def revert_time(duration: str, now: datetime) -> datetime:
    if duration not in DURATIONS:
        raise ValueError("unknown duration")
    minutes = DURATIONS[duration]
    return next_morning(now) if minutes is None else now + timedelta(minutes=minutes)


async def _cancel_pending(session: AsyncSession, shop_id: int | None, key: str) -> None:
    await session.execute(
        update(SwitchRevert)
        .where(
            SwitchRevert.shop_id == _shop_key(shop_id),
            SwitchRevert.setting_key == key,
            SwitchRevert.status == PENDING,
        )
        .values(status=CANCELLED)
    )


async def change_for_a_while(
    session: AsyncSession,
    *,
    switch: QuickSwitch,
    shop_id: int | None,
    enabled: bool,
    actor: StaffActor,
    revert_at: datetime | None,
    scenario: str | None = None,
) -> bool:
    """Set ``switch`` now and record when to put the current value back; False when already set.

    An earlier pending revert of the same switch is replaced. Two staff at once: the partial
    unique index lets one pending revert through, the other gets ``ValueError``.
    """
    if on(await get_setting(session, switch.key, reseller_id=shop_id)) == enabled:
        return False
    await _cancel_pending(session, shop_id, switch.key)
    session.add(
        SwitchRevert(
            shop_id=_shop_key(shop_id),
            setting_key=switch.key,
            set_value=enabled,
            revert_value=not enabled,
            revert_at=revert_at,
            scenario=scenario,
            status=PENDING,
        )
    )
    try:
        # The switch save commits the revert row, the audit record and the value together.
        return await set_switch(session, switch=switch, shop_id=shop_id, enabled=enabled, actor=actor)
    except IntegrityError as exc:
        await session.rollback()
        raise ValueError("این کلید همین حالا زمان‌بندی دیگری دارد") from exc


async def run_scenario(
    session: AsyncSession,
    *,
    scenario: Scenario,
    shop_id: int | None,
    allowed: tuple[QuickSwitch, ...],
    actor: StaffActor,
    now: datetime,
) -> list[QuickSwitch]:
    """Apply every action of ``scenario`` the viewer may take; returns the switches it changed."""
    revert_at = next_morning(now) if scenario.until_morning else None
    allowed_codes = {switch.code for switch in allowed}
    changed = []
    for code, enabled in scenario.actions:
        switch = switch_by_code(shop_id, code)
        if switch is None or code not in allowed_codes:
            continue
        if await change_for_a_while(
            session,
            switch=switch,
            shop_id=shop_id,
            enabled=enabled,
            actor=actor,
            revert_at=revert_at,
            scenario=scenario.code,
        ):
            changed.append(switch)
    return changed


async def pending_reverts(session: AsyncSession, *, shop_id: int | None) -> list[SwitchRevert]:
    """The shop's pending reverts, oldest first (bounded by the number of switches)."""
    rows = await session.scalars(
        select(SwitchRevert)
        .where(SwitchRevert.shop_id == _shop_key(shop_id), SwitchRevert.status == PENDING)
        .order_by(SwitchRevert.id)
        .limit(len(switches_for(shop_id)))
    )
    return list(rows)


async def apply_revert(
    session: AsyncSession,
    revert_id: int,
    *,
    shop_id: int | None,
    actor: StaffActor,
    allowed_keys: frozenset[str] | None = None,
) -> bool:
    """Claim one pending revert of ``shop_id`` and put its value back; False when not applied.

    ``allowed_keys`` limits a person to the switches they may change (None: the scheduler).
    The claim and the value are committed together, so a crash leaves the row pending for the
    next run and two runs cannot both apply it.
    """
    row = await session.get(SwitchRevert, int(revert_id))
    if row is None or row.shop_id != _shop_key(shop_id) or row.status != PENDING:
        return False
    if allowed_keys is not None and row.setting_key not in allowed_keys:
        return False
    switch = next((s for s in switches_for(shop_id) if s.key == row.setting_key), None)
    still_set = switch is not None and on(await get_setting(session, row.setting_key, reseller_id=shop_id)) == row.set_value
    claim = await session.execute(
        update(SwitchRevert)
        .where(SwitchRevert.id == row.id, SwitchRevert.status == PENDING)
        .values(status=DONE if still_set else SKIPPED)
    )
    if claim.rowcount != 1:
        await session.rollback()
        return False
    if not still_set:
        await session.commit()
        return False
    await set_switch(session, switch=switch, shop_id=shop_id, enabled=row.revert_value, actor=actor)
    await session.commit()
    return True


async def end_scenario(
    session: AsyncSession, *, shop_id: int | None, scenario: str, actor: StaffActor, allowed_keys: frozenset[str]
) -> int:
    """Put back every pending change of ``scenario`` in the shop; returns how many were put back."""
    rows = [row for row in await pending_reverts(session, shop_id=shop_id) if row.scenario == scenario]
    applied = 0
    for row in rows:
        applied += int(
            await apply_revert(session, row.id, shop_id=shop_id, actor=actor, allowed_keys=allowed_keys)
        )
    return applied


async def run_due_reverts(session_factory: Callable[[], AsyncSession], *, now: datetime) -> int:
    """Put back every switch whose time has come; one failing row is logged and the rest continue."""
    async with session_factory() as session:
        due = (
            await session.execute(
                select(SwitchRevert.id, SwitchRevert.shop_id)
                .where(SwitchRevert.status == PENDING, SwitchRevert.revert_at <= now)
                .order_by(SwitchRevert.revert_at, SwitchRevert.id)
                .limit(DUE_BATCH)
            )
        ).all()
    applied = 0
    for revert_id, shop_key in due:
        shop_id = int(shop_key) or None
        try:
            async with session_factory() as session:
                applied += int(await apply_revert(session, revert_id, shop_id=shop_id, actor=system_actor(shop_id)))
        except (SQLAlchemyError, PermissionError):
            logger.warning("switch revert %s failed", revert_id, exc_info=True)
    return applied
