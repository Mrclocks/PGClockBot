"""Customizable daily Telegram report — role-scoped metrics + safe templates.

Security:
- Every query takes an explicit ``reseller_id`` (``None`` = Owner platform shop).
- Metric allowlists differ by actor; Owner-only keys never resolve for shops.
- Templates use ``render_message_template`` / ``safe_format`` only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.demo_users import non_demo_customer

from app.db.models import BotUser, UserService
from app.services.home_overview import bot_panel_summary, reseller_shop_summary
from app.services.safe_format import safe_format
from app.services.service_scope import shop_services_clause

DOMAIN_DAILY_REPORT = "daily_report"

_TEHRAN = ZoneInfo("Asia/Tehran")

# Actor kinds for metric allowlists.
ACTOR_OWNER = "owner"
ACTOR_SHOP = "shop"  # reseller L1 or principal with shop_settings on that shop


@dataclass(frozen=True)
class DailyMetric:
    key: str
    title_fa: str
    group: str  # header | activity | ops | funnel | totals
    description_fa: str
    example: str
    actors: frozenset[str]
    default_on: bool = True


_METRICS: tuple[DailyMetric, ...] = (
    DailyMetric(
        key="admin_name",
        title_fa="نام ادمین/فروشگاه",
        group="header",
        description_fa="نام نمایشی گیرنده گزارش.",
        example="علی",
        actors=frozenset({ACTOR_OWNER, ACTOR_SHOP}),
        default_on=True,
    ),
    DailyMetric(
        key="period_label",
        title_fa="برچسب دوره",
        group="header",
        description_fa="مثلاً «روزانه».",
        example="روزانه",
        actors=frozenset({ACTOR_OWNER, ACTOR_SHOP}),
        default_on=True,
    ),
    DailyMetric(
        key="day",
        title_fa="روز (شمسی)",
        group="header",
        description_fa="شماره روز در ماه شمسی.",
        example="۲۹",
        actors=frozenset({ACTOR_OWNER, ACTOR_SHOP}),
        default_on=True,
    ),
    DailyMetric(
        key="month_name",
        title_fa="نام ماه شمسی",
        group="header",
        description_fa="مثلاً مرداد.",
        example="مرداد",
        actors=frozenset({ACTOR_OWNER, ACTOR_SHOP}),
        default_on=True,
    ),
    DailyMetric(
        key="year",
        title_fa="سال شمسی",
        group="header",
        description_fa="سال هجری شمسی.",
        example="۱۴۰۵",
        actors=frozenset({ACTOR_OWNER, ACTOR_SHOP}),
        default_on=True,
    ),
    DailyMetric(
        key="date_from",
        title_fa="از تاریخ",
        group="header",
        description_fa="شروع بازهٔ گزارش (شمسی).",
        example="۱۴۰۵/۵/۲۹",
        actors=frozenset({ACTOR_OWNER, ACTOR_SHOP}),
        default_on=True,
    ),
    DailyMetric(
        key="date_to",
        title_fa="تا تاریخ",
        group="header",
        description_fa="پایان بازهٔ گزارش (شمسی).",
        example="۱۴۰۵/۵/۳۰",
        actors=frozenset({ACTOR_OWNER, ACTOR_SHOP}),
        default_on=True,
    ),
    DailyMetric(
        key="users_new",
        title_fa="کاربران جدید امروز",
        group="activity",
        description_fa="کاربران ربات که امروز در محدودهٔ فروشگاه ساخته شده‌اند.",
        example="۳",
        actors=frozenset({ACTOR_OWNER, ACTOR_SHOP}),
    ),
    DailyMetric(
        key="orders_new",
        title_fa="سفارش‌های امروز",
        group="activity",
        description_fa="تعداد سفارش ثبت‌شده امروز.",
        example="۵",
        actors=frozenset({ACTOR_OWNER, ACTOR_SHOP}),
    ),
    DailyMetric(
        key="orders_delivered",
        title_fa="تحویل امروز",
        group="activity",
        description_fa="سفارش‌های تحویل‌شده امروز.",
        example="۴",
        actors=frozenset({ACTOR_OWNER, ACTOR_SHOP}),
    ),
    DailyMetric(
        key="revenue_today",
        title_fa="درآمد امروز",
        group="activity",
        description_fa="جمع مبلغ سفارش‌های تحویل‌شده امروز (فرمت پول).",
        example="۱٬۲۰۰٬۰۰۰ تومان",
        actors=frozenset({ACTOR_OWNER, ACTOR_SHOP}),
    ),
    DailyMetric(
        key="services_new",
        title_fa="سرویس جدید امروز",
        group="activity",
        description_fa="سرویس‌های ساخته‌شده امروز در محدودهٔ فروشگاه.",
        example="۴",
        actors=frozenset({ACTOR_OWNER, ACTOR_SHOP}),
    ),
    DailyMetric(
        key="pending",
        title_fa="رسید معلق",
        group="ops",
        description_fa="پرداخت‌های در انتظار بررسی.",
        example="۲",
        actors=frozenset({ACTOR_OWNER, ACTOR_SHOP}),
    ),
    DailyMetric(
        key="tickets_open",
        title_fa="تیکت باز",
        group="ops",
        description_fa="تیکت‌های باز فروشگاه.",
        example="۱",
        actors=frozenset({ACTOR_OWNER, ACTOR_SHOP}),
    ),
    DailyMetric(
        key="failures",
        title_fa="تحویل ناموفق",
        group="ops",
        description_fa="صف تحویل ناموفق.",
        example="۰",
        actors=frozenset({ACTOR_OWNER, ACTOR_SHOP}),
    ),
    DailyMetric(
        key="funnel_pay_start",
        title_fa="شروع پرداخت (امروز)",
        group="funnel",
        description_fa="رویداد رفتار کاربر: شروع پرداخت.",
        example="۸",
        actors=frozenset({ACTOR_OWNER, ACTOR_SHOP}),
        default_on=False,
    ),
    DailyMetric(
        key="funnel_receipt",
        title_fa="رسید (امروز)",
        group="funnel",
        description_fa="رویداد رفتار کاربر: ارسال رسید.",
        example="۶",
        actors=frozenset({ACTOR_OWNER, ACTOR_SHOP}),
        default_on=False,
    ),
    DailyMetric(
        key="funnel_delivered",
        title_fa="تحویل قیف (امروز)",
        group="funnel",
        description_fa="رویداد رفتار کاربر: تحویل.",
        example="۵",
        actors=frozenset({ACTOR_OWNER, ACTOR_SHOP}),
        default_on=False,
    ),
    DailyMetric(
        key="users_total",
        title_fa="کل کاربران",
        group="totals",
        description_fa="تعداد کل کاربران فروشگاه.",
        example="۱۲۰",
        actors=frozenset({ACTOR_OWNER, ACTOR_SHOP}),
    ),
    DailyMetric(
        key="orders_total",
        title_fa="کل سفارش‌ها",
        group="totals",
        description_fa="تعداد کل سفارش‌ها.",
        example="۸۰",
        actors=frozenset({ACTOR_OWNER, ACTOR_SHOP}),
    ),
    DailyMetric(
        key="services_total",
        title_fa="کل سرویس‌ها",
        group="totals",
        description_fa="تعداد کل سرویس‌های فعال/ثبت‌شده.",
        example="۷۵",
        actors=frozenset({ACTOR_OWNER, ACTOR_SHOP}),
    ),
    DailyMetric(
        key="revenue_total",
        title_fa="کل درآمد تحویل‌شده",
        group="totals",
        description_fa="جمع درآمد سفارش‌های تحویل‌شده (فرمت پول).",
        example="۱۲٬۰۰۰٬۰۰۰ تومان",
        actors=frozenset({ACTOR_OWNER, ACTOR_SHOP}),
    ),
    DailyMetric(
        key="resellers_active",
        title_fa="نمایندگان فعال",
        group="totals",
        description_fa="فقط مالک سیستم — تعداد پروفایل نماینده فعال.",
        example="۴",
        actors=frozenset({ACTOR_OWNER}),
        default_on=False,
    ),
)

ALL_METRICS: tuple[DailyMetric, ...] = _METRICS
_BY_KEY = {m.key: m for m in ALL_METRICS}

GROUP_LABELS_FA: dict[str, str] = {
    "header": "سربرگ تاریخ و هویت",
    "activity": "فعالیت امروز",
    "ops": "عملیات باز",
    "funnel": "رفتار کاربر امروز",
    "totals": "آمار کل",
}

DEFAULT_REPORT_TEMPLATE = (
    "📊 <b>گزارش روزانه</b>\n"
    "\n"
    "👤 ادمین: {admin_name}\n"
    "📅 دوره: {period_label}\n"
    "📆 روز: {day}\n"
    "🗓️ ماه: {month_name}\n"
    "📅 سال: {year}\n"
    "📅 از: {date_from}\n"
    "📅 تا: {date_to}\n"
    "\n"
    "📊 <b>فعالیت:</b>\n"
    "\n"
    "• کاربران جدید: {users_new}\n"
    "• سفارش‌ها: {orders_new}\n"
    "• تحویل‌شده: {orders_delivered}\n"
    "• درآمد امروز: {revenue_today}\n"
    "• سرویس جدید: {services_new}\n"
    "\n"
    "🛠 <b>عملیات باز:</b>\n"
    "• رسید معلق: {pending}\n"
    "• تیکت باز: {tickets_open}\n"
    "• تحویل ناموفق: {failures}\n"
    "\n"
    "📈 <b>آمار کل:</b>\n"
    "• کل کاربران: {users_total}\n"
    "• کل سفارش‌ها: {orders_total}\n"
    "• کل سرویس‌ها: {services_total}\n"
    "• کل درآمد: {revenue_total}"
)

_MONTH_FA = (
    "",
    "فروردین",
    "اردیبهشت",
    "خرداد",
    "تیر",
    "مرداد",
    "شهریور",
    "مهر",
    "آبان",
    "آذر",
    "دی",
    "بهمن",
    "اسفند",
)


def gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    """Convert Gregorian Y/M/D to Jalali Y/M/D (no external deps)."""
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    if gy > 1600:
        jy = 979
        gy -= 1600
    else:
        jy = 0
        gy -= 621
    gy2 = gm > 2 and (gy + 1) or gy
    days = (
        365 * gy
        + (gy2 + 3) // 4
        - (gy2 + 99) // 100
        + (gy2 + 399) // 400
        - 80
        + gd
        + g_d_m[gm - 1]
    )
    jy += 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + days // 31
        jd = 1 + (days % 31)
    else:
        jm = 7 + (days - 186) // 30
        jd = 1 + ((days - 186) % 30)
    return int(jy), int(jm), int(jd)


def format_jalali_parts(dt: datetime) -> dict[str, str]:
    local = dt.astimezone(_TEHRAN) if dt.tzinfo else dt.replace(tzinfo=timezone.utc).astimezone(_TEHRAN)
    jy, jm, jd = gregorian_to_jalali(local.year, local.month, local.day)
    return {
        "day": str(jd),
        "month": str(jm),
        "month_name": _MONTH_FA[jm] if 1 <= jm <= 12 else str(jm),
        "year": str(jy),
        "date": f"{jy}/{jm}/{jd}",
    }


def metrics_for_actor(actor: str) -> list[DailyMetric]:
    return [m for m in _METRICS if actor in m.actors]


def default_metric_keys(actor: str) -> list[str]:
    return [m.key for m in metrics_for_actor(actor) if m.default_on]


def parse_metric_keys(raw: str | None, *, actor: str) -> list[str]:
    allowed = {m.key for m in metrics_for_actor(actor)}
    if raw is None or not str(raw).strip():
        return default_metric_keys(actor)
    text = str(raw).strip()
    if text.startswith("["):
        try:
            data = json.loads(text)
            if isinstance(data, list):
                keys = [str(x).strip() for x in data if str(x).strip()]
            else:
                keys = []
        except Exception:
            keys = []
    else:
        keys = [p.strip() for p in text.replace(";", ",").split(",") if p.strip()]
    out = [k for k in keys if k in allowed]
    return out or default_metric_keys(actor)


def serialize_metric_keys(keys: Iterable[str], *, actor: str) -> str:
    allowed = {m.key for m in metrics_for_actor(actor)}
    cleaned = [k for k in keys if k in allowed]
    if not cleaned:
        cleaned = default_metric_keys(actor)
    return ",".join(cleaned)


def metric_groups_for_ui(actor: str) -> list[dict[str, Any]]:
    by_group: dict[str, list[DailyMetric]] = {}
    for m in metrics_for_actor(actor):
        by_group.setdefault(m.group, []).append(m)
    out: list[dict[str, Any]] = []
    for gid, label in GROUP_LABELS_FA.items():
        items = by_group.get(gid) or []
        if not items:
            continue
        out.append(
            {
                "id": gid,
                "label": label,
                "metrics": [
                    {
                        "key": m.key,
                        "title_fa": m.title_fa,
                        "description_fa": m.description_fa,
                        "token": "{" + m.key + "}",
                        "default_on": m.default_on,
                    }
                    for m in items
                ],
            }
        )
    return out


def preview_fill_map(actor: str = ACTOR_OWNER) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in metrics_for_actor(actor):
        out["{" + m.key + "}"] = m.example
    return out


async def _scoped_totals(session: AsyncSession, *, reseller_id: int | None) -> dict[str, int]:
    summary = (
        await bot_panel_summary(session)
        if reseller_id is None
        else await reseller_shop_summary(session, int(reseller_id))
    )
    return {
        "users_total": summary["users"],
        "orders_total": summary["orders"],
        "services_total": summary["services"],
        "revenue_total": summary["revenue"],
        "pending": summary["pending"],
        "tickets_open": summary["tickets"],
        "resellers_active": summary.get("resellers", 0),
    }


async def _today_activity(session: AsyncSession, *, reseller_id: int | None) -> dict[str, int]:
    from app.services.home_overview import shop_period_stats

    periods = await shop_period_stats(session, reseller_id=reseller_id)
    # shop_period_stats keys are day/week/month (not "today").
    today = periods.get("day") or {}
    # Services created today (shop-scoped)
    since = datetime.now(timezone.utc).astimezone(_TEHRAN).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).astimezone(timezone.utc)
    if reseller_id is None:
        svc_q = (
            select(func.count())
            .select_from(UserService)
            .where(UserService.created_at >= since, shop_services_clause(None), non_demo_customer(UserService.bot_user_id))
        )
    else:
        rid = int(reseller_id)
        svc_q = (
            select(func.count())
            .select_from(UserService)
            .where(UserService.created_at >= since, shop_services_clause(rid), non_demo_customer(UserService.bot_user_id))
        )
    services_new = int((await session.execute(svc_q)).scalar() or 0)
    return {
        "users_new": int(today.get("new_users") or 0),
        "orders_new": int(today.get("orders") or 0),
        "orders_delivered": int(today.get("delivered") or 0),
        "revenue_today": int(today.get("revenue") or 0),
        "services_new": services_new,
    }


async def collect_report_values(
    session: AsyncSession,
    *,
    reseller_id: int | None,
    actor: str,
    admin_name: str,
    enabled_keys: list[str] | None = None,
) -> dict[str, Any]:
    """Compute values for enabled keys only (no cross-shop bleed)."""
    from app.services.formatting import format_toman
    from app.config import get_settings
    from app.services.ux20 import build_action_center, funnel_summary

    keys = enabled_keys if enabled_keys is not None else default_metric_keys(actor)
    allowed = {m.key for m in metrics_for_actor(actor)}
    keys = [k for k in keys if k in allowed]

    now = datetime.now(timezone.utc)
    j_from = format_jalali_parts(now)
    j_to = format_jalali_parts(now + timedelta(days=1))

    need_activity = any(
        k in keys for k in ("users_new", "orders_new", "orders_delivered", "revenue_today", "services_new")
    )
    need_totals = any(
        k in keys
        for k in (
            "users_total",
            "orders_total",
            "services_total",
            "revenue_total",
            "pending",
            "tickets_open",
            "resellers_active",
        )
    )
    need_ops_extra = "failures" in keys
    need_funnel = any(k.startswith("funnel_") for k in keys)

    activity = await _today_activity(session, reseller_id=reseller_id) if need_activity else {}
    totals = await _scoped_totals(session, reseller_id=reseller_id) if need_totals else {}

    failures = 0
    if need_ops_extra:
        ac = await build_action_center(session, reseller_id=reseller_id)
        failures = int(ac.get("failures") or 0)
        if "pending" not in totals:
            totals["pending"] = int(ac.get("pending") or 0)
        if "tickets_open" not in totals:
            totals["tickets_open"] = int(ac.get("tickets") or 0)

    funnel: dict[str, Any] = {}
    if need_funnel:
        funnel = await funnel_summary(session, reseller_id=reseller_id, days=1)

    currency = get_settings().currency
    raw: dict[str, Any] = {
        "admin_name": admin_name or "—",
        "period_label": "روزانه",
        "day": j_from["day"],
        "month_name": j_from["month_name"],
        "year": j_from["year"],
        "date_from": j_from["date"],
        "date_to": j_to["date"],
        "users_new": activity.get("users_new", 0),
        "orders_new": activity.get("orders_new", 0),
        "orders_delivered": activity.get("orders_delivered", 0),
        "revenue_today": format_toman(int(activity.get("revenue_today") or 0), currency),
        "services_new": activity.get("services_new", 0),
        "pending": totals.get("pending", 0),
        "tickets_open": totals.get("tickets_open", 0),
        "failures": failures,
        "funnel_pay_start": int(funnel.get("pay_start") or 0),
        "funnel_receipt": int(funnel.get("receipt") or 0),
        "funnel_delivered": int(funnel.get("delivered") or 0),
        "users_total": totals.get("users_total", 0),
        "orders_total": totals.get("orders_total", 0),
        "services_total": totals.get("services_total", 0),
        "revenue_total": format_toman(int(totals.get("revenue_total") or 0), currency),
        "resellers_active": totals.get("resellers_active", 0),
    }
    return {k: raw.get(k, "") for k in keys}


def render_daily_report_template(
    template: str | None,
    values: dict[str, Any],
    *,
    enabled_keys: list[str],
) -> str:
    """Fill template; disabled keys stay as empty string (placeholder removed)."""
    text, _kw = render_daily_report_outbound(
        template, values, enabled_keys=enabled_keys
    )
    return text


def render_daily_report_outbound(
    template: str | None,
    values: dict[str, Any],
    *,
    enabled_keys: list[str],
) -> tuple[str, dict]:
    """Fill template; preserve packed custom-emoji entities when present."""
    from app.services.rich_text import (
        rich_plain_text,
        substitute_preserving_entities,
        unpack_rich_text,
    )

    plain, ents = unpack_rich_text(template)
    raw = (plain or "").strip() or DEFAULT_REPORT_TEMPLATE
    if not rich_plain_text(template).strip():
        ents = None
    fill: dict[str, Any] = {}
    enabled = set(enabled_keys)
    for m in _METRICS:
        if m.key in enabled:
            fill[m.key] = values.get(m.key, "")
        else:
            fill[m.key] = ""
    if ents:
        text, out_ents = substitute_preserving_entities(raw, ents, fill)
        text = (text or "").strip()
        if out_ents:
            return text, {"entities": out_ents, "parse_mode": None}
        return text, {}
    return safe_format(raw, fill).strip(), {}


async def build_daily_report(
    session: AsyncSession,
    *,
    reseller_id: int | None,
    actor: str,
    admin_name: str,
    template: str | None,
    metrics_raw: str | None,
) -> str:
    text, _kw = await build_daily_report_outbound(
        session,
        reseller_id=reseller_id,
        actor=actor,
        admin_name=admin_name,
        template=template,
        metrics_raw=metrics_raw,
    )
    return text


async def build_daily_report_outbound(
    session: AsyncSession,
    *,
    reseller_id: int | None,
    actor: str,
    admin_name: str,
    template: str | None,
    metrics_raw: str | None,
) -> tuple[str, dict]:
    keys = parse_metric_keys(metrics_raw, actor=actor)
    values = await collect_report_values(
        session,
        reseller_id=reseller_id,
        actor=actor,
        admin_name=admin_name,
        enabled_keys=keys,
    )
    return render_daily_report_outbound(template, values, enabled_keys=keys)


async def shop_daily_report_chat_ids(session: AsyncSession, reseller_id: int) -> list[int]:
    """Owner + bot_admin_ids for shop daily report (no notify_* ACL gate)."""
    from app.db.models import ResellerProfile
    from app.services.platform_identity import deliverable_telegram_id
    from app.services.reseller_access import parse_telegram_ids

    profile = (
        await session.execute(
            select(ResellerProfile).where(ResellerProfile.user_id == int(reseller_id))
        )
    ).scalar_one_or_none()
    if profile is None or not profile.is_active:
        return []
    ids: list[int] = []
    seen: set[int] = set()
    owner = await session.get(BotUser, int(reseller_id))
    if owner and owner.telegram_id is not None:
        tid = deliverable_telegram_id(int(owner.telegram_id))
        if tid is not None:
            seen.add(tid)
            ids.append(tid)
    for raw in parse_telegram_ids(profile.bot_admin_ids):
        tid = deliverable_telegram_id(raw)
        if tid is not None and tid not in seen:
            seen.add(tid)
            ids.append(tid)
    return ids
