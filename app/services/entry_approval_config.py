"""Settings of «تأیید ورود کاربران جدید»: keys, defaults, the settings form and their parsing.

Each shop (and the platform) has its own copy of these keys. ``entry_approval_since`` is not
shown in any form: it records when the gate was last switched on, so customers who joined
before that moment keep using the bot.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from app.services.numbers import normalize_digits

KEY_ENABLED = "entry_approval_enabled"
KEY_SINCE = "entry_approval_since"
KEY_SKIP_REFERRED = "entry_approval_skip_referred"
KEY_REQUIRE_PHONE = "entry_approval_require_phone"
KEY_RETRY_HOURS = "entry_approval_retry_hours"
KEY_EXPIRE_DAYS = "entry_approval_expire_days"
KEY_SUPPORT = "entry_approval_support"
KEY_PHONE_TEXT = "entry_approval_phone_text"
KEY_PENDING_TEXT = "entry_approval_pending_text"
KEY_APPROVED_TEXT = "entry_approval_approved_text"
KEY_REJECTED_TEXT = "entry_approval_rejected_text"
NOTIFY_KEY = "notify_entry_approval"
PERMISSION = "customers_approve"

GROUP_TITLE = "تأیید ورود کاربران جدید"
RETRY_HOURS_MAX = 24 * 30
EXPIRE_DAYS_MAX = 90
DEFAULT_RETRY_HOURS = 24

ENTRY_APPROVAL_DEFAULTS: dict[str, str] = {
    KEY_ENABLED: "0",
    KEY_SINCE: "",
    KEY_SKIP_REFERRED: "0",
    KEY_REQUIRE_PHONE: "0",
    KEY_RETRY_HOURS: str(DEFAULT_RETRY_HOURS),
    KEY_EXPIRE_DAYS: "0",
    KEY_SUPPORT: "1",
    KEY_PHONE_TEXT: (
        "📱 برای ثبت درخواست ورود، شماره تماس خودتان را با دکمهٔ زیر ارسال کنید.\n"
        "شماره فقط برای بررسی درخواست به ادمین نشان داده می‌شود و ذخیره نمی‌شود."
    ),
    KEY_PENDING_TEXT: (
        "⏳ درخواست ورود شما ثبت شد و در انتظار تأیید ادمین است.\n"
        "به محض تأیید، همین‌جا به شما خبر می‌دهیم."
    ),
    KEY_APPROVED_TEXT: "✅ درخواست ورود شما تأیید شد. خوش آمدید!",
    KEY_REJECTED_TEXT: "درخواست ورود شما به ربات تأیید نشد.",
    NOTIFY_KEY: "1",
}

ENTRY_APPROVAL_TEXT_KEYS = frozenset({KEY_PHONE_TEXT, KEY_PENDING_TEXT, KEY_APPROVED_TEXT, KEY_REJECTED_TEXT})
ENTRY_APPROVAL_NUMBER_BOUNDS: dict[str, tuple[int, int]] = {
    KEY_RETRY_HOURS: (0, RETRY_HOURS_MAX),
    KEY_EXPIRE_DAYS: (0, EXPIRE_DAYS_MAX),
}


def entry_approval_setting_fields() -> list[tuple[str, str, str, str]]:
    """Fields of the settings card, in the same tuple shape as ``SETTING_GROUPS``."""
    return [
        (
            KEY_ENABLED,
            "ورود فقط با تأیید ادمین",
            "toggle",
            "روشن باشد، کاربری که از این به بعد ربات را استارت کند تا تأیید ادمین منو و مینی‌اپ را نمی‌بیند. "
            "برای ادمین پیام تأیید یا رد می‌رود. کاربران قبلی معاف‌اند.",
        ),
        (
            KEY_SKIP_REFERRED,
            "معافیت ورود با لینک دعوت",
            "toggle",
            "کاربری که با کد یا لینک دعوت یک مشتری همین ربات وارد شود بدون تأیید وارد می‌شود.",
        ),
        (
            KEY_REQUIRE_PHONE,
            "دریافت شماره تماس قبل از درخواست",
            "toggle",
            "کاربر اول شمارهٔ همین حساب تلگرام را می‌فرستد و بعد درخواست برای ادمین می‌رود. "
            "شماره فقط در پیام ادمین دیده می‌شود و خام ذخیره نمی‌شود؛ برای خرید هم تأییدشده حساب می‌شود.",
        ),
        (
            KEY_SUPPORT,
            "دکمهٔ پشتیبانی در زمان انتظار",
            "toggle",
            "زیر پیام انتظار و پیام رد، دکمهٔ «تماس با پشتیبانی» نمایش داده می‌شود.",
        ),
        (
            KEY_RETRY_HOURS,
            "درخواست دوباره بعد از رد (ساعت)",
            "number",
            f"کاربر ردشده بعد از این مدت می‌تواند دوباره درخواست بدهد. ۰ یعنی هرگز. حداکثر {RETRY_HOURS_MAX}. "
            "«مسدود» هیچ‌وقت دوباره باز نمی‌شود.",
        ),
        (
            KEY_EXPIRE_DAYS,
            "رد خودکار درخواست بی‌پاسخ (روز)",
            "number",
            f"درخواستی که این‌قدر بی‌جواب بماند خودکار رد می‌شود. ۰ یعنی خاموش. حداکثر {EXPIRE_DAYS_MAX}.",
        ),
        (KEY_PHONE_TEXT, "پیام دریافت شماره", "textarea", "بالای دکمهٔ «ارسال شماره تماس»"),
        (KEY_PENDING_TEXT, "پیام در انتظار تأیید", "textarea", "به کاربری که هنوز تأیید نشده نشان داده می‌شود"),
        (KEY_APPROVED_TEXT, "پیام تأیید", "textarea", "بعد از تأیید، همراه دکمهٔ ورود به ربات فرستاده می‌شود"),
        (KEY_REJECTED_TEXT, "پیام رد", "textarea", "بعد از رد یا مسدودسازی فرستاده می‌شود"),
    ]


def entry_approval_visibility() -> dict[str, tuple[str, tuple[str, ...]]]:
    """Every other field of the card shows only while the gate is on."""
    return {key: (KEY_ENABLED, ("1",)) for key, *_ in entry_approval_setting_fields() if key != KEY_ENABLED}


@dataclass(frozen=True, slots=True)
class EntryApprovalConfig:
    enabled: bool
    since: datetime | None
    skip_referred: bool
    require_phone: bool
    retry_hours: int
    expire_days: int
    show_support: bool


def _is_on(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _bounded(raw: str | None, key: str, default: int) -> int:
    low, high = ENTRY_APPROVAL_NUMBER_BOUNDS[key]
    try:
        value = int(normalize_digits(str(raw or "")).strip())
    except ValueError:
        return default
    return min(max(value, low), high)


def parse_since(raw: str | None) -> datetime | None:
    try:
        value = datetime.fromisoformat((raw or "").strip())
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def read_config(ui: Mapping[str, str]) -> EntryApprovalConfig:
    return EntryApprovalConfig(
        enabled=_is_on(ui.get(KEY_ENABLED)),
        since=parse_since(ui.get(KEY_SINCE)),
        skip_referred=_is_on(ui.get(KEY_SKIP_REFERRED)),
        require_phone=_is_on(ui.get(KEY_REQUIRE_PHONE)),
        retry_hours=_bounded(ui.get(KEY_RETRY_HOURS), KEY_RETRY_HOURS, DEFAULT_RETRY_HOURS),
        expire_days=_bounded(ui.get(KEY_EXPIRE_DAYS), KEY_EXPIRE_DAYS, 0),
        show_support=_is_on(ui.get(KEY_SUPPORT)),
    )


def touches_entry_approval(values: Mapping[str, str]) -> bool:
    return KEY_ENABLED in values or KEY_SINCE in values or any(k in values for k in ENTRY_APPROVAL_NUMBER_BOUNDS)


def prepare_saved_values(values: dict[str, str], current: Mapping[str, str]) -> dict[str, str]:
    """``values`` ready to store: numbers clamped, and the moment the gate was switched on.

    Saving the form again while the gate stays on keeps the first moment, so the customers
    exempted then stay exempted; switching it off clears it. ``entry_approval_since`` itself
    is never taken from a form.
    """
    out = {k: v for k, v in values.items() if k != KEY_SINCE}
    for key in ENTRY_APPROVAL_NUMBER_BOUNDS:
        if key in out:
            out[key] = str(_bounded(out[key], key, int(ENTRY_APPROVAL_DEFAULTS[key])))
    if KEY_ENABLED not in out:
        return out
    turning_on = _is_on(out[KEY_ENABLED])
    if not turning_on:
        out[KEY_SINCE] = ""
    elif not _is_on(current.get(KEY_ENABLED)) or not parse_since(current.get(KEY_SINCE)):
        out[KEY_SINCE] = datetime.now(timezone.utc).isoformat()
    return out
