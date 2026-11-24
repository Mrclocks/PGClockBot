"""Quick switches of the command center: the few on/off settings staff change most, one tap each.

A tap carries the value it sets (never «flip»), so a double tap or two staff tapping at once end
in the state they asked for. Every change is written to the staff action log in the same commit,
which also gives «آخرین تغییر» and its undo. Settings are always read and written for the given
shop (``None`` = the platform shop), never from the request.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Plan, ShopStaffAction
from app.services.entry_approval_config import PERMISSION as ENTRY_APPROVAL_PERMISSION
from app.services.numbers import to_fa_digits
from app.services.pg_link_alerts import OUTAGE_ACTIVE_KEY
from app.services.staff_audit import StaffActor, add_staff_action
from app.services.users import get_setting, on, set_setting

SWITCH_ON_ACTION = "setting_on"
SWITCH_OFF_ACTION = "setting_off"
SWITCH_TARGET = "setting"
TRIAL_KEY = "trial_enabled"
MAINTENANCE_KEY = "shop_maintenance_enabled"
# A switch left off this long is worth a reminder on the command center.
LONG_OFF_AFTER = timedelta(days=1)
# How many recent switch records are read for «آخرین تغییر» and the reminders.
RECENT_CHANGES_LIMIT = 30
# Every way a customer can pay; when all are off nobody can buy.
PAYMENT_METHOD_KEYS: tuple[str, ...] = (
    "pay_wallet_enabled",
    "pay_card_enabled",
    "pay_gateway_enabled",
    "pay_psp_enabled",
    "pay_bbc_enabled",
    "pay_crypto_enabled",
    "pay_stars_enabled",
)


@dataclass(frozen=True, slots=True)
class QuickSwitch:
    """``code`` travels in callback data; ``permission`` is the extra shop feature a shop needs."""

    code: str
    key: str
    label: str
    permission: str | None = None


_MAINTENANCE = QuickSwitch("mt", MAINTENANCE_KEY, "🛠 حالت تعمیرات")
_FORCE_JOIN = QuickSwitch("fj", "force_join_enabled", "📢 عضویت اجباری")
_ENTRY_APPROVAL = QuickSwitch("ea", "entry_approval_enabled", "✋ تأیید ورود", permission=ENTRY_APPROVAL_PERMISSION)
_PAYMENTS = (
    QuickSwitch("pc", "pay_card_enabled", "💳 کارت به کارت"),
    QuickSwitch("pa", "pay_card_auto_enabled", "🤖 تأیید خودکار کارت"),
    QuickSwitch("pg", "pay_gateway_enabled", "🌐 درگاه"),
    QuickSwitch("pw", "pay_wallet_enabled", "👛 کیف پول"),
    QuickSwitch("pr", "pay_crypto_enabled", "🪙 رمزارز"),
)
OWNER_SWITCHES: tuple[QuickSwitch, ...] = (
    _MAINTENANCE,
    QuickSwitch("tr", TRIAL_KEY, "🧪 تست رایگان"),
    _FORCE_JOIN,
    _ENTRY_APPROVAL,
    *_PAYMENTS,
)
# Shops switch their trial through their trial plan, not a setting.
SHOP_SWITCHES: tuple[QuickSwitch, ...] = (_MAINTENANCE, _FORCE_JOIN, _ENTRY_APPROVAL, *_PAYMENTS)


def switches_for(shop_id: int | None) -> tuple[QuickSwitch, ...]:
    return OWNER_SWITCHES if shop_id is None else SHOP_SWITCHES


def switch_by_code(shop_id: int | None, code: str) -> QuickSwitch | None:
    return next((switch for switch in switches_for(shop_id) if switch.code == code), None)


def _switch_by_key(shop_id: int | None, key: str | None) -> QuickSwitch | None:
    return next((switch for switch in switches_for(shop_id) if switch.key == key), None)


async def _sync_trial_plan(session: AsyncSession, *, enabled: bool) -> None:
    """The platform trial plan is sold only while the trial switch is on."""
    trial = await session.scalar(select(Plan).where(Plan.is_trial.is_(True)).limit(1))
    if trial is not None and trial.is_active != enabled:
        trial.is_active = enabled
        await session.commit()


async def _apply_switch(session: AsyncSession, *, key: str, enabled: bool, shop_id: int | None) -> None:
    """Store one switch value with the side effects its settings save has; commits."""
    await set_setting(session, key, "1" if enabled else "0", reseller_id=shop_id)
    if key == TRIAL_KEY and shop_id is None:
        await _sync_trial_plan(session, enabled=enabled)


async def set_switch(
    session: AsyncSession, *, switch: QuickSwitch, shop_id: int | None, enabled: bool, actor: StaffActor
) -> bool:
    """Set ``switch`` to ``enabled`` for the shop; False when it already had that value.

    ``actor.shop_id`` must be the shop being changed: a mismatch is refused, so one shop's staff
    can never write another shop's settings through this path.
    """
    if actor.shop_id != shop_id or _switch_by_key(shop_id, switch.key) is None:
        raise PermissionError("switch outside the actor's shop")
    if on(await get_setting(session, switch.key, reseller_id=shop_id)) == enabled:
        return False
    # Added before the save, whose commit writes the record together with the value.
    add_staff_action(
        session,
        actor,
        action=SWITCH_ON_ACTION if enabled else SWITCH_OFF_ACTION,
        target_type=SWITCH_TARGET,
        target_id=None,
        reason=switch.key,
    )
    await _apply_switch(session, key=switch.key, enabled=enabled, shop_id=shop_id)
    return True


@dataclass(frozen=True, slots=True)
class SwitchChange:
    record_id: int
    switch: QuickSwitch
    enabled: bool
    actor_label: str
    at: datetime | None


def _shop_clause(shop_id: int | None):
    return ShopStaffAction.shop_id.is_(None) if shop_id is None else ShopStaffAction.shop_id == int(shop_id)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _to_change(shop_id: int | None, row: ShopStaffAction) -> SwitchChange | None:
    switch = _switch_by_key(shop_id, row.reason)
    if switch is None:
        return None
    return SwitchChange(
        record_id=int(row.id),
        switch=switch,
        enabled=row.action == SWITCH_ON_ACTION,
        actor_label=row.actor_label,
        at=_as_utc(row.created_at),
    )


async def recent_switch_changes(session: AsyncSession, *, shop_id: int | None) -> list[SwitchChange]:
    """The shop's latest switch changes, newest first (one bounded query)."""
    rows = await session.scalars(
        select(ShopStaffAction)
        .where(_shop_clause(shop_id), ShopStaffAction.action.in_((SWITCH_ON_ACTION, SWITCH_OFF_ACTION)))
        .order_by(ShopStaffAction.id.desc())
        .limit(RECENT_CHANGES_LIMIT)
    )
    changes = (_to_change(shop_id, row) for row in rows)
    return [change for change in changes if change is not None]


async def undo_switch_change(
    session: AsyncSession,
    *,
    shop_id: int | None,
    record_id: int,
    actor: StaffActor,
    allowed_keys: frozenset[str],
) -> bool:
    """Put back the value a recorded change replaced; False when there is nothing to undo.

    The record is looked up inside the shop, only for a switch the actor may change
    (``allowed_keys``), and only while the switch still holds the value that change set: a later
    change by someone else is never overwritten.
    """
    row = await session.scalar(
        select(ShopStaffAction).where(ShopStaffAction.id == int(record_id), _shop_clause(shop_id))
    )
    change = _to_change(shop_id, row) if row is not None else None
    if change is None or row.action not in (SWITCH_ON_ACTION, SWITCH_OFF_ACTION):
        return False
    if change.switch.key not in allowed_keys:
        return False
    if on(await get_setting(session, change.switch.key, reseller_id=shop_id)) != change.enabled:
        return False
    return await set_switch(session, switch=change.switch, shop_id=shop_id, enabled=not change.enabled, actor=actor)


def _off_since(changes: list[SwitchChange], key: str) -> datetime | None:
    latest = next((change for change in changes if change.switch.key == key), None)
    return latest.at if latest is not None and not latest.enabled else None


def switch_alerts(
    ui: dict[str, str], changes: list[SwitchChange], *, shop_id: int | None, now: datetime
) -> list[str]:
    """Short warnings for states that cost sales, worst first."""
    alerts: list[str] = []
    if shop_id is None and on(ui.get(OUTAGE_ACTIVE_KEY)):
        alerts.append("🔴 اتصال پنل پاسارگارد قطع است.")
    if not any(on(ui.get(key)) for key in PAYMENT_METHOD_KEYS):
        alerts.append("⚠️ هیچ روش پرداختی روشن نیست؛ کسی نمی‌تواند خرید کند.")
    if on(ui.get(MAINTENANCE_KEY)):
        alerts.append("🛠 حالت تعمیرات روشن است؛ خرید جدید بسته است.")
    trial_off_since = _off_since(changes, TRIAL_KEY) if shop_id is None else None
    if trial_off_since is not None and not on(ui.get(TRIAL_KEY)) and now - trial_off_since >= LONG_OFF_AFTER:
        days = max(1, (now - trial_off_since).days)
        alerts.append(f"🧪 تست رایگان {to_fa_digits(days)} روز است خاموش است.")
    return alerts
