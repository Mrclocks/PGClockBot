"""The action center as one ordered list of rows, for the header modal.

``build_inbox_context`` returns several differently shaped alerts (update, ticket, maintenance, capacity, PAYG risk,
the work queue). The modal shows them all the same way: icon, title, one line of detail and where to act, plus the
key the «حذف» button hides it with. Dismissals were already applied when the context was built.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

TONE_WARN = "warn"
TONE_ERR = "err"
TONE_NEUTRAL = "neutral"
_TONES = frozenset({TONE_WARN, TONE_ERR, TONE_NEUTRAL})

# Icons of the work-queue rows (action_center.build_action_center keys); anything new falls back to the inbox bell.
_ACTION_CENTER_ICONS: dict[str, str] = {
    "pending": "receipt",
    "delivery": "queue",
    "tickets": "ticket",
    "expiring": "time",
    "shop_review": "shop",
    "low_volume": "volume-low",
}


@dataclass(frozen=True, slots=True)
class InboxEntry:
    """One row of the action center. ``dismiss_key`` is empty for a row that cannot be hidden."""

    title: str
    detail: str
    href: str
    icon: str
    tone: str = TONE_WARN
    dismiss_key: str = ""
    entity_id: str = ""


def _update_entry(update: dict[str, Any] | None) -> InboxEntry | None:
    if not (update or {}).get("update_available"):
        return None
    return InboxEntry(
        title=f"نسخه {update.get('remote_version')} منتشر شده",
        detail=f"نسخه فعلی شما {update.get('version')} است.",
        href="/settings?tab=update",
        icon="download",
        dismiss_key="update",
    )


def _ticket_entry(ticket: dict[str, Any] | None) -> InboxEntry | None:
    if not ticket:
        return None
    return InboxEntry(
        title=str(ticket.get("title") or ""),
        detail=str(ticket.get("detail") or ""),
        href=str(ticket.get("href") or "/tickets"),
        icon="ticket",
        dismiss_key="ticket_alert",
        entity_id=str(ticket.get("ticket_id") or ticket.get("id") or ""),
    )


def _shop_entries(ctx: dict[str, Any]) -> list[InboxEntry]:
    entries: list[InboxEntry] = []
    if ctx.get("shop_maintenance"):
        entries.append(InboxEntry(
            title="فروشگاه در حالت تعمیرات است",
            detail="خرید جدید برای کاربران بسته است.",
            href="/finance?tab=orders&settings=payment",
            icon="shop",
            dismiss_key="shop_maintenance",
        ))
    if ctx.get("capacity_warn"):
        entries.append(InboxEntry(
            title="ظرفیت پاسارگارد نزدیک پر شدن است",
            detail="قبل از قفل شدن سهمیه، ظرفیت را افزایش دهید.",
            href="/pg",
            icon="server",
            dismiss_key="capacity_warn",
        ))
    alerts_page = ctx.get("customer_alerts_off")
    if alerts_page:
        entries.append(InboxEntry(
            title="هشدار کمبود سرویس برای مشتریان خاموش است",
            detail="مشتری پیش از تمام شدن زمان یا حجم سرویسش پیامی نمی‌گیرد.",
            href=str(alerts_page),
            icon="alert",
            dismiss_key="customer_alerts_off",
        ))
    return entries


def _names(rows: list[dict[str, Any]], limit: int = 3) -> str:
    names = "، ".join(str(row.get("label") or "") for row in rows[:limit])
    return names + ("…" if len(rows) > limit else "")


def _payg_entries(risk: dict[str, Any]) -> list[InboxEntry]:
    entries: list[InboxEntry] = []
    suspended = list(risk.get("suspended") or [])
    if suspended:
        entries.append(InboxEntry(
            title=f"{len(suspended)} نماینده PAYG مسدود",
            detail=_names(suspended),
            href="/resellers",
            icon="payg",
            tone=TONE_ERR,
            dismiss_key="payg_suspended",
        ))
    review = int(risk.get("review") or 0)
    if review:
        entries.append(InboxEntry(
            title=f"{review} هزینهٔ PAYG در انتظار بازبینی",
            detail="پنل پاسخ روشن نداد؛ اعمال شدن یا نشدن تغییر را مشخص کنید",
            href="/payg/charges/review",
            icon="payg",
        ))
    low = list(risk.get("low") or [])
    if low:
        entries.append(InboxEntry(
            title=f"{len(low)} نماینده PAYG با موجودی پایین",
            detail=_names(low),
            href="/resellers",
            icon="wallet",
            dismiss_key="payg_low",
        ))
    return entries


def _action_center_entries(action_center: dict[str, Any]) -> list[InboxEntry]:
    entries: list[InboxEntry] = []
    for item in action_center.get("entries") or []:
        key = str(item.get("key") or "")
        tone = str(item.get("tone") or TONE_NEUTRAL)
        entries.append(InboxEntry(
            title=str(item.get("title") or ""),
            detail=str(item.get("detail") or ""),
            href=str(item.get("href") or "/inbox"),
            icon=_ACTION_CENTER_ICONS.get(key, "inbox"),
            tone=tone if tone in _TONES else TONE_NEUTRAL,
            dismiss_key=f"ac:{key}" if key else "",
        ))
    return entries


def inbox_entries(ctx: dict[str, Any]) -> list[InboxEntry]:
    """Visible action-center rows in the order the inbox page shows them."""
    entries = [entry for entry in (_update_entry(ctx.get("update")), _ticket_entry(ctx.get("ticket_alert"))) if entry]
    entries += _shop_entries(ctx)
    entries += _payg_entries(ctx.get("payg_risk") or {})
    entries += _action_center_entries(ctx.get("action_center") or {})
    return entries
