"""Editable texts of the notices a customer gets about their service.

Each notice has a setting holding its template; an empty setting keeps the built-in text of
``customer_action_notices``. A shop edits its own copy, used for the notices its bot sends.
Placeholders are filled through ``render_message_template`` with escaped values, so a template
can only place the service name, the reason, the deletion date and the shop title.
"""

from __future__ import annotations

# (key, label, kind, help) — the shape of a web settings field.
WebField = tuple[str, str, str, str]
# (key, label, kind) — the shape of a bot settings field.
BotField = tuple[str, str, str]

KEY_PREFIX = "notice_text_"

# Notices about something staff did, with the reason they gave.
STAFF_ACTION_LABELS: dict[str, str] = {
    "service_renew": "تمدید سرویس",
    "service_extend": "تغییر مانده سرویس",
    "service_delete": "حذف سرویس",
    "service_detach": "جدا شدن سرویس از لیست",
    "service_import_assign": "افزودن سرویس قبلی",
    "pg_user_disable": "غیرفعال شدن سرویس",
    "pg_user_enable": "فعال شدن سرویس",
    "pg_user_reset_usage": "صفر شدن مصرف",
    "pg_user_revoke_sub": "تغییر لینک اشتراک",
    "pg_user_delete": "حذف سرویس از پنل",
}
# Automatic cleanup notices; their reason is the deletion date.
CLEANUP_ACTION_LABELS: dict[str, str] = {
    "cleanup_expired": "منقضی شدن سرویس",
    "cleanup_warning": "هشدار حذف سرویس",
    "cleanup_volume_finished": "تمام شدن حجم",
    "cleanup_volume_warning": "هشدار حذف سرویس بی‌حجم",
    "cleanup_deleted": "حذف پس از انقضا",
    "cleanup_volume_deleted": "حذف پس از اتمام حجم",
    "cleanup_detached": "جدا شدن سرویس قبلی از لیست پس از پایان مهلت",
}

STAFF_ACTION_VARIABLES = frozenset({"service_name", "reason", "shop_title"})
CLEANUP_VARIABLES = frozenset({"service_name", "delete_date", "shop_title"})
_STAFF_HELP = "خالی = متن پیش‌فرض. متغیرها: {service_name} {reason} {shop_title}"
_CLEANUP_HELP = "خالی = متن پیش‌فرض. متغیرها: {service_name} {delete_date} {shop_title}"

STAFF_ACTION_GROUP = "متن اعلان کار کارکنان روی سرویس"
CLEANUP_GROUP = "متن اعلان پاکسازی سرویس"


def notice_text_key(action: str) -> str:
    """The setting that holds the template of one notice."""
    return f"{KEY_PREFIX}{action}"


NOTICE_TEXT_KEYS: frozenset[str] = frozenset(
    notice_text_key(action) for action in (*STAFF_ACTION_LABELS, *CLEANUP_ACTION_LABELS)
)


def notice_variables(key: str) -> frozenset[str]:
    """Placeholders a notice template may use."""
    action = key.removeprefix(KEY_PREFIX)
    return CLEANUP_VARIABLES if action in CLEANUP_ACTION_LABELS else STAFF_ACTION_VARIABLES


def web_setting_groups() -> dict[str, list[WebField]]:
    """The two web settings groups of the notifications tab."""
    return {
        STAFF_ACTION_GROUP: [
            (notice_text_key(action), label, "textarea", _STAFF_HELP) for action, label in STAFF_ACTION_LABELS.items()
        ],
        CLEANUP_GROUP: [
            (notice_text_key(action), label, "textarea", _CLEANUP_HELP) for action, label in CLEANUP_ACTION_LABELS.items()
        ],
    }


STAFF_ACTION_BOT_FIELDS: list[BotField] = [
    (notice_text_key(action), label, "textarea") for action, label in STAFF_ACTION_LABELS.items()
]
CLEANUP_BOT_FIELDS: list[BotField] = [
    (notice_text_key(action), label, "textarea") for action, label in CLEANUP_ACTION_LABELS.items()
]


def notice_text_defaults() -> dict[str, str]:
    """Empty settings: every notice keeps its built-in text until edited."""
    return {key: "" for key in NOTICE_TEXT_KEYS}
