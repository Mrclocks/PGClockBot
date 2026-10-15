"""Editable monitoring entry labels and static prompt defaults."""

from __future__ import annotations


CONNECTION_STATUS_MENU_KEY = "connection_status"

MONITOR_TEXT_DEFAULTS = {
    "btn_connection_status": "📡 وضعیت از دید سرور",
    "connection_status_title": "وضعیت هاست‌ها از دید سرور ربات",
    "connection_status_empty": "هنوز موردی برای نمایش عمومی منتشر نشده است.",
    "connection_status_error": "دریافت وضعیت اکنون ممکن نیست؛ کمی بعد دوباره تلاش کنید.",
    "btn_connection_status_admin": "📡 مدیریت وضعیت اتصال",
    "connection_status_admin_title": "📡 وضعیت اتصال هاست‌ها",
    "connection_status_account_prompt": "نام کاربری حساب موجود و اختصاصی پایش در پنل را بفرستید. از حساب مشتری استفاده نکنید.",
    "connection_status_name_prompt": "نام عمومی این هاست را وارد کنید؛ حداکثر ۶۴ نویسه.",
    "connection_status_order_prompt": "ترتیب نمایش را به صورت عدد صحیح از صفر وارد کنید.",
}

REASON_LABELS = {
    "core_unavailable": "هستهٔ پایش در دسترس نیست.", "account_unavailable": "حساب پایش قابل تأیید نیست.",
    "catalog_unavailable": "اطلاعات تازهٔ پنل یا حساب پایش قابل تأیید نیست.",
    "control_unavailable": "اعتبارسنجی مقصدهای آزمایش ممکن نشد.", "proxy_failed": "درخواست HTTPS از مسیر پروکسی ناموفق بود.",
    "fallback_destination": "اتصال از مقصد دوم تأیید شد.", "probe_unavailable": "بررسی این مسیر کامل نشد.",
    "snapshot_invalid": "نتیجهٔ ذخیره‌شده معتبر نیست.", "monitor_paused": "پایش متوقف است.",
    "stale": "نتیجه قدیمی است و وضعیت فعلی تأیید نشده است.", "slow_delay": "میانهٔ تأخیر بیش از دو ثانیه است.",
    "recovering": "تأیید بازیابی یا پوشش همهٔ مسیرها هنوز کامل نیست.", "no_observation": "هنوز بررسی کامل ثبت نشده است.",
    "invalid_catalog": "اطلاعات هاست‌ها معتبر نیست.", "host_unavailable": "هاست فعال در دسترس نیست.",
    "out_of_scope": "هاست در محدودهٔ مجاز نیست.", "unsupported_route": "ترکیب تنظیمات این مسیر پشتیبانی نشده است.",
    "ambiguous_route": "نگاشت هاست به مسیر یکتا نیست.", "missing_route": "کانفیگ معتبر این هاست در اشتراک پایش نیست.",
    "endpoint_capacity": "تعداد مسیرهای مؤثر از ظرفیت پایش بیشتر است.",
}
