"""Central catalog of bot message placeholders (متغیرهای پیام).

Operator-editable texts use ``{snake_case}`` only. Resolution always goes through
``safe_format`` — never ``str.format``. Domains limit which keys may resolve so
a payment template cannot pull user/admin secrets by accident.

Security contract:
- Unknown placeholders stay literal (no crash, no probing).
- Attribute / index format syntax is never interpreted (``safe_format``).
- User-controlled values are HTML-escaped when rendering Telegram HTML bodies.
- URL-style templates (gateway_link, naming) substitute without HTML escape.
- Catalog UI never exposes tokens, ADMIN_IDS, passwords, or env secrets.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Any, Mapping

from app.services.safe_format import safe_format

# Domains = where a variable is allowed to resolve.
DOMAIN_SHOP = "shop"
DOMAIN_USER = "user"
DOMAIN_ORDER = "order"
DOMAIN_PAYMENT = "payment"
DOMAIN_WALLET = "wallet"
DOMAIN_REFERRAL = "referral"
DOMAIN_FORCE_JOIN = "force_join"
DOMAIN_NAMING = "naming"
DOMAIN_QR = "qr"
DOMAIN_DAILY_REPORT = "daily_report"

DOMAIN_LABELS_FA: dict[str, str] = {
    DOMAIN_SHOP: "فروشگاه (سراسری)",
    DOMAIN_USER: "کاربر",
    DOMAIN_ORDER: "سفارش و سرویس",
    DOMAIN_PAYMENT: "پرداخت",
    DOMAIN_WALLET: "کیف پول",
    DOMAIN_REFERRAL: "دعوت دوستان",
    DOMAIN_FORCE_JOIN: "عضویت اجباری",
    DOMAIN_NAMING: "نام‌گذاری پاسارگارد",
    DOMAIN_QR: "کپشن QR",
    DOMAIN_DAILY_REPORT: "گزارش روزانه",
}

# Relative to bot settings base, or absolute panel path for modal-hosted texts.
DOMAIN_SETTINGS_PATH: dict[str, str] = {
    DOMAIN_SHOP: "?tab=welcome",
    DOMAIN_USER: "?tab=welcome",
    DOMAIN_ORDER: "?tab=messages",
    DOMAIN_WALLET: "?tab=messages",
    DOMAIN_PAYMENT: "/finance?tab=orders&settings=payment",
    DOMAIN_REFERRAL: "/loyalty?settings=referral",
    DOMAIN_FORCE_JOIN: "?tab=forcejoin",
    DOMAIN_NAMING: "?tab=naming",
    DOMAIN_QR: "?tab=qr",
    DOMAIN_DAILY_REPORT: "?tab=daily_report",
}


def domain_settings_href(domain: str, settings_base: str) -> str:
    """Deep-link to the place that edits texts for this domain."""
    path = DOMAIN_SETTINGS_PATH.get(domain) or ""
    if not path:
        return settings_base
    if path.startswith("/"):
        return path
    return f"{settings_base}{path}"

# Setting keys → domain used when rendering that field.
SETTING_DOMAIN: dict[str, str] = {
    "welcome_text": DOMAIN_USER,
    "purchase_success_text": DOMAIN_ORDER,
    "wallet_success_text": DOMAIN_WALLET,
    "referral_text": DOMAIN_REFERRAL,
    "qr_caption": DOMAIN_QR,
    "card_pay_text": DOMAIN_PAYMENT,
    "gateway_pay_text": DOMAIN_PAYMENT,
    "gateway_link": DOMAIN_PAYMENT,
    "crypto_pay_text": DOMAIN_PAYMENT,
    "psp_pay_text": DOMAIN_PAYMENT,
    "bbc_pay_text": DOMAIN_PAYMENT,
    "force_join_msg": DOMAIN_FORCE_JOIN,
    "pg_username_pattern": DOMAIN_NAMING,
    "custom_plan_username_pattern": DOMAIN_NAMING,
    "admin_daily_report_template": DOMAIN_DAILY_REPORT,
}

# Values actually supplied by each field's renderer, not the entire domain.
# Naming uses the catalog directly; daily reports additionally filter by role.
_SETTING_CONTEXT_KEYS: dict[str, frozenset[str]] = {
    "welcome_text": frozenset({"user_name", "user_id", "username", "shop_title"}),
    "purchase_success_text": frozenset({"order_id", "plan_name", "plan_type", "shop_title", "url"}),
    "wallet_success_text": frozenset({"amount", "payment_id", "shop_title"}),
    "referral_text": frozenset({"code", "link", "shop_title"}),
    "qr_caption": frozenset({"url"}),
    "card_pay_text": frozenset({"amount", "card", "holder", "payment_id", "shop_title"}),
    "gateway_pay_text": frozenset({"amount", "order_id", "payment_id", "gateway_name", "shop_title"}),
    "gateway_link": frozenset({"amount", "order_id", "payment_id"}),
    "crypto_pay_text": frozenset({"amount", "asset", "network", "address", "payment_id", "shop_title"}),
    "psp_pay_text": frozenset({"amount", "order_id", "payment_id", "shop_title"}),
    "bbc_pay_text": frozenset(
        {"amount", "unique_amount", "order_id", "payment_id", "shop_title"}
    ),
    "force_join_msg": frozenset({"channels"}),
}


@dataclass(frozen=True)
class MessageVar:
    key: str
    title_fa: str
    description_fa: str
    example: str
    domains: frozenset[str]
    aliases: tuple[str, ...] = ()
    html_escape: bool = True
    owner_only: bool = False  # hide from reseller catalog when True


_VARS: tuple[MessageVar, ...] = (
    MessageVar(
        key="shop_title",
        title_fa="نام فروشگاه",
        description_fa="عنوان فروشگاه از تنظیمات (همان مقداری که در هدر خوش‌آمد می‌آید).",
        example="کلاک بات",
        domains=frozenset({DOMAIN_SHOP, DOMAIN_USER, DOMAIN_ORDER, DOMAIN_PAYMENT, DOMAIN_WALLET, DOMAIN_REFERRAL}),
    ),
    MessageVar(
        key="bot_username",
        title_fa="یوزرنیم ربات",
        description_fa="یوزرنیم ربات فعلی بدون @. فقط وقتی در context پاس شده باشد.",
        example="PGClockBot",
        domains=frozenset({DOMAIN_SHOP, DOMAIN_USER, DOMAIN_REFERRAL}),
        html_escape=True,
    ),
    MessageVar(
        key="user_name",
        title_fa="نام کاربر",
        description_fa="نام نمایشی تلگرام کاربر. برای سازگاری قدیمی، {name} هم همین مقدار را می‌گیرد.",
        example="علی",
        domains=frozenset({DOMAIN_USER}),
        aliases=("name",),
        html_escape=True,
    ),
    MessageVar(
        key="user_id",
        title_fa="آیدی عددی کاربر",
        description_fa="شناسهٔ عددی تلگرام کاربر. دادهٔ حساس ادمین نیست؛ فقط شناسهٔ همان کاربر.",
        example="123456789",
        domains=frozenset({DOMAIN_USER}),
        html_escape=False,
    ),
    MessageVar(
        key="username",
        title_fa="یوزرنیم کاربر",
        description_fa="یوزرنیم تلگرام کاربر؛ در پیام‌ها با @ و در نام سرویس بدون @. اگر نداشت در نام سرویس آیدی عددی جایگزین می‌شود.",
        example="@ali",
        domains=frozenset({DOMAIN_USER, DOMAIN_NAMING}),
        html_escape=True,
    ),
    MessageVar(
        key="order_id",
        title_fa="شماره سفارش",
        description_fa="شناسهٔ سفارش در دیتابیس ربات.",
        example="1024",
        domains=frozenset({DOMAIN_ORDER, DOMAIN_PAYMENT}),
        html_escape=False,
    ),
    MessageVar(
        key="plan_name",
        title_fa="نام پلن",
        description_fa="نام پلنی که خریداری شده (وقتی در context سفارش باشد).",
        example="یک‌ماهه ۵۰ گیگ",
        domains=frozenset({DOMAIN_ORDER}),
        html_escape=True,
    ),
    MessageVar(
        key="plan_type",
        title_fa="نوع پلن",
        description_fa="نوع سفارش: ثابت، تست، دلخواه، فروش عمده، تمدید، بسته حجم/زمان، …",
        example="ثابت",
        domains=frozenset({DOMAIN_ORDER}),
        html_escape=True,
    ),
    MessageVar(
        key="amount",
        title_fa="مبلغ",
        description_fa="مبلغ فرمت‌شده با واحد پول پنل (مثلاً ۵۰٬۰۰۰ تومان).",
        example="۵۰٬۰۰۰ تومان",
        domains=frozenset({DOMAIN_PAYMENT, DOMAIN_WALLET}),
        html_escape=True,
    ),
    MessageVar(
        key="unique_amount",
        title_fa="مبلغ یکتای واریز",
        description_fa="مبلغ دقیق کارت‌به‌کارت بزن به کارت (ریال یکتا). مشتری باید دقیقاً همین را واریز کند.",
        example="۲٬۰۰۰٬۰۴۳ ریال",
        domains=frozenset({DOMAIN_PAYMENT}),
        html_escape=True,
    ),
    MessageVar(
        key="payment_id",
        title_fa="شناسه پرداخت",
        description_fa="شماره رکورد پرداخت. در لینک درگاه و متن موفقیت شارژ قابل استفاده است.",
        example="88",
        domains=frozenset({DOMAIN_PAYMENT, DOMAIN_WALLET}),
        html_escape=False,
    ),
    MessageVar(
        key="gateway_name",
        title_fa="نام درگاه",
        description_fa="عنوان درگاه از تنظیمات پرداخت. برای سازگاری، در راهنمای درگاه {name} هم همین است.",
        example="زرین‌پال",
        domains=frozenset({DOMAIN_PAYMENT}),
        aliases=("name",),
        html_escape=True,
    ),
    MessageVar(
        key="card",
        title_fa="شماره کارت",
        description_fa="شماره کارت ذخیره‌شده در تنظیمات پرداخت.",
        example="6037-****-****-1234",
        domains=frozenset({DOMAIN_PAYMENT}),
        html_escape=True,
    ),
    MessageVar(
        key="holder",
        title_fa="صاحب حساب",
        description_fa="نام دارنده کارت از تنظیمات.",
        example="علی رضایی",
        domains=frozenset({DOMAIN_PAYMENT}),
        html_escape=True,
    ),
    MessageVar(
        key="asset",
        title_fa="رمزارز",
        description_fa="نماد دارایی (مثلاً USDT).",
        example="USDT",
        domains=frozenset({DOMAIN_PAYMENT}),
        html_escape=True,
    ),
    MessageVar(
        key="network",
        title_fa="شبکه رمزارز",
        description_fa="شبکه انتقال (مثلاً TRC20).",
        example="TRC20",
        domains=frozenset({DOMAIN_PAYMENT}),
        html_escape=True,
    ),
    MessageVar(
        key="address",
        title_fa="آدرس کیف رمزارز",
        description_fa="آدرس دریافت از تنظیمات. فقط در متن راهنمای رمزارز.",
        example="TXyz…",
        domains=frozenset({DOMAIN_PAYMENT}),
        html_escape=True,
    ),
    MessageVar(
        key="code",
        title_fa="کد دعوت",
        description_fa="کد دعوت اختصاصی کاربر.",
        example="CLK42",
        domains=frozenset({DOMAIN_REFERRAL}),
        html_escape=True,
    ),
    MessageVar(
        key="link",
        title_fa="لینک دعوت",
        description_fa="لینک t.me ربات با پارامتر start کد دعوت.",
        example="https://t.me/bot?start=CLK42",
        domains=frozenset({DOMAIN_REFERRAL}),
        html_escape=True,
    ),
    MessageVar(
        key="url",
        title_fa="لینک اشتراک",
        description_fa="آدرس سابسکریپشن سرویس برای کپشن QR. معادل مفهومی {sub_link}.",
        example="https://example.com/sub/…",
        domains=frozenset({DOMAIN_QR, DOMAIN_ORDER}),
        aliases=("sub_link",),
        html_escape=True,
    ),
    MessageVar(
        key="channels",
        title_fa="لیست کانال‌ها",
        description_fa="بولت‌لیست کانال‌هایی که عضویت‌شان لازم است. فقط در پیام عضویت اجباری.",
        example="• @channel",
        domains=frozenset({DOMAIN_FORCE_JOIN}),
        html_escape=True,
    ),
    MessageVar(
        key="prefix",
        title_fa="پیشوند نام کاربری",
        description_fa="پیشوند الگوی ساخت یوزرنیم پاسارگارد.",
        example="clk",
        domains=frozenset({DOMAIN_NAMING}),
        html_escape=False,
        owner_only=True,
    ),
    MessageVar(
        key="plan_volume",
        title_fa="حجم عددی پلن",
        description_fa="حجم پلن بدون واحد؛ برای حجم نامحدود مقدار unlimited استفاده می‌شود.",
        example="30",
        domains=frozenset({DOMAIN_NAMING}),
        html_escape=False,
        owner_only=True,
    ),
    MessageVar(
        key="plan_unit",
        title_fa="واحد حجم پلن",
        description_fa="واحد حجم پلن به صورت GB، جدا از مقدار عددی حجم.",
        example="GB",
        domains=frozenset({DOMAIN_NAMING}),
        html_escape=False,
        owner_only=True,
    ),
    MessageVar(
        key="random",
        title_fa="بخش تصادفی",
        description_fa="رشته تصادفی امن برای یکتا بودن نام کاربری.",
        example="a7k2",
        domains=frozenset({DOMAIN_NAMING}),
        html_escape=False,
        owner_only=True,
    ),
    MessageVar(
        key="suffix",
        title_fa="پسوند نام کاربری",
        description_fa="پسوند الگوی ساخت یوزرنیم.",
        example="",
        domains=frozenset({DOMAIN_NAMING}),
        html_escape=False,
        owner_only=True,
    ),
    MessageVar(
        key="id",
        title_fa="شناسه سفارش در نام",
        description_fa="شناسهٔ سفارش داخل الگوی نام‌گذاری پاسارگارد.",
        example="1024",
        domains=frozenset({DOMAIN_NAMING}),
        html_escape=False,
        owner_only=True,
    ),
)

# Daily-report placeholders — shared catalog with settings tab (role-filtered via owner_only).
try:
    from app.services.daily_report import ACTOR_OWNER, ACTOR_SHOP, ALL_METRICS

    _DAILY_VARS: tuple[MessageVar, ...] = tuple(
        MessageVar(
            key=m.key,
            title_fa=m.title_fa,
            description_fa=m.description_fa,
            example=m.example,
            domains=frozenset({DOMAIN_DAILY_REPORT}),
            html_escape=True,
            owner_only=(ACTOR_SHOP not in m.actors and ACTOR_OWNER in m.actors),
        )
        for m in ALL_METRICS
    )
    _VARS = (*_VARS, *_DAILY_VARS)
except Exception:  # pragma: no cover — catalog still loads if daily_report import fails
    pass

_BY_KEY: dict[str, MessageVar] = {v.key: v for v in _VARS}
_ALIAS_TO_CANONICAL: dict[str, str] = {}
for _v in _VARS:
    for _a in _v.aliases:
        # First canonical wins; domain-specific alias resolution happens at render.
        _ALIAS_TO_CANONICAL.setdefault(_a, _v.key)


def all_vars(*, include_owner_only: bool = True) -> list[MessageVar]:
    if include_owner_only:
        return list(_VARS)
    return [v for v in _VARS if not v.owner_only]


def vars_for_domain(domain: str, *, include_owner_only: bool = True) -> list[MessageVar]:
    out = []
    for v in all_vars(include_owner_only=include_owner_only):
        if domain in v.domains:
            out.append(v)
    return out


def catalog_groups(
    *,
    include_owner_only: bool = True,
    settings_base: str = "/settings",
) -> list[dict[str, Any]]:
    """UI-ready groups: domain label + variables + settings deep-link."""
    groups: list[dict[str, Any]] = []
    for domain, label in DOMAIN_LABELS_FA.items():
        items = vars_for_domain(domain, include_owner_only=include_owner_only)
        if not items:
            continue
        groups.append(
            {
                "domain": domain,
                "label": label,
                "settings_href": domain_settings_href(domain, settings_base),
                "vars": [
                    {
                        "key": v.key,
                        "token": "{" + v.key + "}",
                        "title_fa": v.title_fa,
                        "description_fa": v.description_fa,
                        "example": v.example,
                        "aliases": ["{" + a + "}" for a in v.aliases],
                    }
                    for v in items
                ],
            }
        )
    return groups


def allowed_keys_for_domain(domain: str) -> frozenset[str]:
    keys: set[str] = set()
    for v in _VARS:
        if domain not in v.domains:
            continue
        keys.add(v.key)
        keys.update(v.aliases)
    return frozenset(keys)


def template_variable_specs(actor: str = "owner") -> dict[str, dict[str, str]]:
    """Panel field -> supported placeholder names and their Persian titles."""
    specs: dict[str, dict[str, str]] = {}
    for setting, domain in SETTING_DOMAIN.items():
        context_keys = _SETTING_CONTEXT_KEYS.get(setting)
        include_owner_only = domain != DOMAIN_DAILY_REPORT or actor == "owner"
        names: dict[str, str] = {}
        for var in vars_for_domain(domain, include_owner_only=include_owner_only):
            if context_keys is not None and var.key not in context_keys:
                continue
            for key in (var.key, *var.aliases):
                names[key] = var.title_fa
        specs[setting] = names
    return specs


def _escape_if_needed(key: str, value: Any, *, html_mode: bool) -> str:
    if value is None:
        return ""
    text = str(value)
    if not html_mode:
        return text
    meta = _BY_KEY.get(key)
    if meta is None:
        # Alias: find canonical that owns this alias
        for v in _VARS:
            if key in v.aliases:
                meta = v
                break
    if meta is None or not meta.html_escape:
        return text
    return html.escape(text, quote=False)


def render_message_template(
    template: str | None,
    *,
    domain: str,
    values: Mapping[str, Any] | None = None,
    html: bool = True,
    **kwargs: Any,
) -> str:
    """Substitute placeholders allowed for *domain* only.

    Extra kwargs/values outside the domain allowlist are ignored (no leak).
    """
    raw = template or ""
    if not raw:
        return ""
    allowed = allowed_keys_for_domain(domain)
    merged: dict[str, Any] = {}
    if values:
        merged.update(values)
    merged.update(kwargs)

    # Expand aliases: if caller passed user_name, also fill name when allowed.
    for v in _VARS:
        if domain not in v.domains:
            continue
        if v.key in merged:
            for alias in v.aliases:
                if alias in allowed and alias not in merged:
                    merged[alias] = merged[v.key]
        for alias in v.aliases:
            if alias in merged and v.key not in merged and v.key in allowed:
                merged[v.key] = merged[alias]

    filtered: dict[str, Any] = {}
    for key, val in merged.items():
        if key not in allowed:
            continue
        filtered[key] = _escape_if_needed(key, val, html_mode=html)
    return safe_format(raw, filtered)


def preview_fill_map() -> dict[str, str]:
    """Sample values for Telegram settings live preview (no secrets)."""
    out: dict[str, str] = {}
    for v in _VARS:
        out["{" + v.key + "}"] = v.example
        for a in v.aliases:
            out.setdefault("{" + a + "}", v.example)
    return out
