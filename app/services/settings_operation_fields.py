"""Operational settings offered in the bot settings: customer usage alerts, action center, sales state.

The same keys, labels and kinds as the web settings groups of the same names. Number bounds
match what the consumers accept, so a value saved from the bot means the same as one saved
from the web.
"""

from __future__ import annotations

from app.services.alert_thresholds import (
    TIME_UNIT_KEY,
    TIME_VALUE_KEY,
    TRAFFIC_UNIT_KEY,
    TRAFFIC_VALUE_KEY,
    UNIT_DAY,
    UNIT_GB,
    UNIT_PERCENT,
    VALUE_LIMITS,
)
from app.services.trial_guard import MAX_WAIT_MINUTES

# (key, short label, kind) — the shape of every bot settings field.
Field = tuple[str, str, str]

MAX_EXPIRE_WINDOW_DAYS = 365
MAX_RECEIPT_MATCH_MINUTES = 7 * 24 * 60

USAGE_ALERT_FIELDS: list[Field] = [
    ("user_alert_low_enabled", "ارسال خودکار هشدار کمبود", "toggle"),
    (TRAFFIC_VALUE_KEY, "هشدار وقتی حجم کمتر از", "number"),
    (TRAFFIC_UNIT_KEY, "واحد آستانهٔ حجم", "select"),
    (TIME_VALUE_KEY, "هشدار وقتی زمان کمتر از", "number"),
    (TIME_UNIT_KEY, "واحد آستانهٔ زمان", "select"),
    ("user_alert_time_text", "متن هشدار کمبود زمان", "textarea"),
    ("user_alert_traffic_text", "متن هشدار کمبود حجم", "textarea"),
    ("first_use_nudge_enabled", "پیام کمکی سرویس بدون مصرف", "toggle"),
    ("auto_renew_offer_enabled", "پیشنهاد تمدید خودکار", "toggle"),
]

OPERATIONS_FIELDS: list[Field] = [
    ("action_center_expire_days", "پنجرهٔ نزدیک انقضا (روز)", "number"),
    ("one_tap_renew_enabled", "دکمهٔ تمدید در هشدارها", "toggle"),
    ("capacity_warn_pct", "آستانهٔ هشدار ظرفیت (٪)", "number"),
    ("funnel_tracking_enabled", "ثبت رفتار کاربر", "toggle"),
]

# Platform-wide: the one panel link is watched once for every shop.
PG_LINK_HEALTH_FIELDS: list[Field] = [
    ("pg_link_watchdog_enabled", "پایش خودکار ارتباط", "toggle"),
    ("pg_link_alerts_enabled", "اعلان قطع و وصل", "toggle"),
]

SALES_STATE_FIELDS: list[Field] = [
    ("shop_maintenance_enabled", "حالت تعمیرات فروشگاه", "toggle"),
    ("shop_maintenance_text", "متن حالت تعمیرات", "textarea"),
]

RECEIPT_MATCH_FIELDS: list[Field] = [
    ("receipt_auto_match_enabled", "پیشنهاد تطبیق رسیدهای هم‌مبلغ", "toggle"),
    ("receipt_match_window_minutes", "بازهٔ تطبیق (دقیقه)", "number"),
]

# Same keys as the web «رسیدها» section; they apply while «تأیید خودکار رسید» is on.
AUTO_APPROVE_AFTER_FIELDS: list[Field] = [
    ("auto_approve_after_minutes", "مهلت تأیید خودکار (دقیقه، ۰ = فوری)", "number"),
    ("auto_approve_after_max_amount", "سقف مبلغ (تومان، ۰ = بدون سقف)", "number"),
    ("auto_approve_after_returning_only", "فقط مشتریان با خرید قبلی", "toggle"),
]

STARS_FIELDS: list[Field] = [
    ("stars_toman_per_star", "تومان هر استارز", "number"),
    ("stars_title", "عنوان فاکتور", "text"),
    ("stars_description", "توضیح فاکتور", "text"),
]

# Same keys as the web «رمزارز» conversion settings; the shop's own copy in the shop bot.
CRYPTO_RATE_FIELDS: list[Field] = [
    ("crypto_rate_mode", "تبدیل تومان به رمزارز", "select"),
    ("crypto_rate_asset", "ارز مبنای تبدیل", "text"),
    ("crypto_rate_manual", "نرخ دستی (تومان برای هر واحد)", "text"),
    ("crypto_rate_api_url", "آدرس API نرخ (HTTPS)", "text"),
    ("crypto_rate_api_path", "مسیر عدد در JSON", "text"),
    ("crypto_rate_api_divisor", "تقسیم‌کنندهٔ نرخ API", "number"),
    ("crypto_rate_margin_percent", "حاشیه روی مبلغ (٪)", "text"),
    ("crypto_quote_minutes", "اعتبار نرخ (دقیقه)", "number"),
]

WALLET_TOPUP_FIELDS: list[Field] = [
    ("wallet_topup_presets", "مبالغ آمادهٔ شارژ (تومان، با کاما)", "text"),
]

TRIAL_MIN_AGE_FIELD: Field = ("trial_min_account_minutes", "حداقل سن حساب (دقیقه، ۰ = خاموش)", "number")

# Whole-number bounds of the fields above.
OPERATION_NUMBER_BOUNDS: dict[str, tuple[int, int]] = {
    # The widest range of the threshold's units; readers clamp to the chosen unit (alert_thresholds).
    TRAFFIC_VALUE_KEY: (VALUE_LIMITS[UNIT_PERCENT][0], VALUE_LIMITS[UNIT_GB][1]),
    TIME_VALUE_KEY: (VALUE_LIMITS[UNIT_PERCENT][0], VALUE_LIMITS[UNIT_DAY][1]),
    "action_center_expire_days": (1, MAX_EXPIRE_WINDOW_DAYS),
    "capacity_warn_pct": (1, 100),
    "receipt_match_window_minutes": (1, MAX_RECEIPT_MATCH_MINUTES),
    "trial_min_account_minutes": (0, MAX_WAIT_MINUTES),
}

# The unit of each alert threshold, with the web's labels; only the chosen unit is compared.
ALERT_UNIT_OPTIONS: dict[str, tuple[tuple[str, str], ...]] = {
    TRAFFIC_UNIT_KEY: ((UNIT_PERCENT, "درصد"), (UNIT_GB, "گیگ")),
    TIME_UNIT_KEY: ((UNIT_PERCENT, "درصد"), (UNIT_DAY, "روز")),
}
