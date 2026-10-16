"""Editable monitoring entry labels and static prompt defaults."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.services.connection_status_types import ConnectionStatus
from app.services.daily_report import format_jalali_parts


CONNECTION_STATUS_MENU_KEY = "connection_status"

MONITOR_TEXT_DEFAULTS = {
    "btn_connection_status": "📡 وضعیت سرورها",
    "connection_status_title": "وضعیت هاست‌ها از دید سرور ربات",
    "connection_status_empty": "هنوز هاستی برای نمایش به کاربران انتخاب نشده است.",
    "connection_status_error": "دریافت وضعیت اکنون ممکن نیست؛ کمی بعد دوباره تلاش کنید.",
    "btn_connection_status_admin": "📡 وضعیت اتصال",
    "connection_status_admin_title": "📡 وضعیت اتصال",
    "connection_status_account_prompt": "فقط نام کاربری یک کاربر تست موجود در پاسارگارد را بفرستید؛ مثلاً MattDev. این کاربر باید جدا از سرویس مشتری باشد. لینک، توکن یا رمز عبور نفرستید.",
    "connection_status_name_prompt": "این کانفیگ با چه اسمی به کاربران نمایش داده شود؟ حداکثر ۶۴ حرف وارد کنید.",
    "connection_status_order_prompt": "ترتیب نمایش این کانفیگ را با عدد وارد کنید؛ عدد کوچک‌تر بالاتر نمایش داده می‌شود. از صفر شروع کنید.",
}


STATUS_LABELS = {
    ConnectionStatus.HEALTHY: "🟢 در دسترس", ConnectionStatus.DEGRADED: "🟠 اختلال",
    ConnectionStatus.DOWN: "🔴 خارج از دسترس", ConnectionStatus.UNKNOWN: "⚪ نامشخص",
    ConnectionStatus.NOT_READY: "⚪ آمادهٔ تست نیست", ConnectionStatus.PAUSED: "⏸ متوقف",
}
# Web badges carry their own colour; the leading emoji is only for Telegram messages.
PLAIN_STATUS_LABELS = {status: label.split(" ", 1)[1] for status, label in STATUS_LABELS.items()}
REASON_LABELS = {
    "core_unavailable": "هستهٔ Xray آماده نیست؛ تنظیمات سرور را بررسی کنید.", "account_unavailable": "کاربر تست قابل استفاده نیست؛ تنظیمات آن را بررسی کنید.",
    "catalog_unavailable": "اطلاعات پنل یا کاربر تست دریافت نشد؛ تنظیمات اتصال را بررسی کنید.",
    "control_unavailable": "دسترسی سرور به سایت تست تأیید نشد.", "proxy_failed": "درخواست HTTPS از مسیر پروکسی ناموفق بود.",
    "fallback_destination": "اتصال از مقصد دوم تأیید شد.", "probe_unavailable": "بررسی این مسیر کامل نشد.",
    "snapshot_invalid": "نتیجهٔ ذخیره‌شده معتبر نیست.", "monitor_paused": "تست اتصال خاموش است.",
    "stale": "نتیجه قدیمی است و وضعیت فعلی تأیید نشده است.", "slow_delay": "تأخیر واقعی بیشتر از دو ثانیه است.",
    "recovering": "بعضی مسیرها مشکل دارند یا اتصال هنوز کاملاً تأیید نشده.", "no_observation": "هنوز بررسی کامل ثبت نشده است.",
    "invalid_catalog": "اطلاعات هاست‌ها معتبر نیست.", "host_unavailable": "هاست فعال در دسترس نیست.",
    "config_unavailable": "این کانفیگ دیگر در اشتراک نیست یا محتوای آن تغییر کرده؛ دوباره از فهرست اشتراک انتخاب کنید.",
    "out_of_scope": "هاست در محدودهٔ مجاز نیست.", "unsupported_route": "این کانفیگ با تنظیمات فعلی Xray قابل تست نیست؛ خروجی آن بدون تغییر در لیست نگه داشته شده است.",
    "ambiguous_route": "کانفیگ این هاست مشخص نیست؛ تنظیمات هاست را بررسی کنید.", "missing_route": "کانفیگ این هاست در اشتراک کاربر تست نیست.",
    "endpoint_capacity": "تعداد مسیرها بیشتر از ظرفیت تست هم‌زمان است.",
}


LEGACY_MONITOR_TEXTS = {
    "btn_connection_status": "📡 وضعیت از دید سرور",
    "connection_status_empty": "هنوز موردی برای نمایش عمومی منتشر نشده است.",
    "btn_connection_status_admin": "📡 تست و وضعیت اتصال",
    "connection_status_admin_title": "📡 تست و وضعیت اتصال",
    "connection_status_account_prompt": "نام کاربری حساب موجود و اختصاصی پایش در پنل را بفرستید. از حساب مشتری استفاده نکنید.",
    "connection_status_name_prompt": "نام عمومی این هاست را وارد کنید؛ حداکثر ۶۴ نویسه.",
    "connection_status_order_prompt": "ترتیب نمایش را به صورت عدد صحیح از صفر وارد کنید.",
}

# Defaults shipped in 0.4.6/0.4.7 that mixed Latin terms into Persian copy.
LATIN_MIXED_MONITOR_TEXTS = {
    "connection_status_account_prompt": "فقط username یک Test user موجود در PasarGuard را بفرستید؛ مثلاً MattDev. این user باید جدا از سرویس مشتری باشد. لینک، token یا password نفرستید.",
}

# Admin labels from before 0.4.8; kept so untouched older defaults are refreshed and still match the button.
OLDER_ADMIN_LABELS = {
    "btn_connection_status_admin": "📡 مدیریت وضعیت اتصال",
    "connection_status_admin_title": "📡 وضعیت اتصال هاست‌ها",
}

LEGACY_HOST_PROMPTS = {
    "connection_status_name_prompt": "این هاست با چه اسمی به کاربران نمایش داده شود؟ حداکثر ۶۴ حرف وارد کنید.",
    "connection_status_order_prompt": "ترتیب نمایش این هاست را با عدد وارد کنید؛ عدد کوچک‌تر بالاتر نمایش داده می‌شود. از صفر شروع کنید.",
}


def readable_monitor_texts(settings: dict[str, str]) -> dict[str, str]:
    """Refresh untouched old defaults while retaining every custom label and menu order."""
    for previous_texts in (LEGACY_MONITOR_TEXTS, LEGACY_HOST_PROMPTS):
        for key, previous in previous_texts.items():
            outdated = {previous, LATIN_MIXED_MONITOR_TEXTS.get(key, previous), OLDER_ADMIN_LABELS.get(key, previous)}
            if settings.get(key) in outdated:
                settings[key] = MONITOR_TEXT_DEFAULTS[key]
    return settings


def connection_check_time(value: datetime | None) -> str:
    if value is None:
        return "هنوز تست نشده"
    date = format_jalali_parts(value)["date"]
    local_time = value.astimezone(ZoneInfo("Asia/Tehran")).strftime("%H:%M:%S")
    return f"{date} {local_time}"
