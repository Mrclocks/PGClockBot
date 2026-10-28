"""Pure additive quota arithmetic shared by customer delivery and API targets."""

from __future__ import annotations

from app.services.formatting import is_on_hold_status, parse_expire


def calculate_addon_target(info: dict, *, kind: str, amount_bytes: int | None = None,
                           amount_seconds: int | None = None, now: int, on_hold_expire_ts: int | None = None) -> dict:
    payload = {"status": "active"}
    current_limit = int(info.get("data_limit") or 0)
    if current_limit < 0:
        raise ValueError("حجم سرویس نامعتبر است")
    if kind == "volume":
        if type(amount_bytes) is not int or amount_bytes <= 0 or current_limit <= 0:
            raise ValueError("افزایش حجم برای سرویس نامحدود یا بسته نامعتبر ممکن نیست")
        payload["data_limit"] = current_limit+amount_bytes
        if is_on_hold_status(info.get("status")):
            if on_hold_expire_ts is None:
                raise ValueError("زمان آغاز سرویس مشخص نیست")
            payload["expire"] = on_hold_expire_ts
        return payload
    if kind != "duration" or type(amount_seconds) is not int or amount_seconds <= 0:
        raise ValueError("بسته زمان معتبر نیست")
    current_expire = parse_expire(info.get("expire") or info.get("expire_date"))
    if current_expire is None:
        if not is_on_hold_status(info.get("status")) or on_hold_expire_ts is None:
            raise ValueError("افزایش زمان برای سرویس نامحدود ممکن نیست")
        base = on_hold_expire_ts
    else:
        base = max(now, int(current_expire.timestamp()))
    payload.update(expire=base+amount_seconds, data_limit=current_limit)
    return payload
