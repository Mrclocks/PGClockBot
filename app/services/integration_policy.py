"""Typed integration boundaries, scope declarations and safe domain errors."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum


class ScopeKind(str, Enum):
    PLATFORM = "platform"
    SHOP = "shop"


@dataclass(frozen=True, slots=True)
class IntegrationShop:
    kind: ScopeKind
    shop_id: int | None

    def __post_init__(self) -> None:
        valid_platform = self.kind == ScopeKind.PLATFORM and self.shop_id is None
        valid_shop = self.kind == ScopeKind.SHOP and type(self.shop_id) is int and self.shop_id > 0
        if not (valid_platform or valid_shop):
            raise IntegrationError("permission_denied")


class IntegrationError(ValueError):
    """Only fixed classifications may cross the API boundary."""

    def __init__(self, code: str) -> None:
        self.code = code if code in ERROR_MESSAGES else "internal_error"
        super().__init__(ERROR_MESSAGES[self.code])


ERROR_MESSAGES: dict[str, str] = {
    "secure_delivery_required": "نمایش کلید خصوصی فقط از طریق اتصال امن HTTPS مجاز است.",
    "already_submitted": "این فرم قبلاً ارسال شده است. نتیجه را در جزئیات بررسی کنید؛ برای کلید گم‌شده، اعتبارنامه را باطل کنید و سپس فرم تازه‌ای بگیرید.",
    "confirmation_required": "پیامد این تغییر را بخوانید و تأیید کنید.",
    "invalid_request": "درخواست معتبر نیست.",
    "unsupported_authentication": "روش احراز هویت پشتیبانی نمی‌شود.",
    "invalid_credential": "اعتبارنامه معتبر نیست.",
    "api_disabled": "رابط یکپارچه‌سازی غیرفعال است.",
    "integration_disabled": "یکپارچه‌سازی غیرفعال است.",
    "principal_inactive": "حساب مجاز و فعال در دسترس نیست.",
    "permission_denied": "دسترسی مجاز نیست.",
    "customer_blocked": "حساب مشتری مسدود است.",
    "website_entry_not_enabled": "ثبت مشتری از وب‌سایت تأیید نشده است.",
    "customer_reference_conflict": "این شناسه مشتری با اطلاعات دیگری ثبت شده است.",
    "not_found": "منبع یافت نشد.",
    "addon_ineligible": "بسته برای این سرویس مجاز نیست.",
    "plan_ineligible": "پلن برای این یکپارچه‌سازی مجاز نیست.",
    "authorization_unavailable": "بررسی مجوز در حال حاضر ممکن نیست.",
    "rate_limited": "تعداد درخواست‌ها بیش از حد مجاز است. کمی بعد دوباره تلاش کنید.",
    "security_unavailable": "بررسی امنیت درخواست در حال حاضر ممکن نیست.",
    "external_reference_conflict": "این شناسه عملیات با اطلاعات دیگری ثبت شده است.",
    "service_busy": "تغییر دیگری برای این سرویس در حال انجام یا بررسی است.",
    "terms_changed": "شرایط فنی تغییر کرده است؛ فهرست را دوباره بررسی کنید.",
    "quota_exceeded": "ظرفیت فنی کافی نیست.",
    "billing_blocked": "اجازه ایجاد تعهد جدید در این حساب وجود ندارد.",
    "temporarily_unavailable": "پردازش در حال حاضر ممکن نیست.",
    "operation_archived": "این عملیات بایگانی شده است.",
    "access_not_ready": "اطلاعات اتصال هنوز آماده دریافت نیست.",
    "queue_expired": "مهلت درخواست پیش از اجرا تمام شده است.",
    "outcome_unknown": "نتیجه عملیات نیازمند بررسی است؛ درخواست تازه ثبت نکنید.",
    "payload_too_large": "حجم درخواست بیش از حد مجاز است.",
    "unsupported_media_type": "قالب درخواست پشتیبانی نمی‌شود.",
    "unsupported_terms": "شرایط فنی این سرویس قابل تأیید نیست.",
    "service_identity_mismatch": "هویت سرویس نیازمند بررسی است.",
    "panel_unavailable": "اطلاعات پنل در حال حاضر در دسترس نیست.",
    "invalid_cursor": "نشانگر صفحه معتبر نیست.",
    "idempotency_key_required": "کلید تکرار درخواست معتبر لازم است.",
    "idempotency_conflict": "کلید درخواست با اطلاعات دیگری استفاده شده است.",
    "link_conflict": "پیوند دیگری وجود دارد؛ ابتدا پیوند قبلی را لغو کنید.",
    "challenge_expired": "مهلت تأیید پیوند تمام شده است.",
    "link_not_confirmed": "تأیید هر دو حساب لازم است.",
    "queue_limit_reached": "تعداد درخواست‌های در انتظار بیش از حد مجاز است.",
    "internal_error": "خطای داخلی. دوباره تلاش کنید.",
}

SCOPE_FEATURES: dict[str, tuple[str, ...]] = {
    "capabilities:read": (),
    "plans:read": ("integration_plans_read", "plans"),
    "customers:read": ("integration_customers_read",),
    "customers:write": ("integration_customers_write",),
    "services:read": ("integration_services_read",),
    "services:provision": ("integration_services_provision", "plans"),
    "services:renew": ("integration_services_renew", "plans"),
    "services:addons": ("integration_services_addons", "plans"),
    "services:access": ("integration_service_access", "integration_services_read"),
    "operations:read": ("integration_operations_read",),
    "telegram:link": ("integration_telegram_link",),
}
PG_SCOPE_ACTIONS: dict[str, tuple[str, ...]] = {
    "plans:read": ("read",),
    "services:read": ("read",),
    "services:provision": ("read", "create"),
    "services:renew": ("read", "update"),
    "services:addons": ("read", "update"),
    "services:access": ("read",),
}
INTEGRATION_FEATURE_PERMS = [
    ("integrations_manage", "مدیریت یکپارچه‌سازی‌ها"),
    ("integration_plans_read", "مشاهده پلن‌های یکپارچه‌سازی"),
    ("integration_customers_read", "مشاهده مشتریان یکپارچه‌سازی"),
    ("integration_customers_write", "ثبت مشتریان یکپارچه‌سازی"),
    ("integration_services_read", "مشاهده سرویس‌های یکپارچه‌سازی"),
    ("integration_services_provision", "ایجاد سرویس یکپارچه‌سازی"),
    ("integration_services_renew", "تمدید سرویس یکپارچه‌سازی"),
    ("integration_services_addons", "افزونه‌های سرویس یکپارچه‌سازی"),
    ("integration_service_access", "دریافت خصوصی اطلاعات اتصال"),
    ("integration_operations_read", "مشاهده عملیات یکپارچه‌سازی"),
    ("integration_telegram_link", "پیوند تلگرام یکپارچه‌سازی"),
    ("integration_operations_reconcile", "بررسی عملیات یکپارچه‌سازی"),
    ("integration_webhooks_manage", "مدیریت وب‌هوک یکپارچه‌سازی"),
]


def validate_scopes(scopes: frozenset[str]) -> frozenset[str]:
    if not isinstance(scopes, frozenset) or not scopes <= SCOPE_FEATURES.keys():
        raise IntegrationError("invalid_request")
    return scopes


def stored_scopes(raw: str) -> frozenset[str]:
    return validate_scopes(frozenset(raw.split(",")) if raw else frozenset())


def utc_time(value: datetime) -> datetime:
    """SQLite returns naive values for UTC DateTime columns."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
