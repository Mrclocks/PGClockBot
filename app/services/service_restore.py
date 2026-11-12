"""The panel quota a renewal or add-on replaced, saved on its order before the panel write.

A void of that order writes the saved quota back, but only while the panel still shows what the
renewal or add-on wrote: any later change to the service leaves the void for a person.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.services.formatting import hold_duration_from_info, is_on_hold_status, parse_expire

RECORD_VERSION = 1
# A panel may round the expiry it stores; anything further apart is a different expiry.
EXPIRE_TOLERANCE_SECONDS = 2
# The panel moves an active user to limited or expired by itself; all three were written as active.
RUNNING_STATUSES = frozenset({"active", "limited", "expired"})
ACTIVE, DISABLED, ON_HOLD = "active", "disabled", "on_hold"


@dataclass(frozen=True, slots=True)
class PanelQuota:
    """What a write sets on the panel user: status, volume limit (0 = none), expiry and hold time."""

    status: str
    data_limit: int
    expire: int
    hold_seconds: int

    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {"status": self.status, "data_limit": self.data_limit, "expire": self.expire}
        if self.status == ON_HOLD:
            body["on_hold_expire_duration"] = self.hold_seconds
        return body

    def shows(self, expected: PanelQuota) -> bool:
        """True when this live quota is still the one ``expected`` describes (usage aside)."""
        return (
            self.status == expected.status
            and self.data_limit == expected.data_limit
            and abs(self.expire - expected.expire) <= EXPIRE_TOLERANCE_SECONDS
            and self.hold_seconds == expected.hold_seconds
        )


@dataclass(frozen=True, slots=True)
class SavedChange:
    """The quota before and after one renewal or add-on, and the plan the service had before it."""

    service_id: int
    plan_id: int | None
    before: PanelQuota
    after: PanelQuota


def _written_status(raw: object) -> str:
    status = str(raw or "").strip().lower()
    if is_on_hold_status(status):
        return ON_HOLD
    if status == DISABLED:
        return DISABLED
    if status in RUNNING_STATUSES:
        return ACTIVE
    raise ValueError("وضعیت سرویس در پنل ناشناخته است")


def _whole(raw: object) -> int:
    if isinstance(raw, bool):
        raise ValueError("حجم سرویس در پنل نامعتبر است")
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("حجم سرویس در پنل نامعتبر است") from exc


def quota_from_panel(info: Mapping[str, Any]) -> PanelQuota:
    """The quota of a panel user, or of a write payload laid over the user it changes."""
    status = _written_status(info.get("status"))
    expire_at = parse_expire(info["expire"] if "expire" in info else info.get("expire_date"))
    hold = hold_duration_from_info(info) if status == ON_HOLD else None
    return PanelQuota(
        status=status,
        data_limit=_whole(info.get("data_limit")),
        expire=int(expire_at.timestamp()) if expire_at else 0,
        hold_seconds=int(hold or 0),
    )


def encode_change(
    *, service_id: int, plan_id: int | None, before: Mapping[str, Any], payload: Mapping[str, Any]
) -> str:
    """The record to store on the order: ``before`` is the live user, ``payload`` the write about to happen."""
    after = quota_from_panel({**before, **payload})
    return json.dumps({
        "version": RECORD_VERSION,
        "service_id": int(service_id),
        "plan_id": int(plan_id) if plan_id else None,
        "before": _quota_dict(quota_from_panel(before)),
        "after": _quota_dict(after),
    })


def _quota_dict(quota: PanelQuota) -> dict[str, Any]:
    return {
        "status": quota.status,
        "data_limit": quota.data_limit,
        "expire": quota.expire,
        "hold_seconds": quota.hold_seconds,
    }


def _quota(raw: object) -> PanelQuota:
    if not isinstance(raw, Mapping) or raw.get("status") not in (ACTIVE, DISABLED, ON_HOLD):
        raise ValueError("invalid quota")
    values = [raw.get(key) for key in ("data_limit", "expire", "hold_seconds")]
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
        raise ValueError("invalid quota")
    return PanelQuota(str(raw["status"]), *values)


def saved_change(raw: str | None) -> SavedChange | None:
    """The record an order carries, or None for an order delivered without one."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if data.get("version") != RECORD_VERSION:
            return None
        plan_id = data.get("plan_id")
        return SavedChange(
            service_id=int(data["service_id"]),
            plan_id=int(plan_id) if plan_id else None,
            before=_quota(data.get("before")),
            after=_quota(data.get("after")),
        )
    except (TypeError, ValueError, KeyError, AttributeError):
        return None


def _same_user(info: object, pg_user_id: int) -> Mapping[str, Any]:
    if not isinstance(info, Mapping) or int(info.get("id") or 0) != int(pg_user_id):
        raise ValueError("پاسخ پنل با سرویس همخوان نیست")
    return info


async def assert_still_changed(pg: Any, pg_user_id: int, change: SavedChange) -> None:
    """Refuse a void once the service changed again after the renewal or add-on (or was already put back)."""
    live = quota_from_panel(_same_user(await pg.get_user_by_id(int(pg_user_id)), pg_user_id))
    if not live.shows(change.after) and not live.shows(change.before):
        raise ValueError("سرویس بعد از این تمدید یا افزونه تغییر کرده است؛ آن را دستی اصلاح کنید")


async def put_back(pg: Any, pg_user_id: int, change: SavedChange) -> None:
    """Write the saved quota back; a retry that finds it already back writes nothing.

    Raises when the panel shows neither state, or does not confirm the write.
    """
    live = quota_from_panel(_same_user(await pg.get_user_by_id(int(pg_user_id)), pg_user_id))
    if live.shows(change.before):
        return
    if not live.shows(change.after):
        raise ValueError("سرویس بعد از این تمدید یا افزونه تغییر کرده است")
    await pg.modify_user_by_id(int(pg_user_id), change.before.payload())
    live = quota_from_panel(_same_user(await pg.get_user_by_id(int(pg_user_id)), pg_user_id))
    if not live.shows(change.before):
        raise ValueError("بازگشت سرویس در پنل تأیید نشد")
