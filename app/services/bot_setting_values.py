"""Value rules for number and choice settings typed or tapped in the owner and shop bots.

Every number field a bot offers has whole-number bounds here, taken from what its consumers
accept, so the bots never store NaN, infinity, a negative price or a value the reader would
clamp or crash on. Choice fields list their values with the web's labels.
"""

from __future__ import annotations

from collections.abc import Callable

from app.services import bot_feedback_texts as feedback
from app.services.approval_rule import MAX_MINUTES as APPROVAL_MAX_MINUTES
from app.services.backup_settings import KEEP_CHOICES as BACKUP_KEEP_CHOICES, KEEP_KEY as BACKUP_KEEP_KEY
from app.services import crypto_rates
from app.services.catalog_pricing import MAX_PRICE
from app.services.miniapp_branding import NOTICE_SETTING as MINIAPP_NOTICE, normalize_notice
from app.services.wallet_topup import normalize_topup_presets
from app.services.customer_notice_texts import NOTICE_TEXT_KEYS
from app.services.entry_approval_config import ENTRY_APPROVAL_NUMBER_BOUNDS
from app.services.message_variables import template_variable_specs
from app.services.numbers import normalize_number_text, to_fa_digits
from app.services.service_import_config import (
    KEY_MAX_PER_USER as IMPORT_KEY_MAX_PER_USER,
    KEY_MODE as IMPORT_KEY_MODE,
    KEY_PLACEMENT as IMPORT_KEY_PLACEMENT,
    MAX_PER_USER_LIMIT as IMPORT_MAX_PER_USER_LIMIT,
    MIN_PER_USER as IMPORT_MIN_PER_USER,
    PLACEMENT_CHOICES as IMPORT_PLACEMENT_CHOICES,
    UNLIMITED_VALUE as IMPORT_UNLIMITED_VALUE,
    UNLIMITED_WORDS as IMPORT_UNLIMITED_WORDS,
)
from app.services.service_cancellation_settings import CANCEL_BOUNDS, CHOICES as CANCEL_CHOICES
from app.services.service_freeze_settings import FREEZE_BOUNDS
from app.services.service_transfer_settings import TRANSFER_BOUNDS
from app.services.ticket_reminders import MAX_HOURS as TICKET_REMINDER_MAX_HOURS, SETTING_KEY as TICKET_REMINDER_KEY
from app.services.settings_operation_fields import ALERT_UNIT_OPTIONS, OPERATION_NUMBER_BOUNDS

TOPUP_PRESETS_KEY = "wallet_topup_presets"
CUSTOM_PLAN_MAX_GB = 100_000
CUSTOM_PLAN_MAX_DAYS = 3650
DAILY_REPORT_LAST_HOUR = 23

# Whole-number bounds, inclusive, of every bot number setting.
NUMBER_BOUNDS: dict[str, tuple[int, int]] = {
    "billing_low_balance": (0, 10_000_000_000),
    "billing_tick_minutes": (1, 1440),
    "credit_due_days": (0, 365),
    "credit_block_create_days": (0, 365),
    "credit_block_changes_days": (0, 365),
    "credit_block_days": (0, 365),
    # Same bounds the consumers apply (approval_rule, daily_report, service_import_config).
    "auto_approve_after_minutes": (0, APPROVAL_MAX_MINUTES),
    "auto_approve_after_max_amount": (0, MAX_PRICE),
    "admin_daily_report_hour": (0, DAILY_REPORT_LAST_HOUR),
    "backup_schedule_hour": (0, 23),
    crypto_rates.DIVISOR_KEY: (1, 1_000_000),
    crypto_rates.MINUTES_KEY: (crypto_rates.MIN_MINUTES, crypto_rates.MAX_MINUTES),
    # Money and catalog numbers: never negative, never above the catalog price cap.
    "referral_bonus": (0, MAX_PRICE),
    "stars_toman_per_star": (1, MAX_PRICE),
    "custom_plan_price_per_gb": (0, MAX_PRICE),
    "custom_plan_price_per_day": (0, MAX_PRICE),
    "custom_plan_min_gb": (1, CUSTOM_PLAN_MAX_GB),
    "custom_plan_max_gb": (1, CUSTOM_PLAN_MAX_GB),
    "custom_plan_min_days": (1, CUSTOM_PLAN_MAX_DAYS),
    "custom_plan_max_days": (1, CUSTOM_PLAN_MAX_DAYS),
    **OPERATION_NUMBER_BOUNDS,
    IMPORT_KEY_MAX_PER_USER: (IMPORT_MIN_PER_USER, IMPORT_MAX_PER_USER_LIMIT),
    **FREEZE_BOUNDS,
    **TRANSFER_BOUNDS,
    **CANCEL_BOUNDS,
    **ENTRY_APPROVAL_NUMBER_BOUNDS,
    TICKET_REMINDER_KEY: (0, TICKET_REMINDER_MAX_HOURS),
}

# Number settings that also take «نامحدود» (stored as the value here), like the web field's button.
NUMBER_UNLIMITED_VALUES: dict[str, str] = {IMPORT_KEY_MAX_PER_USER: IMPORT_UNLIMITED_VALUE}
UNLIMITED_LABEL = "نامحدود"

# Choice fields cycle through these (value, Persian label); the same values the web offers.
SELECT_OPTIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "billing_on_empty": (("block_provision", "مسدود کردن ساخت و تمدید سرویس"),),
    IMPORT_KEY_MODE: (("instant", "فوری"), ("approval", "با تأیید ادمین")),
    IMPORT_KEY_PLACEMENT: IMPORT_PLACEMENT_CHOICES,
    **CANCEL_CHOICES,
    **ALERT_UNIT_OPTIONS,
    crypto_rates.MODE_KEY: (
        (crypto_rates.MODE_OFF, "خاموش"),
        (crypto_rates.MODE_MANUAL, "نرخ دستی"),
        (crypto_rates.MODE_API, "نرخ از API"),
    ),
    # Same choices as the web; the new count applies when the next backup prunes the old ones.
    BACKUP_KEEP_KEY: tuple((str(count), f"{to_fa_digits(count)} بکاپ") for count in BACKUP_KEEP_CHOICES),
}


def number_range_hint(key: str) -> str:
    """The Persian hint for a bounded number field."""
    low, high = NUMBER_BOUNDS[key]
    hint = f"یک عدد صحیح بین {low} و {high} بفرستید"
    return hint + (f" یا «{UNLIMITED_LABEL}»." if key in NUMBER_UNLIMITED_VALUES else ".")


def parse_bounded_int(raw: str, low: int, high: int) -> int:
    """Whole number in ``[low, high]``; rejects decimals, NaN, infinity, exponents and out-of-range values."""
    text = normalize_number_text(raw)
    if not text or text in {".", "-", "+"} or any(ch in text for ch in ".eE"):
        raise ValueError("not an integer")
    try:
        value = int(text)
    except ValueError:
        raise ValueError("not an integer") from None
    if value < low or value > high:
        raise ValueError("out of range")
    return value


def parse_number_setting(key: str, text: str) -> str:
    """Stored text of a number setting; ``ValueError`` carries the Persian hint to show.

    A number field without bounds is refused: every bot number field must be listed in
    ``NUMBER_BOUNDS``.
    """
    raw = str(text or "").replace(",", "").replace("٬", "")
    if key in NUMBER_UNLIMITED_VALUES and raw.strip().lower() in IMPORT_UNLIMITED_WORDS:
        return NUMBER_UNLIMITED_VALUES[key]
    bounds = NUMBER_BOUNDS.get(key)
    if bounds is None:
        raise ValueError(feedback.ENTER_VALID_NUMBER_SHORT)
    try:
        return str(parse_bounded_int(raw, bounds[0], bounds[1]))
    except ValueError:
        raise ValueError(number_range_hint(key)) from None


def select_label(key: str, raw: str | None) -> str:
    """The label of the stored choice, or of the first choice when the stored value is unknown."""
    options = SELECT_OPTIONS.get(key) or ()
    by_value = dict(options)
    if raw in by_value:
        return by_value[raw]
    if options:
        return options[0][1]
    return str(raw or "—")


def next_select_value(key: str, current: str | None) -> str:
    """The choice after ``current``; an unknown stored value moves to the second choice."""
    values = [value for value, _label in SELECT_OPTIONS[key]]
    cur = current if current in values else values[0]
    return values[(values.index(cur) + 1) % len(values)]


# Texts whose empty value means «use the built-in text»; typing one of DEFAULT_WORDS clears them.
EMPTY_MEANS_DEFAULT_KEYS: frozenset[str] = NOTICE_TEXT_KEYS | {
    "user_alert_time_text",
    "user_alert_traffic_text",
    TOPUP_PRESETS_KEY,
}
DEFAULT_WORDS: frozenset[str] = frozenset({"پیش‌فرض", "پیشفرض", "default"})


def wants_default_text(key: str, text: str | None) -> bool:
    """True when the typed value asks to go back to the built-in text of ``key``."""
    return key in EMPTY_MEANS_DEFAULT_KEYS and str(text or "").strip().lower() in DEFAULT_WORDS


def text_template_hint(key: str, *, actor: str = "owner") -> str:
    """Extra prompt lines for a text template: its placeholders and how to restore the default."""
    names = template_variable_specs(actor).get(key) or {}
    lines = []
    if names:
        tokens = " ".join(f"<code>{{{name}}}</code>" for name in sorted(names))
        lines.append(f"متغیرها: {tokens}")
    if key in EMPTY_MEANS_DEFAULT_KEYS:
        lines.append("برای برگشت به متن پیش‌فرض بنویسید: «پیش‌فرض»")
    elif key in CLEARABLE_KEYS:
        lines.append("برای خالی کردن بنویسید: «-»")
    return "\n".join(lines)


# Typed texts saved through their service rule instead of rich-text packing; ValueError = refuse.
CRYPTO_TEXT_KEYS = (
    crypto_rates.ASSET_KEY,
    crypto_rates.MANUAL_KEY,
    crypto_rates.MARGIN_KEY,
    crypto_rates.URL_KEY,
    crypto_rates.PATH_KEY,
)
CLEARABLE_KEYS: frozenset[str] = frozenset({crypto_rates.MANUAL_KEY, crypto_rates.URL_KEY, crypto_rates.PATH_KEY})


def _crypto_normalizer(key: str) -> Callable[[str], str]:
    return lambda text: crypto_rates.normalize_rate_setting(key, text)


VALUE_NORMALIZERS: dict[str, Callable[[str], str]] = {
    MINIAPP_NOTICE: normalize_notice,
    TOPUP_PRESETS_KEY: normalize_topup_presets,
    **{key: _crypto_normalizer(key) for key in CRYPTO_TEXT_KEYS},
}
