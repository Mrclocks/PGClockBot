"""Lucky wheel setting keys and limits, shared by the wheel, the web panel and the bot's settings.

Kept free of the wheel engine's imports so the bot's settings editor can read the limits.
"""

from __future__ import annotations

SETTING_WHEEL_ENABLED = "lucky_wheel_enabled"
SETTING_SPIN_COST = "lucky_wheel_spin_cost_points"
SETTING_DAILY_LIMIT = "lucky_wheel_daily_limit"
SETTING_COOLDOWN = "lucky_wheel_cooldown_seconds"
# "Once every N days": the shortest gap between two spins of one customer, in days (0 = off).
SETTING_INTERVAL_DAYS = "lucky_wheel_interval_days"
SETTING_FREE_SPINS_DAILY = "lucky_wheel_free_spins_daily"

MAX_SPIN_COST = 1_000_000
MAX_DAILY_LIMIT = 100
MAX_COOLDOWN = 86_400
MAX_INTERVAL_DAYS = 365
MAX_FREE_SPINS_DAILY = 50

WHEEL_NUMBER_BOUNDS: dict[str, tuple[int, int]] = {
    SETTING_SPIN_COST: (0, MAX_SPIN_COST),
    SETTING_DAILY_LIMIT: (0, MAX_DAILY_LIMIT),
    SETTING_COOLDOWN: (0, MAX_COOLDOWN),
    SETTING_INTERVAL_DAYS: (0, MAX_INTERVAL_DAYS),
    SETTING_FREE_SPINS_DAILY: (0, MAX_FREE_SPINS_DAILY),
}

GROUP_TITLE = "چرخ شانس"
# The same fields as the web «چرخ شانس» settings; prizes and the menu order stay on the web.
BOT_FIELDS: list[tuple[str, str, str]] = [
    (SETTING_WHEEL_ENABLED, "فعال بودن چرخ شانس", "toggle"),
    (SETTING_SPIN_COST, "هزینه هر چرخش (امتیاز، ۰ = رایگان)", "number"),
    (SETTING_DAILY_LIMIT, "سقف روزانه (۰ = نامحدود)", "number"),
    (SETTING_COOLDOWN, "کولدون (ثانیه)", "number"),
    (SETTING_INTERVAL_DAYS, "هر چند روز یک‌بار (۰ = خاموش)", "number"),
    (SETTING_FREE_SPINS_DAILY, "چرخش رایگان روزانه", "number"),
]
