"""Features whose background work costs the server noticeably more than the rest.

The settings pages tag these switches «پر مصرف» and say in one line what runs in the
background while they are on, so an owner on a small server knows what to turn off first.
The texts describe the background work in this codebase; keep them in step with the jobs
they name (``app/jobs/scheduler.py``) when an interval or a fan-out changes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.services.first_use_nudge import CHECK_INTERVAL_MINUTES as FIRST_USE_CHECK_MINUTES
from app.services.job_metrics import JobStatsView
from app.services.numbers import to_fa_digits
from app.services.usage_alerts import ALERT_CHECK_INTERVAL_HOURS

HEAVY_LABEL = "پر مصرف"

# Settings key -> what runs in the background while the switch is on.
HEAVY_SETTINGS: dict[str, str] = {
    "user_alert_low_enabled": (
        f"هر {to_fa_digits(ALERT_CHECK_INTERVAL_HOURS)} ساعت وضعیت تک‌تک سرویس‌های فعال از پنل خوانده می‌شود؛ "
        "با تعداد زیاد سرویس، بار پنل و شبکه بالا می‌رود."
    ),
    "first_use_nudge_enabled": (
        f"هر {to_fa_digits(FIRST_USE_CHECK_MINUTES)} دقیقه سرویس‌های تازه از پنل خوانده می‌شوند "
        "تا مصرف نداشتنشان معلوم شود."
    ),
}

# Connection monitoring has its own page and switch, not a settings key.
CONNECTION_MONITOR_COST = (
    "در هر دور بررسی هستهٔ xray روی همین سرور اجرا می‌شود و کانفیگ‌های انتخاب‌شده تست می‌شوند؛ "
    "بیشترین مصرف پردازنده و رم را بین قابلیت‌ها دارد. با «فاصلهٔ بررسی» بیشتر، مصرف کمتر می‌شود."
)


def heavy_setting_note(key: str) -> str:
    """The background cost of a setting, or an empty string when it is not tagged."""
    return HEAVY_SETTINGS.get(key, "")


# Persian names of the scheduled jobs (ids from app/jobs/scheduler.py) for the resource view.
JOB_LABELS: dict[str, str] = {
    "billing_tick": "صورت‌حساب مصرفی نماینده‌ها",
    "credit_cycle": "دورهٔ اعتبار نماینده‌ها",
    "cashback_mature": "آزادسازی کش‌بک",
    "pg_admin_subscription": "اشتراک ادمین‌های پنل",
    "order_purge": "پاکسازی سفارش‌های قدیمی",
    "scheduled_receipt_approval": "تأیید زمان‌دار رسید",
    "crypto_rate_refresh": "نرخ رمزارز",
    "scheduled_backup": "بکاپ زمان‌بندی‌شده",
    "connection_status": "تست اتصال سرویس",
    "pg_link_watchdog": "پایش ارتباط با پنل",
    "bbc_settlement_poll": "پیگیری پرداخت «بزن به کارت»",
    "psp_settlement_poll": "پیگیری پرداخت درگاه",
    "paid_order_recovery": "بازیابی سفارش پرداخت‌شده",
    "volume_external_sync": "شمارش حجم کاربران پنل",
    "chat_message_purge": "پاکسازی پیام‌های گفتگو",
    "service_cleanup": "پاکسازی سرویس‌ها",
    "switch_reverts": "برگشت سوییچ‌های زمان‌دار",
    "bot_profile_photo": "عکس پروفایل ربات",
    "user_photo_prune": "پاکسازی عکس کاربران",
    "cancellation_notifications": "اعلان لغو سرویس",
    "customer_notices": "اعلان‌های مشتری",
    "panel_alert_notifications": "هشدارهای پنل",
    "targeted_campaigns": "کمپین‌ها",
    "broadcast": "ارسال همگانی",
    "first_use_nudge": "پیام سرویس بدون مصرف",
    "service_automation": "تمدید و افزایش خودکار",
    "expiry": "هشدار کمبود حجم و زمان",
    "admin_daily_report": "گزارش روزانه",
}

# Jobs behind the switches tagged above, plus the panel recount of capacity-gated shops.
HEAVY_JOBS = frozenset({"connection_status", "expiry", "first_use_nudge", "volume_external_sync"})

RESOURCE_VIEW_LIMIT = 10


@dataclass(frozen=True, slots=True)
class JobUsageRow:
    job_id: str
    label: str
    runs: int
    average_ms: int
    max_ms: int
    failures: int
    overlaps: int
    is_heavy: bool


def job_usage_rows(stats: Sequence[JobStatsView], *, limit: int = RESOURCE_VIEW_LIMIT) -> list[JobUsageRow]:
    """The busiest jobs first (``stats`` is already ordered by total busy time), named in Persian."""
    return [
        JobUsageRow(
            job_id=view.job_id,
            label=JOB_LABELS.get(view.job_id, view.job_id),
            runs=view.runs,
            average_ms=round(view.average_seconds * 1000),
            max_ms=round(view.max_seconds * 1000),
            failures=view.failures,
            overlaps=view.overlaps,
            is_heavy=view.job_id in HEAVY_JOBS,
        )
        for view in stats[:limit]
    ]
