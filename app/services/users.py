from __future__ import annotations

import json
import secrets
import string
import time
from contextvars import ContextVar
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import BotUser, Role, Setting

# When handling updates on a reseller-owned bot, settings read/write overlay
# that reseller's shop settings automatically.
_shop_reseller_id: ContextVar[int | None] = ContextVar("shop_reseller_id", default=None)

# Short TTL cache: reseller_id (0=global) → (monotonic_at, settings dict)
_SETTINGS_CACHE: dict[int, tuple[float, dict[str, str]]] = {}
_SETTINGS_CACHE_TTL = 15.0


def clear_settings_cache(reseller_id: int | None = None) -> None:
    """Drop cached settings. Clear all when global keys change."""
    if reseller_id is None:
        _SETTINGS_CACHE.clear()
        return
    _SETTINGS_CACHE.pop(int(reseller_id), None)


def _split_channel_tokens(raw: str | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for part in (raw or "").replace(",", "\n").splitlines():
        ch, _required = _parse_channel_line(part)
        if not ch:
            continue
        key = ch.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(ch)
    return out


def _parse_channel_line(part: str) -> tuple[str, bool]:
    """Parse one force-join line → (channel_id, required).

    Optional markers (Telegram admin edit / legacy text):
    ``@chan !optional``, ``@chan !opt``, ``@chan (اختیاری)``.
    """
    ch = (part or "").strip()
    if not ch:
        return "", True
    required = True
    lower = ch.lower()
    for marker in ("!optional", "!opt"):
        if lower.endswith(marker):
            ch = ch[: -len(marker)].strip()
            required = False
            break
    if ch.endswith("(اختیاری)"):
        ch = ch[: -len("(اختیاری)")].strip()
        required = False
    return ch, required


def parse_force_join_entries(raw: str | None) -> list[dict[str, Any]]:
    """Parse force-join setting into [{id, required}, ...].

    Accepts JSON array (new) or legacy line/comma-separated channels (all required
    unless marked ``!optional``).
    """
    s = (raw or "").strip()
    if not s:
        return []
    if s.startswith("["):
        try:
            data = json.loads(s)
        except Exception:
            data = None
        if isinstance(data, list):
            out: list[dict[str, Any]] = []
            seen: set[str] = set()
            for item in data:
                if isinstance(item, str):
                    ch, required = _parse_channel_line(item)
                elif isinstance(item, dict):
                    ch = str(item.get("id") or item.get("channel") or "").strip()
                    req = item.get("required", True)
                    if isinstance(req, str):
                        required = req.strip().lower() in {"1", "true", "yes", "on"}
                    else:
                        required = bool(req)
                else:
                    continue
                if not ch:
                    continue
                key = ch.lower()
                if key in seen:
                    continue
                seen.add(key)
                out.append({"id": ch, "required": required})
            return out
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for part in s.replace(",", "\n").splitlines():
        ch, required = _parse_channel_line(part)
        if not ch:
            continue
        key = ch.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"id": ch, "required": required})
    return out


def parse_force_join_channels(raw: str | None) -> list[str]:
    """Channel ids that must be joined (required=true) before using the bot."""
    return [e["id"] for e in parse_force_join_entries(raw) if e.get("required", True)]


def serialize_force_join_entries(entries: list[dict[str, Any]] | None) -> str:
    """Normalize entries to a compact JSON string for storage."""
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in entries or []:
        if not isinstance(item, dict):
            continue
        ch = str(item.get("id") or "").strip()
        if not ch:
            continue
        key = ch.lower()
        if key in seen:
            continue
        seen.add(key)
        req = item.get("required", True)
        if isinstance(req, str):
            required = req.strip().lower() in {"1", "true", "yes", "on"}
        else:
            required = bool(req)
        normalized.append({"id": ch, "required": required})
    return json.dumps(normalized, ensure_ascii=False)


def format_force_join_for_edit(raw: str | None) -> str:
    """Human-editable multi-line preview for bot settings (keeps optional flags)."""
    lines: list[str] = []
    for e in parse_force_join_entries(raw):
        ch = e["id"]
        if e.get("required", True):
            lines.append(ch)
        else:
            lines.append(f"{ch} !optional")
    return "\n".join(lines)

def normalize_force_join_channel_value(raw: str | None) -> str:
    """Accept JSON or legacy lines; always store JSON entries (required defaults true)."""
    return serialize_force_join_entries(parse_force_join_entries(raw))


def set_shop_reseller_id(reseller_user_id: int | None):
    return _shop_reseller_id.set(reseller_user_id)


def reset_shop_reseller_id(token) -> None:
    _shop_reseller_id.reset(token)


def current_shop_reseller_id() -> int | None:
    return _shop_reseller_id.get()


def current_ui_snapshot() -> dict[str, str]:
    """Best-effort settings snapshot for sync keyboard builders.

    Prefers the per-request shop cache warmed by ``get_all_settings``;
    falls back to defaults when the cache is cold.
    """
    rid = _shop_reseller_id.get()
    cache_key = int(rid or 0)
    hit = _SETTINGS_CACHE.get(cache_key)
    if hit:
        return dict(hit[1])
    return dict(DEFAULT_SETTINGS)


def _effective_reseller_id(reseller_id: int | None) -> int | None:
    if reseller_id is not None:
        return reseller_id
    return _shop_reseller_id.get()


def _referral_code() -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(8))


async def get_or_create_user(
    session: AsyncSession,
    telegram_id: int,
    *,
    username: str | None = None,
    full_name: str | None = None,
    referred_by_code: str | None = None,
    reseller_owner_id: int | None = None,
) -> BotUser:
    result = await session.execute(
        select(BotUser).where(BotUser.telegram_id == telegram_id)
    )
    user = result.scalar_one_or_none()
    settings = get_settings()
    is_admin = telegram_id in settings.admin_ids

    if user:
        changed = False
        if username and user.username != username:
            user.username = username
            changed = True
        if full_name and user.full_name != full_name:
            user.full_name = full_name
            changed = True
        if is_admin and user.role != Role.ADMIN.value:
            user.role = Role.ADMIN.value
            changed = True
        # Demote sticky ADMIN when removed from ADMIN_IDS (unless still a reseller)
        elif (
            not is_admin
            and user.role == Role.ADMIN.value
            and telegram_id not in settings.admin_ids
        ):
            from app.db.models import ResellerProfile

            profile = (
                await session.execute(
                    select(ResellerProfile).where(ResellerProfile.user_id == user.id)
                )
            ).scalar_one_or_none()
            if profile and profile.is_active:
                user.role = Role.RESELLER.value
            else:
                user.role = Role.USER.value
            changed = True
        # Sticky first-touch attribution for reseller shop bots
        if (
            reseller_owner_id
            and user.reseller_id is None
            and user.id != reseller_owner_id
            and user.role == Role.USER.value
        ):
            user.reseller_id = reseller_owner_id
            changed = True
        if changed:
            await session.commit()
            await session.refresh(user)
        return user

    referred_by_id = None
    if referred_by_code:
        ref = await session.execute(
            select(BotUser).where(BotUser.referral_code == referred_by_code.upper())
        )
        referrer = ref.scalar_one_or_none()
        if referrer and referrer.telegram_id != telegram_id:
            referred_by_id = referrer.id

    assign_reseller = None
    if reseller_owner_id and not is_admin:
        assign_reseller = reseller_owner_id

    user = BotUser(
        telegram_id=telegram_id,
        username=username,
        full_name=full_name,
        role=Role.ADMIN.value if is_admin else Role.USER.value,
        referral_code=_referral_code(),
        referred_by_id=referred_by_id,
        reseller_id=assign_reseller,
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        result = await session.execute(
            select(BotUser).where(BotUser.telegram_id == telegram_id)
        )
        existing = result.scalar_one_or_none()
        if existing:
            return existing
        raise
    await session.refresh(user)
    # Don't attribute the reseller owner to themselves
    if assign_reseller and user.id == assign_reseller:
        user.reseller_id = None
        await session.commit()
        await session.refresh(user)
    if referred_by_id:
        try:
            from app.services.loyalty import on_user_referred

            await on_user_referred(session, referred=user, source="telegram")
        except Exception:
            pass
    return user


async def get_setting(
    session: AsyncSession,
    key: str,
    default: str = "",
    *,
    reseller_id: int | None = None,
) -> str:
    rid = _effective_reseller_id(reseller_id)
    if rid:
        from app.db.models import ResellerSetting

        result = await session.execute(
            select(ResellerSetting).where(
                ResellerSetting.reseller_user_id == rid,
                ResellerSetting.key == key,
            )
        )
        row = result.scalar_one_or_none()
        if row is not None:
            return row.value
        # Shop isolation: never fall through to live platform Setting rows
        return DEFAULT_SETTINGS.get(key, default)
    result = await session.execute(select(Setting).where(Setting.key == key))
    row = result.scalar_one_or_none()
    if row:
        return row.value
    return default


async def set_setting(
    session: AsyncSession,
    key: str,
    value: str,
    *,
    reseller_id: int | None = None,
) -> None:
    rid = _effective_reseller_id(reseller_id)
    if rid:
        from app.db.models import ResellerSetting

        result = await session.execute(
            select(ResellerSetting).where(
                ResellerSetting.reseller_user_id == rid,
                ResellerSetting.key == key,
            )
        )
        row = result.scalar_one_or_none()
        if row:
            row.value = value
        else:
            session.add(
                ResellerSetting(reseller_user_id=rid, key=key, value=value)
            )
        await session.commit()
        clear_settings_cache(rid)
        return
    result = await session.execute(select(Setting).where(Setting.key == key))
    row = result.scalar_one_or_none()
    if row:
        row.value = value
    else:
        session.add(Setting(key=key, value=value))
    await session.commit()
    # Only invalidate platform cache — do not flush isolated shop caches
    clear_settings_cache(0)


async def set_settings_bulk(
    session: AsyncSession,
    values: dict[str, str],
    *,
    reseller_id: int | None = None,
) -> None:
    """Upsert many settings with a single commit (avoids lock storms on menu save)."""
    if not values:
        return
    rid = _effective_reseller_id(reseller_id)
    keys = list(values.keys())
    if rid:
        from app.db.models import ResellerSetting

        result = await session.execute(
            select(ResellerSetting).where(
                ResellerSetting.reseller_user_id == rid,
                ResellerSetting.key.in_(keys),
            )
        )
        existing = {r.key: r for r in result.scalars().all()}
        for key, value in values.items():
            row = existing.get(key)
            if row:
                row.value = value
            else:
                session.add(
                    ResellerSetting(reseller_user_id=rid, key=key, value=value)
                )
        await session.commit()
        clear_settings_cache(rid)
        return
    result = await session.execute(select(Setting).where(Setting.key.in_(keys)))
    existing = {r.key: r for r in result.scalars().all()}
    for key, value in values.items():
        row = existing.get(key)
        if row:
            row.value = value
        else:
            session.add(Setting(key=key, value=value))
    await session.commit()
    clear_settings_cache(0)


DEFAULT_SETTINGS = {
    "shop_title": "کلاک بات",
    "welcome_text": (
        "سلام {name} 👋\n\n"
        "به فروشگاه کلاک خوش آمدید.\n"
        "از منوی زیر می‌توانید سرویس بخرید، وضعیت را ببینید و پشتیبانی بگیرید."
    ),
    "card_number": "",
    "card_holder": "",
    "support_text": "پیام خود را بنویسید؛ تیم پشتیبانی پاسخ می‌دهد.",
    "support_contacts": "[]",
    "force_join_channel": "",
    "force_join_enabled": "0",
    "trial_enabled": "0",
    "referral_bonus": "0",
    "loyalty_enabled": "1",
    "points_to_wallet_rate": "100",
    "auto_approve_payments": "0",
    "faq_text": (
        "❓ حجم تمام شد چه کنم؟\nاز بخش سرویس‌ها → تمدید.\n\n"
        "❓ لینک کار نمی‌کند؟\nQR یا لینک را دوباره از سرویس‌های من بگیرید."
    ),
    "guide_text": (
        "۱) سرویس را بخرید و پرداخت را انجام دهید\n"
        "۲) لینک یا QR اشتراک را دریافت کنید\n"
        "۳) در کلاینت (v2rayNG / Streisand / …) لینک را Import کنید"
    ),
    "purchase_success_text": "پرداخت شما تأیید شد و سرویس فعال است.\nشماره سفارش: #{order_id}",
    "delivery_title": "✅ سرویس آماده است",
    "wallet_success_title": "💰 شارژ کیف پول",
    "wallet_success_text": "مبلغ {amount} به کیف پول شما اضافه شد.",
    "payment_ok_title": "✅ پرداخت تأیید شد",
    "payment_reject_text": "پرداخت شما رد شد. اگر اشتباهی رخ داده با پشتیبانی در تماس باشید.",
    "pending_order_cleanup_enabled": "0",
    "pending_order_ttl_hours": "48",
    "pending_order_cleanup_awaiting_approval": "0",
    "card_pay_text": (
        "مبلغ قابل پرداخت: <b>{amount}</b>\n"
        "شماره کارت: <code>{card}</code>\n"
        "به نام: {holder}\n\n"
        "پس از واریز، عکس رسید را در همین گفتگو ارسال کنید."
    ),
    "referral_text": (
        "با دعوت دوستان پاداش بگیرید.\n\n"
        "کد دعوت شما: <code>{code}</code>\n"
        "لینک دعوت:\n{link}"
    ),
    "empty_services_text": "هنوز سرویسی ندارید.\nاز بخش «خرید سرویس» شروع کنید.",
    "shop_empty_text": "در حال حاضر پلنی برای فروش فعال نیست.",
    "qr_enabled": "1",
    "qr_caption": "📱 QR اشتراک\nبا دوربین گوشی اسکن کنید یا در کلاینت Import کنید.",
    "qr_background": "",
    "show_sub_link_in_text": "1",
    # Admin Telegram notification toggles (settings → اعلان‌ها)
    "notify_new_subscription": "1",
    "notify_pending_approval": "1",
    "notify_new_order": "0",
    "notify_wallet_topup": "1",
    "notify_new_ticket": "1",
    "notify_auto_approve": "1",
    "notify_account_edits": "1",
    # User low-remaining alerts (volume / time)
    "user_alert_low_enabled": "0",
    "user_alert_low_traffic_pct": "20",
    "user_alert_low_time_pct": "20",
    # Reseller panel URLs (setup links + messages). Empty = system defaults
    "reseller_panel_base_url": "",
    "reseller_pg_panel_base_url": "",
    # Cached Telegram bot profile (synced via appearance tab → Bot API)
    "bot_tg_name": "",
    "bot_tg_description": "",
    "bot_tg_short_description": "",
    "bot_tg_photo": "",

    "btn_shop": "🟢🛒 خرید سرویس",
    "btn_services": "🔵📦 سرویس‌های من",
    "btn_wallet": "🟡👛 کیف پول",
    "btn_support": "🟣🎧 پشتیبانی",
    "btn_guide": "📘 راهنما",
    "btn_faq": "❓ سوالات متداول",
    "btn_referral": "🎁 دعوت دوستان",
    "btn_reseller_apply": "🤝 درخواست نمایندگی",
    "btn_miniapp": "📱 مینی‌اپ",
    "btn_reseller": "🤝 پنل نماینده",
    "btn_reseller_creds": "🔐 اطلاعات ورود پنل و ربات",
    "btn_admin": "🛠 پنل ادمین",
    "btn_menu_home": "🏠 منوی اصلی",
    "btn_adm_orders": "🛒 سفارش‌ها",
    "btn_adm_payments": "🧾 رسیدها",
    "btn_adm_tickets": "🎫 تیکت‌ها",
    "btn_adm_plans": "📦 پلن‌ها",
    "btn_adm_pg": "🖥 پاسارگارد",
    "btn_adm_users": "👥 کاربران",
    "btn_adm_settings": "⚙️ تنظیمات",
    "btn_adm_broadcast": "📢 پیام گروهی",
    "btn_adm_preview": "👁 پیش‌نمایش منوی کاربر",
    "btn_back": "⬅️ بازگشت",
    "btn_pay_wallet": "🟢👛 پرداخت از کیف پول",
    "btn_pay_card": "🔵💳 کارت به کارت",
    "btn_pay_gateway": "🟢🌐 درگاه پرداخت",
    "btn_pay_crypto": "🟡💎 رمزارز",
    "btn_pay_stars": "⭐ استارز تلگرام",
    "btn_pay_discount": "🏷 کد تخفیف",
    "btn_cancel": "❌ انصراف",
    "btn_renew": "🔄 تمدید",
    "btn_sub_link": "🔗 لینک و QR",
    "show_guide": "0",
    "show_faq": "0",
    "show_referral": "1",
    "show_reseller_apply": "1",
    "show_wallet": "1",
    "show_support": "1",
    "show_miniapp": "1",
    "menu_layout": "compact",
    "menu_order": "shop,services,wallet,support,referral,reseller_apply,miniapp",
    "pg_username_prefix": "clk",
    "pg_username_suffix": "",
    "pg_username_pattern": "{prefix}_{random}{suffix}",
    "custom_plan_enabled": "0",
    "custom_plan_price_per_gb": "1000",
    "custom_plan_price_per_day": "500",
    "custom_plan_min_gb": "1",
    "custom_plan_max_gb": "500",
    "custom_plan_min_days": "1",
    "custom_plan_max_days": "365",
    "custom_plan_template_id": "",
    "custom_plan_group_ids": "",
    "custom_plan_username_prefix": "",
    "custom_plan_username_suffix": "",
    "custom_plan_username_pattern": "",
    # Wholesale / bulk sales
    "wholesale_enabled": "0",
    "wholesale_min_qty": "5",
    "wholesale_max_qty": "20",
    # JSON list: [{"min":5,"percent":10},{"min":20,"percent":20}]
    "wholesale_tiers": '[{"min":5,"percent":10},{"min":20,"percent":20}]',
    "btn_wholesale": "📦 فروش عمده",
    # Payment methods
    "pay_wallet_enabled": "1",
    "pay_card_enabled": "1",
    "pay_gateway_enabled": "0",
    "pay_crypto_enabled": "0",
    "pay_stars_enabled": "0",
    "pay_discount_enabled": "1",
    "gateway_name": "درگاه پرداخت",
    "gateway_link": "",
    "gateway_pay_text": (
        "مبلغ قابل پرداخت: <b>{amount}</b>\n"
        "از دکمه زیر وارد درگاه شوید و پرداخت را انجام دهید.\n"
        "سپس عکس رسید را در همین گفتگو بفرستید."
    ),
    "crypto_asset": "USDT",
    "crypto_network": "TRC20",
    "crypto_address": "",
    "crypto_pay_text": (
        "مبلغ تقریبی سفارش: <b>{amount}</b>\n"
        "رمزارز: <b>{asset}</b> ({network})\n"
        "آدرس ولت:\n<code>{address}</code>\n\n"
        "پس از واریز، عکس رسید/هش تراکنش را در همین گفتگو بفرستید."
    ),
    "stars_toman_per_star": "500",
    "stars_title": "خرید سرویس",
    "stars_description": "پرداخت سفارش با استارز تلگرام",
    # Unified Billing (platform; PAYG resellers)
    "billing_enabled": "0",
    "billing_price_per_gb": "0",
    "billing_low_balance": "10000",
    "billing_on_empty": "block_provision",
    "billing_tick_minutes": "15",
}

# field kinds: text | textarea | toggle | select | number | image
# (key, label, kind, help?, options?)
# Tabs for /settings?tab=... (fewer logical categories — less clutter)
SETTINGS_TABS: list[tuple[str, str]] = [
    ("messages", "پیام‌ها و ظاهر"),
    ("appearance", "ظاهر ربات"),
    ("menu", "منوی بات"),
    ("payment", "پرداخت"),
    ("billing", "کیف پول و PAYG"),
    ("services", "سرویس و فروش"),
    ("notifications", "اعلان‌ها"),
    ("reseller", "نمایندگی"),
    ("supports", "پشتیبانی"),
    ("bot", "ربات و اتصال"),
]

# Legacy tab ids → current (bookmarks / old links / tests)
SETTINGS_TAB_ALIASES: dict[str, str] = {
    "welcome": "messages",
    "buttons": "messages",
    "qr": "messages",
    "naming": "services",
    "forcejoin": "services",
}

# Web-panel settings (sidebar under dashboard — not bot settings tabs)
PANEL_SETTINGS_TABS: list[tuple[str, str]] = [
    ("backup", "بکاپ"),
    ("pwa", "وب‌اپ"),
    ("ssl", "SSL"),
    ("update", "آپدیت"),
]
PANEL_SETTINGS_KEYS = {t[0] for t in PANEL_SETTINGS_TABS}
ALL_SETTINGS_TABS: list[tuple[str, str]] = [*SETTINGS_TABS, *PANEL_SETTINGS_TABS]

SETTING_GROUPS = {
    "خوش‌آمد و هویت": [
        ("shop_title", "نام فروشگاه", "text", "بالای منوی اصلی ربات دیده می‌شود"),
        (
            "welcome_text",
            "پیام خوش‌آمد (/start)",
            "textarea",
            "اولین پیامی که کاربر بعد از استارت می‌بیند. متغیر: {name}",
        ),
    ],
    "متن پیام‌ها": [
        ("guide_text", "متن راهنما", "textarea", "دستور /help در تلگرام"),
        ("faq_text", "متن سوالات متداول", "textarea", "قابل استفاده در پیام‌ها"),
        ("support_text", "متن صفحه پشتیبانی", "textarea", "بالای فرم تیکت نمایش داده می‌شود"),
        ("referral_text", "متن دعوت دوستان", "textarea", "متغیرها: {code} و {link}"),
        ("empty_services_text", "وقتی سرویسی ندارد", "textarea", "پیام بخش سرویس‌های من اگر لیست خالی باشد"),
        ("shop_empty_text", "وقتی پلنی نیست", "textarea", "پیام فروشگاه اگر پلن فعالی نباشد"),
        ("delivery_title", "عنوان پیام تحویل سرویس", "text", "مثلاً: ✅ سرویس آماده است"),
        ("purchase_success_text", "متن موفقیت خرید", "textarea", "متغیر: {order_id} — پیام کوتاه موفقیت (جزئیات روی QR است)"),
        ("wallet_success_text", "متن موفقیت شارژ کیف پول", "textarea", "متغیر: {amount}"),
    ],
    "متن دکمه‌های منو": [
        ("btn_shop", "دکمه خرید", "text", ""),
        ("btn_services", "دکمه سرویس‌ها", "text", ""),
        ("btn_wallet", "دکمه کیف پول", "text", ""),
        ("btn_support", "دکمه پشتیبانی", "text", ""),
        ("btn_guide", "دکمه راهنما", "text", ""),
        ("btn_faq", "دکمه سوالات", "text", ""),
        ("btn_referral", "دکمه دعوت", "text", ""),
        ("btn_reseller_apply", "دکمه درخواست نمایندگی", "text", ""),
        ("btn_miniapp", "دکمه مینی‌اپ", "text", "فقط به‌صورت اینلاین زیر پیام (محدودیت تلگرام)"),
        ("btn_wholesale", "دکمه فروش عمده", "text", "در لیست پلن‌های فروشگاه نمایش داده می‌شود"),
        ("btn_reseller", "دکمه نماینده", "text", "در کیبورد ربات اختصاصی نماینده"),
        ("btn_reseller_creds", "دکمه اطلاعات ورود نماینده", "text", "در ربات اصلی برای صاحب فروشگاه"),
        ("btn_admin", "دکمه ادمین", "text", ""),
        ("btn_menu_home", "دکمه منوی اصلی (کیبورد)", "text", "ردیف پایین کیبورد دائمی"),
        ("btn_adm_orders", "دکمه سفارش‌ها (ادمین)", "text", "کیبورد ادمین"),
        ("btn_adm_payments", "دکمه رسیدها (ادمین)", "text", "کیبورد ادمین"),
        ("btn_adm_tickets", "دکمه تیکت‌ها (ادمین)", "text", "کیبورد ادمین"),
        ("btn_adm_plans", "دکمه پلن‌ها (ادمین)", "text", "کیبورد ادمین"),
        ("btn_adm_pg", "دکمه پاسارگارد (ادمین)", "text", "کیبورد ادمین"),
        ("btn_adm_preview", "دکمه پیش‌نمایش کاربر (ادمین)", "text", "کیبورد ادمین"),
        ("btn_back", "دکمه بازگشت", "text", "زیر پیام‌های انتخابی (اینلاین)"),
        ("btn_cancel", "انصراف", "text", ""),
        ("btn_renew", "تمدید", "text", ""),
        ("btn_sub_link", "لینک و QR", "text", ""),
    ],
    "نمایش منو": [
        (
            "menu_layout",
            "چیدمان کیبورد اصلی",
            "select",
            "منوی اصلی داخل کیبورد پایین است؛ انتخاب‌ها (پلن/پرداخت/…) زیر پیام می‌آیند",
            [("classic", "کلاسیک — هر دکمه یک ردیف"), ("compact", "فشرده — دکمه‌ها جفتی")],
        ),
    ],
    "نمایندگی": [
        (
            "show_reseller_apply",
            "نمایش درخواست نمایندگی",
            "toggle",
            "دکمه درخواست نمایندگی در منوی کاربران عادی",
        ),
        (
            "reseller_panel_base_url",
            "آدرس وب‌پنل نماینده",
            "text",
            "اختیاری. خالی = پیش‌فرض سیستم (آی‌پی سرور + پورت پنل، معمولاً :9000). در پیام تأیید برای نماینده ارسال می‌شود.",
        ),
        (
            "reseller_pg_panel_base_url",
            "آدرس پنل پاسارگارد برای نماینده",
            "text",
            "اختیاری. خالی = دقیقاً همان PG_BASE_URL تنظیم‌شده در اتصال بات (با path کامل).",
        ),
    ],
    "مدیریت PAYG": [
        (
            "billing_enabled",
            "فعال‌سازی PAYG",
            "toggle",
            "فقط روی نمایندگان Pay As You Go اثر دارد؛ Fixed دست‌نخورده می‌ماند",
        ),
        (
            "billing_low_balance",
            "آستانه هشدار موجودی کم (تومان)",
            "number",
            "وقتی موجودی Billing به این مقدار یا کمتر برسد یک‌بار هشدار تلگرام می‌رود؛ خرید PAYG نیز نیازمند کیف پول بیشتر از دو برابر این آستانه است",
        ),
        (
            "billing_on_empty",
            "رفتار اتمام موجودی",
            "select",
            "پیش‌فرض: مسدود کردن همه عملیات ساخت/تمدید سرویس (ساخت/تمدید/افزایش حجم/…)",
            [("block_provision", "مسدود کردن ساخت و تمدید سرویس")],
        ),
        (
            "billing_tick_minutes",
            "بازه شارژ خودکار (دقیقه)",
            "number",
            "فاصله اجرای billing tick برای کسر مصرف تفاضلی (دلتا) از watermark نماینده",
        ),
    ],
    "هشدار سرویس کاربر": [
        (
            "user_alert_low_enabled",
            "ارسال خودکار هشدار کمبود به کاربر",
            "toggle",
            "وقتی حجم یا زمان باقی‌مانده سرویس کمتر از درصد تعیین‌شده شود، در تلگرام به کاربر پیام می‌رود",
        ),
        (
            "user_alert_low_traffic_pct",
            "کمتر از چند درصد حجم؟",
            "number",
            "۱ تا ۹۹ — مثلاً ۲۰ یعنی وقتی کمتر از ۲۰٪ حجم مانده پیام برود",
        ),
        (
            "user_alert_low_time_pct",
            "کمتر از چند درصد زمان؟",
            "number",
            "۱ تا ۹۹ — مثلاً ۲۰ یعنی وقتی کمتر از ۲۰٪ از مدت سرویس مانده پیام برود",
        ),
    ],
    "QR اشتراک": [
        ("qr_enabled", "ارسال خودکار QR", "toggle", "بعد از تحویل سرویس، QR لینک اشتراک فرستاده می‌شود"),
        ("show_sub_link_in_text", "نمایش لینک در کپشن QR", "toggle", "لینک متنی هم در کپشن QR باشد"),
        ("qr_caption", "کپشن عکس QR", "textarea", "جزئیات لینک/حجم/زمان خودکار اضافه می‌شود. متغیر: {url}"),
        ("qr_background", "عکس پس‌زمینه QR", "image", "اختیاری — PNG/JPG"),
    ],
    "روش‌های پرداخت": [
        ("pay_wallet_enabled", "کیف پول داخلی", "toggle", "پرداخت از موجودی کیف پول کاربر"),
        ("pay_card_enabled", "کارت به کارت", "toggle", ""),
        ("pay_gateway_enabled", "درگاه پرداخت", "toggle", "لینک درگاه خارجی + ارسال رسید"),
        ("pay_crypto_enabled", "رمزارز", "toggle", ""),
        ("pay_stars_enabled", "استارز تلگرام", "toggle", "پرداخت درون‌برنامه‌ای با ⭐"),
        ("pay_discount_enabled", "کد تخفیف", "toggle", "نمایش دکمه کد تخفیف هنگام پرداخت"),
        ("auto_approve_payments", "تأیید خودکار رسید", "toggle", "روشن = بلافاصله بعد از رسید خرید، سرویس تحویل می‌شود. شارژ کیف‌پول هرگز خودکار تأیید نمی‌شود."),
        ("referral_bonus", "پاداش دعوت (تومان)", "number", "هدیه به معرف بعد از خرید موفق دعوت‌شده"),
        ("payment_reject_text", "متن رد پرداخت", "textarea", "وقتی ادمین رسید را رد می‌کند"),
    ],
    "پاکسازی سفارش‌های معلق": [
        (
            "pending_order_cleanup_enabled",
            "حذف خودکار سفارش‌های معلق",
            "toggle",
            "سفارش‌های پرداخت‌نشده قدیمی به‌صورت خودکار لغو شوند",
        ),
        (
            "pending_order_ttl_hours",
            "سن لغو (ساعت)",
            "number",
            "سفارش‌های قدیمی‌تر از این مقدار لغو می‌شوند (۱ تا ۷۲۰)",
        ),
        (
            "pending_order_cleanup_awaiting_approval",
            "شامل منتظر تأیید رسید",
            "toggle",
            "اگر روشن باشد، سفارش‌های awaiting_approval هم بعد از TTL لغو می‌شوند",
        ),
    ],
    "کارت به کارت": [
        ("card_number", "شماره کارت", "text", "۱۶ رقم"),
        ("card_holder", "نام صاحب کارت", "text", ""),
        ("card_pay_text", "راهنمای کارت‌به‌کارت", "textarea", "متغیرها: {amount} {card} {holder}"),
        ("btn_pay_card", "متن دکمه کارت به کارت", "text", ""),
    ],
    "درگاه پرداخت": [
        ("gateway_name", "نام درگاه", "text", "مثلاً زرین‌پال"),
        ("gateway_link", "لینک درگاه / صفحه پرداخت", "text", "می‌تواند شامل {amount} یا {order_id} باشد"),
        ("gateway_pay_text", "راهنمای درگاه", "textarea", "متغیرها: {amount} {order_id} {name}"),
        ("btn_pay_gateway", "متن دکمه درگاه", "text", ""),
    ],
    "رمزارز": [
        ("crypto_asset", "رمزارز", "text", "مثلاً USDT"),
        ("crypto_network", "شبکه", "text", "مثلاً TRC20"),
        ("crypto_address", "آدرس ولت", "text", ""),
        ("crypto_pay_text", "راهنمای رمزارز", "textarea", "متغیرها: {amount} {asset} {network} {address}"),
        ("btn_pay_crypto", "متن دکمه رمزارز", "text", ""),
    ],
    "استارز تلگرام": [
        (
            "stars_toman_per_star",
            "هر استارز چند تومان؟",
            "number",
            "مثال: اگر ۵۰۰ باشد، سفارش ۱۰۰٬۰۰۰ تومانی = ۲۰۰ استارز",
        ),
        ("stars_title", "عنوان فاکتور", "text", ""),
        ("stars_description", "توضیح فاکتور", "text", ""),
        ("btn_pay_stars", "متن دکمه استارز", "text", ""),
    ],
    "متن دکمه‌های پرداخت": [
        ("btn_pay_wallet", "پرداخت با کیف پول", "text", ""),
        ("btn_pay_discount", "کد تخفیف", "text", ""),
    ],
    "نام‌گذاری سرویس در پاسارگارد": [
        ("pg_username_prefix", "پیشوند ثابت", "text", "مثلاً clk — اول نام کاربر ساخته‌شده"),
        ("pg_username_suffix", "پسوند ثابت", "text", "اختیاری — ته نام"),
        (
            "pg_username_pattern",
            "الگوی نام",
            "text",
            "متغیرها: {prefix} {random} {suffix} {id} — پیش‌فرض: {prefix}_{random}{suffix}",
        ),
    ],
    "کانال اجباری": [
        ("force_join_enabled", "عضویت اجباری کانال", "toggle", "قبل از استفاده از ربات"),
        (
            "force_join_channel",
            "کانال‌ها",
            "force_channels",
            "هر کانال را جدا وارد کنید. با + کانال جدید اضافه کنید و برای هر کدام الزامی بودن عضویت را تعیین کنید",
        ),
    ],
}

# Map tab id → which SETTING_GROUPS cards to show (menu/notifications/update special)
TAB_SETTING_GROUPS: dict[str, list[str]] = {
    "menu": [],
    "messages": [
        "خوش‌آمد و هویت",
        "متن پیام‌ها",
        "متن دکمه‌های منو",
        "QR اشتراک",
    ],
    "payment": [
        "روش‌های پرداخت",
        "پاکسازی سفارش‌های معلق",
        "کارت به کارت",
        "درگاه پرداخت",
        "رمزارز",
        "استارز تلگرام",
        "متن دکمه‌های پرداخت",
    ],
    "supports": [],
    "services": [
        "نام‌گذاری سرویس در پاسارگارد",
        "کانال اجباری",
    ],
    "reseller": ["نمایندگی"],
    "billing": ["مدیریت PAYG"],
    "notifications": ["هشدار سرویس کاربر"],
    "backup": [],
    "pwa": [],
    "update": [],
    "ssl": [],
    "bot": [],
    "appearance": [],
    # Legacy aliases kept for keys_for_tab / old save URLs
    "welcome": ["خوش‌آمد و هویت"],
    "buttons": ["متن دکمه‌های منو"],
    "qr": ["QR اشتراک"],
    "naming": ["نام‌گذاری سرویس در پاسارگارد"],
    "forcejoin": ["کانال اجباری"],
}

TOGGLE_KEYS = {
    item[0]
    for fields in SETTING_GROUPS.values()
    for item in fields
    if len(item) >= 3 and item[2] == "toggle"
}

IMAGE_KEYS = {
    item[0]
    for fields in SETTING_GROUPS.values()
    for item in fields
    if len(item) >= 3 and item[2] == "image"
}


def keys_for_tab(tab: str) -> set[str]:
    """Setting keys that belong to a settings tab (prevents wiping other tabs on save)."""
    names = TAB_SETTING_GROUPS.get(tab) or []
    keys: set[str] = set()
    for name in names:
        for item in SETTING_GROUPS.get(name, []):
            keys.add(item[0])
    return keys


_defaults_ready = False


async def ensure_default_settings(session: AsyncSession) -> None:
    """Insert missing default settings once per process (cheap after first call)."""
    global _defaults_ready
    if _defaults_ready:
        return
    result = await session.execute(select(Setting.key))
    existing = {row[0] for row in result.all()}
    missing = False
    for key, value in DEFAULT_SETTINGS.items():
        if key not in existing:
            session.add(Setting(key=key, value=value))
            missing = True
    if missing:
        await session.commit()
    _defaults_ready = True


async def get_all_settings(
    session: AsyncSession,
    *,
    reseller_id: int | None = None,
) -> dict[str, str]:
    # Defaults are seeded at startup — avoid N+1 writes on every read path.
    rid = _effective_reseller_id(reseller_id)
    cache_key = int(rid or 0)
    now = time.monotonic()
    hit = _SETTINGS_CACHE.get(cache_key)
    if hit and (now - hit[0]) < _SETTINGS_CACHE_TTL:
        return dict(hit[1])

    data = dict(DEFAULT_SETTINGS)
    if rid:
        from app.db.models import ResellerSetting

        # Shop isolation: defaults + reseller overrides ONLY.
        # Never inherit live platform Setting rows (payment, menu, force-join, …).
        r_result = await session.execute(
            select(ResellerSetting).where(ResellerSetting.reseller_user_id == rid)
        )
        data.update({r.key: r.value for r in r_result.scalars().all()})
        # Reseller bots never show platform apply / admin buttons / miniapp (platform WebApp)
        data["show_reseller_apply"] = "0"
        data["show_miniapp"] = "0"
        order = [
            p.strip()
            for p in str(data.get("menu_order") or "").split(",")
            if p.strip() and p.strip() not in {"reseller_apply", "miniapp"}
        ]
        if "shop" not in order:
            order.insert(0, "shop")
        data["menu_order"] = ",".join(order)
        _SETTINGS_CACHE[cache_key] = (now, dict(data))
        return data
    result = await session.execute(select(Setting))
    rows = result.scalars().all()
    data.update({r.key: r.value for r in rows})
    _SETTINGS_CACHE[cache_key] = (now, dict(data))
    return data


def on(value: str | None) -> bool:
    return (value or "").strip() in {"1", "true", "yes", "on", "True"}


def clamp_alert_percent(raw: str | None, default: int = 20) -> str:
    """Keep user-alert thresholds in 1..99."""
    try:
        n = int(float(str(raw).strip()))
    except Exception:
        n = default
    return str(max(1, min(99, n)))


def is_protected_admin(user: BotUser) -> bool:
    """True for panel/bot admins that must not be blocked or deleted."""
    if user.role == Role.ADMIN.value:
        return True
    return user.telegram_id in get_settings().admin_ids


async def delete_bot_user(
    session: AsyncSession,
    user_id: int,
    *,
    delete_pg_services: bool = True,
    delete_pg_admin: bool = True,
    actor_user_id: int | None = None,
) -> dict:
    """Hard-delete a bot user and related rows. Refuses protected admins."""
    from sqlalchemy import delete, update

    from app.db.models import (
        Order,
        PanelTicket,
        PanelTicketMessage,
        Payment,
        Plan,
        ResellerApplication,
        ResellerBillingRate,
        ResellerBillingTransaction,
        ResellerSetting,
        Ticket,
        TicketMessage,
        TrialClaim,
        UserService,
        WalletTransaction,
    )
    from app.services.pasarguard import get_pg

    user = await session.get(BotUser, user_id)
    if not user:
        raise ValueError("کاربر یافت نشد")
    if actor_user_id is not None and user.id == actor_user_id:
        raise ValueError("نمی‌توانید خودتان را حذف کنید")
    if is_protected_admin(user):
        raise ValueError("حذف ادمین مجاز نیست")

    telegram_id = user.telegram_id
    name = user.full_name or user.username or str(telegram_id)

    # Revoke reseller first (profile + PG admin + unlink customers)
    from app.services.resellers import get_reseller_profile, revoke_reseller

    if await get_reseller_profile(session, user_id):
        await revoke_reseller(
            session,
            user_id,
            delete_pg_admin=delete_pg_admin,
            commit=False,
        )

    # Clear self-references pointing at this user
    await session.execute(
        update(BotUser).where(BotUser.referred_by_id == user_id).values(referred_by_id=None)
    )
    # reseller_id / order.reseller_id already cleared by revoke_reseller when applicable
    await session.execute(
        update(BotUser).where(BotUser.reseller_id == user_id).values(reseller_id=None)
    )
    await session.execute(
        update(Order).where(Order.reseller_id == user_id).values(reseller_id=None)
    )
    from app.db.models import Ticket

    await session.execute(
        update(Ticket).where(Ticket.reseller_id == user_id).values(reseller_id=None)
    )

    services = list(
        (
            await session.execute(select(UserService).where(UserService.bot_user_id == user_id))
        ).scalars().all()
    )
    svc_ids = [s.id for s in services]
    pg_services_deleted = 0
    if delete_pg_services:
        for svc in services:
            if not svc.pg_user_id:
                continue
            try:
                await get_pg().delete_user_by_id(int(svc.pg_user_id))
                pg_services_deleted += 1
            except Exception:
                try:
                    await get_pg().set_disabled_by_id(int(svc.pg_user_id), True)
                except Exception:
                    pass

    # Break orders ↔ services cycle
    await session.execute(
        update(Order).where(Order.user_id == user_id).values(service_id=None)
    )
    if svc_ids:
        await session.execute(
            update(Order).where(Order.service_id.in_(svc_ids)).values(service_id=None)
        )

    # Reseller applications (null order_id then delete)
    apps = list(
        (
            await session.execute(
                select(ResellerApplication).where(ResellerApplication.user_id == user_id)
            )
        ).scalars().all()
    )
    for app in apps:
        app.order_id = None
    await session.flush()
    await session.execute(
        delete(ResellerApplication).where(ResellerApplication.user_id == user_id)
    )

    await session.execute(delete(Payment).where(Payment.user_id == user_id))
    await session.execute(delete(Order).where(Order.user_id == user_id))
    await session.execute(delete(UserService).where(UserService.bot_user_id == user_id))

    ticket_ids = list(
        (
            await session.execute(select(Ticket.id).where(Ticket.user_id == user_id))
        ).scalars().all()
    )
    if ticket_ids:
        await session.execute(
            delete(TicketMessage).where(TicketMessage.ticket_id.in_(ticket_ids))
        )
        await session.execute(delete(Ticket).where(Ticket.id.in_(ticket_ids)))

    await session.execute(
        delete(WalletTransaction).where(WalletTransaction.user_id == user_id)
    )

    # Shop/reseller rows that FK to bot_users (no ON DELETE CASCADE in schema)
    await session.execute(
        delete(ResellerSetting).where(ResellerSetting.reseller_user_id == user_id)
    )
    await session.execute(
        delete(ResellerBillingTransaction).where(
            ResellerBillingTransaction.reseller_user_id == user_id
        )
    )
    await session.execute(
        delete(ResellerBillingRate).where(ResellerBillingRate.reseller_user_id == user_id)
    )
    plan_ids = list(
        (
            await session.execute(select(Plan.id).where(Plan.owner_reseller_id == user_id))
        ).scalars().all()
    )
    if plan_ids:
        await session.execute(
            update(Order).where(Order.plan_id.in_(plan_ids)).values(plan_id=None)
        )
        await session.execute(delete(Plan).where(Plan.id.in_(plan_ids)))
    panel_ticket_ids = list(
        (
            await session.execute(
                select(PanelTicket.id).where(PanelTicket.opener_reseller_user_id == user_id)
            )
        ).scalars().all()
    )
    if panel_ticket_ids:
        await session.execute(
            delete(PanelTicketMessage).where(
                PanelTicketMessage.ticket_id.in_(panel_ticket_ids)
            )
        )
        await session.execute(
            delete(PanelTicket).where(PanelTicket.id.in_(panel_ticket_ids))
        )
    await session.execute(delete(TrialClaim).where(TrialClaim.user_id == user_id))

    await session.delete(user)
    await session.commit()

    return {
        "user_id": user_id,
        "telegram_id": telegram_id,
        "name": name,
        "services_removed": len(svc_ids),
        "pg_services_deleted": pg_services_deleted,
    }


def friendly_user_delete_error(exc: Exception) -> str:
    """Short Persian message for hard-delete failures (hide raw SQL)."""
    text = str(exc or "")
    low = text.lower()
    if (
        "foreign key" in low
        or "integrityerror" in low
        or "constraint failed" in low
        or isinstance(exc, IntegrityError)
    ):
        return (
            "حذف کاربر ممکن نشد چون هنوز دادهٔ وابسته‌ای به این حساب وصل است. "
            "علت: محدودیت یکپارچگی پایگاه‌داده (کلید خارجی). "
            "راه حل: دوباره تلاش کنید؛ اگر تکرار شد از پشتیبانی بخواهید لاگ سرور را بررسی کند."
        )
    return (text or "حذف ناموفق بود")[:320]
