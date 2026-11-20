"""Features whose background work costs the server noticeably more than the rest.

The settings pages tag these switches «پر مصرف» and say in one line what runs in the
background while they are on, so an owner on a small server knows what to turn off first.
The texts describe the background work in this codebase; keep them in step with the jobs
they name (``app/jobs/scheduler.py``) when an interval or a fan-out changes.
"""

from __future__ import annotations

HEAVY_LABEL = "پر مصرف"

# Settings key -> what runs in the background while the switch is on.
HEAVY_SETTINGS: dict[str, str] = {
    "user_alert_low_enabled": (
        "هر ۶ ساعت وضعیت تک‌تک سرویس‌های فعال از پنل خوانده می‌شود؛ با تعداد زیاد سرویس، بار پنل و شبکه بالا می‌رود."
    ),
    "first_use_nudge_enabled": (
        "هر ۳۰ دقیقه سرویس‌های تازه یکی‌یکی از پنل خوانده می‌شوند تا مصرف نداشتنشان معلوم شود."
    ),
}

# Connection monitoring has its own page and switch, not a settings key.
CONNECTION_MONITOR_COST = (
    "هر دقیقه هستهٔ xray روی همین سرور اجرا می‌شود و کانفیگ‌های انتخاب‌شده تست می‌شوند؛ "
    "بیشترین مصرف پردازنده و رم را بین قابلیت‌ها دارد."
)


def heavy_setting_note(key: str) -> str:
    """The background cost of a setting, or an empty string when it is not tagged."""
    return HEAVY_SETTINGS.get(key, "")
