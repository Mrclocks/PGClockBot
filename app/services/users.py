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
from app.services.button_styles import default_settings as button_style_defaults
from app.services.button_styles import setting_group_fields as button_style_setting_fields

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


def normalize_force_join_invite_link(raw: str | None) -> str:
    """Normalize a public t.me / telegram.me invite or channel URL for inline buttons."""
    link = (raw or "").strip()
    if not link:
        return ""
    lower = link.lower()
    if lower.startswith("tg://"):
        return link
    for prefix in (
        "https://t.me/",
        "http://t.me/",
        "t.me/",
        "https://telegram.me/",
        "http://telegram.me/",
        "telegram.me/",
    ):
        if lower.startswith(prefix):
            rest = link[len(prefix) :].lstrip("/")
            if not rest:
                return ""
            # Private message deep-links (t.me/c/…) are not join URLs
            if rest.lower().startswith("c/"):
                return ""
            return f"https://t.me/{rest}"
    if link.startswith("@") and len(link) > 1:
        return f"https://t.me/{link[1:].lower()}"
    return ""


def is_force_join_invite_ref(raw: str | None) -> bool:
    """True when value is an invite / joinchat URL (resolved via getChat at check time)."""
    link = normalize_force_join_invite_link(raw)
    if not link:
        return False
    lower = link.lower()
    return "/+" in lower or "joinchat/" in lower


def force_join_channel_url(channel_id: str, invite_link: str | None = None) -> str | None:
    """Best-effort join URL for inline buttons (@username or stored invite link)."""
    custom = normalize_force_join_invite_link(invite_link)
    if custom:
        return custom
    # Invite stored as the channel id itself
    as_invite = normalize_force_join_invite_link(channel_id)
    if as_invite:
        return as_invite
    ch = normalize_force_join_channel_id(channel_id)
    if not ch:
        return None
    if ch.startswith("@"):
        return f"https://t.me/{ch[1:]}"
    return None


def normalize_force_join_channel_id(raw: str | None) -> str:
    """Normalize @username / numeric id / t.me URL into a Telegram chat id string.

    Usernames are lowercased (Telegram treats them case-insensitively).
    Private invite links (``t.me/+hash``, ``joinchat/...``) return ``\"\"`` — keep them
    in the optional ``link`` field (or as invite-ref id); runtime resolves via ``getChat``.
    ``t.me/c/<internal>`` private deep-links become ``-100<internal>``.
    """
    import re

    from app.services.numbers import normalize_digits

    ch = normalize_digits((raw or "").strip())
    if not ch:
        return ""
    lower = ch.lower()
    for prefix in (
        "https://t.me/",
        "http://t.me/",
        "t.me/",
        "https://telegram.me/",
        "http://telegram.me/",
        "telegram.me/",
    ):
        if lower.startswith(prefix):
            ch = ch[len(prefix) :]
            break
    ch = ch.strip().strip("/")
    if "?" in ch:
        ch = ch.split("?", 1)[0].strip()
    if not ch:
        return ""
    # t.me/c/<channel_internal_id>[/message_id] → -100<internal>
    m = re.match(r"^[Cc]/(\d+)(?:/\d+)?$", ch)
    if m:
        return f"-100{m.group(1)}"
    # Private invite paths — not a direct get_chat_member chat id
    if ch.startswith("+") or ch.lower().startswith("joinchat") or "/" in ch:
        return ""
    if ch.startswith("@"):
        return "@" + ch[1:].lower()
    if ch.startswith("-") and ch[1:].isdigit():
        return ch
    if ch.isdigit():
        # Bare internal ids from t.me/c/… copy-paste without prefix
        if len(ch) >= 9:
            return f"-100{ch}" if not ch.startswith("100") else f"-{ch}"
        return ch
    if re.match(r"^[A-Za-z][A-Za-z0-9_]{3,}$", ch):
        return f"@{ch.lower()}"
    return ch


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
    normalized = normalize_force_join_channel_id(ch)
    if normalized:
        return normalized, required
    # Invite-only line — keep normalized invite URL as id for runtime resolve
    invite = normalize_force_join_invite_link(ch)
    return (invite, required) if invite else ("", required)


def _entry_invite_link(item: dict[str, Any], *, id_raw: str = "") -> str:
    """Pull optional button URL from JSON fields or an invite-only id value."""
    for key in ("link", "invite_link", "url"):
        link = normalize_force_join_invite_link(str(item.get(key) or ""))
        if link:
            return link
    raw = (id_raw or "").strip()
    if not raw or raw.startswith("@"):
        return ""
    lower = raw.lower()
    if lower.startswith(("http://", "https://", "t.me/", "telegram.me/", "tg://")):
        return normalize_force_join_invite_link(raw)
    return ""


def _entry_title(item: dict[str, Any] | None) -> str:
    if not isinstance(item, dict):
        return ""
    title = str(item.get("title") or item.get("name") or item.get("label") or "").strip()
    return title[:64]


def parse_force_join_entries(raw: str | None) -> list[dict[str, Any]]:
    """Parse force-join setting into [{id, required, link?, title?}, ...].

    Accepts JSON array (new) or legacy line/comma-separated channels (all required
    unless marked ``!optional``). ``link`` is the join URL for inline buttons;
    invite-only ids are kept and resolved via ``getChat`` when checking membership.
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
                title = ""
                link = ""
                if isinstance(item, str):
                    ch, required = _parse_channel_line(item)
                    if not ch:
                        link = normalize_force_join_invite_link(item)
                        ch = link
                    elif is_force_join_invite_ref(ch):
                        link = ch
                elif isinstance(item, dict):
                    id_raw = str(item.get("id") or item.get("channel") or "")
                    ch = normalize_force_join_channel_id(id_raw)
                    link = _entry_invite_link(item, id_raw=id_raw)
                    title = _entry_title(item)
                    if not ch and link:
                        # Invite URL alone — keep for getChat resolve + button
                        ch = link
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
                entry: dict[str, Any] = {"id": ch, "required": required}
                if link:
                    entry["link"] = link
                elif is_force_join_invite_ref(ch):
                    entry["link"] = ch
                if title:
                    entry["title"] = title
                out.append(entry)
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
        entry: dict[str, Any] = {"id": ch, "required": required}
        if is_force_join_invite_ref(ch):
            entry["link"] = ch
        out.append(entry)
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
        id_raw = str(item.get("id") or "")
        ch = normalize_force_join_channel_id(id_raw)
        link = _entry_invite_link(item, id_raw=id_raw)
        if not ch and link:
            ch = link
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
        entry: dict[str, Any] = {"id": ch, "required": required}
        if link:
            entry["link"] = link
        elif is_force_join_invite_ref(ch):
            entry["link"] = ch
        title = _entry_title(item)
        if title:
            entry["title"] = title
        normalized.append(entry)
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
    from app.services.payment_destinations import (
        KEY_CARDS,
        KEY_CRYPTO,
        KEY_GATEWAYS,
        enrich_payment_settings,
    )

    merged = dict(values)
    if KEY_CARDS in merged or KEY_GATEWAYS in merged or KEY_CRYPTO in merged:
        merged = enrich_payment_settings({**DEFAULT_SETTINGS, **merged})
        for legacy in (
            "card_number",
            "card_holder",
            "gateway_name",
            "gateway_link",
            "crypto_asset",
            "crypto_network",
            "crypto_address",
        ):
            if legacy in merged:
                values[legacy] = merged[legacy]
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
    "payment_cards": "[]",
    "payment_gateways": "[]",
    "payment_crypto_wallets": "[]",
    "card_number": "",
    "card_holder": "",
    "support_text": "پیام خود را بنویسید؛ تیم پشتیبانی پاسخ می‌دهد.",
    "support_contacts": "[]",
    "force_join_channel": "",
    "force_join_enabled": "0",
    "force_join_msg": (
        "برای استفاده از ربات ابتدا از دکمه‌های زیر عضو کانال شوید، "
        "سپس «عضو شدم» را بزنید."
    ),
    "btn_force_join": "عضویت در کانال",
    "btn_force_join_check": "✅ عضو شدم",
    "terms_entry_enabled": "0",
    "terms_entry_text": "",
    "terms_entry_btn": "موافقم",
    "terms_entry_reaccept": "1",
    "terms_buy_user_enabled": "0",
    "terms_buy_user_text": "",
    "terms_buy_user_btn": "موافقم",
    "terms_buy_user_reaccept": "1",
    "terms_buy_reseller_enabled": "0",
    "terms_buy_reseller_text": "",
    "terms_buy_reseller_btn": "موافقم",
    "terms_buy_reseller_reaccept": "1",
    "trial_enabled": "0",
    "referral_bonus": "0",
    "loyalty_enabled": "1",
    "points_to_wallet_rate": "100",
    "lucky_wheel_enabled": "0",
    "lucky_wheel_spin_cost_points": "10",
    "lucky_wheel_daily_limit": "3",
    "lucky_wheel_cooldown_seconds": "0",
    "lucky_wheel_free_spins_daily": "0",
    "loyalty_submenu_order": "loy_referral,loy_points,loy_rewards,loy_wheel,loy_history",
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
    "btn_wallet_topup": "🟢➕ شارژ کیف پول",
    "btn_wallet_tx": "🟡📜 تراکنش‌ها",
    "btn_support": "🟣🎧 پشتیبانی",
    "btn_support_new": "🟣✉️ تیکت جدید",
    "btn_support_list": "📋 تیکت‌های من",
    "btn_guide": "📘 راهنما",
    "btn_faq": "❓ سوالات متداول",
    "btn_referral": "👥 دعوت دوستان",
    "btn_loyalty": "⭐ باشگاه مشتریان",
    "btn_loy_points": "⭐ امتیاز من",
    "btn_loy_rewards": "🎁 جوایز",
    "btn_loy_wheel": "🎡 چرخ شانس",
    "btn_loy_history": "📜 تاریخچه",
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
    "show_loyalty": "1",
    "show_reseller_apply": "1",
    "show_wallet": "1",
    "show_support": "1",
    "show_miniapp": "1",
    "menu_layout": "compact",
    "menu_order": "shop,services,wallet,support,loyalty,reseller_apply,miniapp",
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
    "custom_plan_button_style": "",
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
    "pay_psp_enabled": "0",
    "pay_card_auto_enabled": "0",
    "pay_discount_enabled": "1",
    "psp_provider": "zarinpal",
    "psp_merchant_id": "",
    "psp_sandbox": "1",
    "psp_pay_text": (
        "مبلغ قابل پرداخت: <b>{amount}</b>\n"
        "از دکمه زیر وارد درگاه شوید و پرداخت را تکمیل کنید.\n"
        "پس از تأیید درگاه، سرویس به‌صورت خودکار تحویل می‌شود."
    ),
    "btn_pay_psp": "🔵🏦 درگاه آنلاین",
    "card_auto_provider": "generic",
    "card_auto_webhook_secret": "",
    "card_auto_hint_text": (
        "اگر تأیید خودکار کارت روشن باشد، پس از واریز دقیق مبلغ، پرداخت بدون رسید عکس تأیید می‌شود."
    ),
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
    # UX20 ops
    "shop_maintenance_enabled": "0",
    "shop_maintenance_text": (
        "فروشگاه موقتاً در حال به‌روزرسانی است.\n"
        "تمدید و پشتیبانی فعال است — کمی بعد دوباره سر بزنید."
    ),
    "admin_daily_report_enabled": "1",
    "admin_daily_report_hour": "0",
    "admin_daily_report_last": "",
    "admin_daily_report_template": "",
    "admin_daily_report_metrics": "",
    "backup_schedule_enabled": "1",
    "backup_schedule_hour": "3",
    "backup_include_env_scheduled": "0",
    "backup_last_ok_at": "",
    "backup_last_verify_ok": "",
    "capacity_warn_pct": "80",
    "receipt_auto_match_enabled": "1",
    "receipt_match_window_minutes": "120",
    "action_center_expire_days": "3",
    "funnel_tracking_enabled": "1",
    "one_tap_renew_enabled": "1",
    # Mini App / commerce extras (platform shop toggles)
    "auto_renew_enabled": "0",
    "auto_renew_days_before": "1",
    "cart_recovery_enabled": "0",
    "cart_recovery_after_hours": "2",
    "cart_recovery_max_sends": "2",
    "user_self_pause_enabled": "1",
    "emergency_credit_enabled": "0",
    "emergency_credit_max_toman": "20000",
    "predictive_traffic_enabled": "0",
    "predictive_traffic_warn_hours": "24",
}
DEFAULT_SETTINGS.update(button_style_defaults())

# field kinds: text | textarea | toggle | select | number | image
# (key, label, kind, help?, options?)
# Tabs for /settings?tab=... (order matches product IA)
SETTINGS_TABS: list[tuple[str, str]] = [
    ("welcome", "خوش‌آمد و هویت"),
    ("appearance", "هویت ربات"),
    ("messages", "متن پیام‌ها"),
    ("buttons", "متن دکمه‌ها"),
    ("colors", "رنگبندی دکمه‌ها"),
    ("menu", "منوی بات"),
    ("links", "لینک‌های سریع"),
    ("qr", "QR اشتراک"),
    ("naming", "نام‌گذاری سرویس"),
    ("forcejoin", "کانال اجباری"),
    ("terms", "قوانین"),
    ("notifications", "نوتیفیکیشن"),
    ("daily_report", "گزارش روزانه"),
    ("bot", "ربات و اتصال"),
]

# Legacy bot-settings tabs moved to page-level modals (finance / support / loyalty).
SETTINGS_DOMAIN_REDIRECTS: dict[str, str] = {
    "payment": "/finance?tab=orders&settings=payment",
    "billing": "/finance?tab=orders&settings=billing",
    "finance": "/finance?tab=orders&settings=payment",
    "supports": "/tickets?supports=1",
    "loyalty": "/loyalty?settings=referral",
    "reseller": "/resellers",
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
            "اولین پیامی که کاربر بعد از استارت می‌بیند. متغیرها: {user_name} (قدیمی: {name})، {shop_title} — فهرست کامل: /message-variables",
        ),
    ],
    "متن پیام‌ها": [
        ("guide_text", "متن راهنما", "textarea", "دستور /help در تلگرام"),
        ("faq_text", "متن سوالات متداول", "textarea", "قابل استفاده در پیام‌ها"),
        ("empty_services_text", "وقتی سرویسی ندارد", "textarea", "پیام بخش سرویس‌های من اگر لیست خالی باشد"),
        ("shop_empty_text", "وقتی پلنی نیست", "textarea", "پیام فروشگاه اگر پلن فعالی نباشد"),
        ("delivery_title", "عنوان پیام تحویل سرویس", "text", "مثلاً: ✅ سرویس آماده است"),
        ("purchase_success_text", "متن موفقیت خرید", "textarea", "متغیرها: {order_id} {plan_name} — فهرست: /message-variables"),
        ("wallet_success_title", "عنوان موفقیت شارژ کیف پول", "text", "عنوان پیام بعد از تأیید شارژ"),
        ("wallet_success_text", "متن موفقیت شارژ کیف پول", "textarea", "متغیرها: {amount} {payment_id}"),
        ("payment_ok_title", "عنوان تأیید پرداخت", "text", "عنوان پیام وقتی پرداخت غیر از شارژ کیف پول تأیید می‌شود"),
    ],
    "متن پشتیبانی ربات": [
        ("support_text", "متن صفحه پشتیبانی", "textarea", "بالای دکمه/فرم پشتیبانی در ربات نمایش داده می‌شود"),
    ],
    "متن دعوت دوستان": [
        ("referral_text", "متن دعوت دوستان", "textarea", "متغیرها: {code} {link} — فهرست: /message-variables"),
    ],
    "متن دکمه‌های منو": [
        ("btn_shop", "دکمه خرید", "text", ""),
        ("btn_services", "دکمه سرویس‌ها", "text", ""),
        ("btn_wallet", "دکمه کیف پول", "text", ""),
        ("btn_wallet_topup", "دکمه شارژ کیف پول", "text", "زیرمنوی کیف پول"),
        ("btn_wallet_tx", "دکمه تراکنش‌ها", "text", "زیرمنوی کیف پول"),
        ("btn_support", "دکمه پشتیبانی", "text", ""),
        ("btn_support_new", "دکمه تیکت جدید", "text", "زیرمنوی پشتیبانی (حالت تیکت)"),
        ("btn_support_list", "دکمه تیکت‌های من", "text", "زیرمنوی پشتیبانی (حالت تیکت)"),
        ("btn_guide", "دکمه راهنما", "text", ""),
        ("btn_faq", "دکمه سوالات", "text", ""),
        ("btn_loyalty", "دکمه باشگاه مشتریان", "text", "منوی اصلی ربات"),
        ("btn_referral", "دکمه دعوت دوستان", "text", "زیرمنوی باشگاه مشتریان"),
        ("btn_loy_points", "دکمه امتیاز من", "text", "زیرمنوی باشگاه مشتریان"),
        ("btn_loy_rewards", "دکمه جوایز", "text", "زیرمنوی باشگاه مشتریان"),
        ("btn_loy_wheel", "دکمه چرخ شانس", "text", "زیرمنوی باشگاه مشتریان"),
        ("btn_loy_history", "دکمه تاریخچه باشگاه", "text", "زیرمنوی باشگاه مشتریان"),
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
        ("btn_adm_users", "دکمه کاربران (ادمین)", "text", "کیبورد ادمین"),
        ("btn_adm_settings", "دکمه تنظیمات (ادمین)", "text", "کیبورد ادمین"),
        ("btn_adm_broadcast", "دکمه پیام گروهی (ادمین)", "text", "کیبورد ادمین"),
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
    "مدیریت PAYG": [
        (
            "billing_enabled",
            "فعال‌سازی PAYG",
            "toggle",
            "فقط روی نمایندگان PAYG اثر دارد؛ Fixed دست‌نخورده می‌ماند",
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
    "گزارش روزانه": [
        (
            "admin_daily_report_enabled",
            "ارسال گزارش روزانه در تلگرام",
            "toggle",
            "هر روز در ساعت مشخص‌شده خلاصهٔ انتخابی به ادمین‌ها فرستاده می‌شود",
        ),
        (
            "admin_daily_report_hour",
            "ساعت ارسال (۰–۲۳، UTC)",
            "number",
            "مثلاً ۰ = نیمه‌شب UTC · ۲۰ = حدود نیمه‌شب تهران در تابستان",
        ),
        (
            "admin_daily_report_template",
            "متن گزارش",
            "textarea",
            "متغیرها: {admin_name} {users_new} {revenue_today} … — فهرست کامل در همین تب و /message-variables",
        ),
    ],
    "گزارش و عملیات": [
        (
            "action_center_expire_days",
            "پنجره سرویس‌های نزدیک انقضا (روز)",
            "number",
            "برای مرکز اقدام داشبورد",
        ),
        (
            "one_tap_renew_enabled",
            "دکمه تمدید یک‌ضرب در هشدار انقضا",
            "toggle",
            "در پیام هشدار انقضا دکمه تمدید سریع نمایش داده شود",
        ),
        (
            "capacity_warn_pct",
            "آستانه هشدار ظرفیت نماینده (٪)",
            "number",
            "مثلاً ۸۰ — قبل از پر شدن سهمیه هشدار داده می‌شود",
        ),
        (
            "funnel_tracking_enabled",
            "ثبت رفتار کاربر",
            "toggle",
            "مراحل باز کردن فروشگاه تا تحویل برای آمار «رفتار کاربر» در مدیریت مالی",
        ),
    ],
    "رنگ دکمه‌ها": button_style_setting_fields(),
    "بکاپ زمان‌بندی": [
        (
            "backup_schedule_enabled",
            "بکاپ خودکار شبانه",
            "toggle",
            "ساخت بکاپ و بررسی سلامت آرشیو",
        ),
        (
            "backup_schedule_hour",
            "ساعت بکاپ (۰–۲۳، UTC)",
            "number",
            "پیش‌فرض ۳",
        ),
        (
            "backup_include_env_scheduled",
            "شامل .env در بکاپ خودکار",
            "toggle",
            "توکن‌ها و اسرار — فقط اگر مطمئنید",
        ),
    ],
    "حالت تعمیرات و رسید": [
        (
            "shop_maintenance_enabled",
            "حالت تعمیرات فروشگاه",
            "toggle",
            "خرید جدید بسته می‌شود؛ تمدید و پشتیبانی باز می‌ماند",
        ),
        (
            "shop_maintenance_text",
            "متن حالت تعمیرات",
            "textarea",
            "پیامی که در ربات به کاربر نشان داده می‌شود",
        ),
        (
            "receipt_auto_match_enabled",
            "پیشنهاد تطبیق رسیدهای هم‌مبلغ",
            "toggle",
            "در صف پرداخت‌ها موارد هم‌مبلغ در بازه زمانی پیشنهاد می‌شود",
        ),
        (
            "receipt_match_window_minutes",
            "بازه تطبیق رسید (دقیقه)",
            "number",
            "مثلاً ۱۲۰",
        ),
    ],
    "QR اشتراک": [
        ("qr_enabled", "ارسال خودکار QR", "toggle", "بعد از تحویل سرویس، QR لینک اشتراک فرستاده می‌شود"),
        ("show_sub_link_in_text", "نمایش لینک در کپشن QR", "toggle", "لینک متنی هم در کپشن QR باشد"),
        ("qr_caption", "کپشن عکس QR", "textarea", "جزئیات لینک/حجم/زمان خودکار اضافه می‌شود. متغیر: {url} یا {sub_link}"),
        ("qr_background", "عکس پس‌زمینه QR", "image", "اختیاری — PNG/JPG"),
    ],
    "روش‌های پرداخت": [
        ("pay_wallet_enabled", "کیف پول داخلی", "toggle", "پرداخت از موجودی کیف پول کاربر"),
        ("pay_card_enabled", "کارت به کارت", "toggle", ""),
        ("pay_gateway_enabled", "درگاه پرداخت (لینک + رسید)", "toggle", "لینک درگاه خارجی + ارسال رسید — روش قبلی حفظ می‌شود"),
        ("pay_psp_enabled", "درگاه آنلاین API", "toggle", "درگاه واقعی request→verify (Mock بدون مرچنت / زرین‌پال با مرچنت)"),
        ("pay_card_auto_enabled", "تأیید خودکار کارت", "toggle", "وب‌هوک امضادار از سرویس تأیید کارت — کنار رسید دستی"),
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
        (
            "payment_cards",
            "کارت‌ها",
            "payment_cards",
            "چند کارت — دکمه + برای افزودن. متغیرهای متن: {amount} {card} {holder}",
        ),
        ("card_pay_text", "راهنمای کارت‌به‌کارت", "textarea", "متغیرها: {amount} {card} {holder}"),
        ("btn_pay_card", "متن دکمه کارت به کارت", "text", ""),
    ],
    "درگاه پرداخت": [
        (
            "payment_gateways",
            "درگاه‌ها",
            "payment_gateways",
            "چند درگاه — لینک می‌تواند شامل {amount} یا {order_id} باشد",
        ),
        ("gateway_pay_text", "راهنمای درگاه", "textarea", "متغیرها: {amount} {order_id} {gateway_name} (قدیمی: {name})"),
        ("btn_pay_gateway", "متن دکمه درگاه", "text", ""),
    ],
    "رمزارز": [
        (
            "payment_crypto_wallets",
            "آدرس‌های ولت",
            "payment_crypto_wallets",
            "چند آدرس — هر ردیف: رمزارز، شبکه، آدرس",
        ),
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
    "درگاه آنلاین API": [
        (
            "psp_provider",
            "ارائه‌دهنده",
            "select",
            "zarinpal = درگاه واقعی؛ mock فقط با ALLOW_SETTLEMENT_MOCK=1 روی سرور",
            [("zarinpal", "زرین‌پال"), ("mock", "Mock (فقط تست محلی)")],
        ),
        (
            "psp_merchant_id",
            "مرچنت آیدی",
            "password",
            "خالی بماند = بدون تغییر. برای mock لازم نیست",
        ),
        (
            "psp_sandbox",
            "حالت سندباکس",
            "toggle",
            "روشن = محیط آزمایشی زرین‌پال",
        ),
        (
            "psp_pay_text",
            "راهنمای درگاه API",
            "textarea",
            "متغیرها: {amount} {order_id} {payment_id}",
        ),
        ("btn_pay_psp", "متن دکمه درگاه آنلاین", "text", ""),
    ],
    "تأیید خودکار کارت": [
        (
            "card_auto_provider",
            "نام ارائه‌دهنده",
            "text",
            "شناسه منطقی (مثلاً generic)",
        ),
        (
            "card_auto_webhook_secret",
            "رمز امضای وب‌هوک",
            "password",
            "حداقل ۱۶ کاراکتر — HMAC-SHA256 روی بدنه؛ هدر X-Signature. خالی = بدون تغییر",
        ),
        (
            "card_auto_hint_text",
            "متن راهنما کنار کارت‌به‌کارت",
            "textarea",
            "پلتفرم: /payments/settlement/card-auto/platform/webhook — فروشگاه: .../shop/{reseller_id}/webhook",
        ),
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
            "کانال‌ها (دکمه‌های اینلاین)",
            "force_channels",
            "شناسه: @username یا -100… یا لینک t.me/c/… — برای کانال خصوصی لینک دعوت (+…) را هم بگذارید. عنوان روی دکمه اینلاین نمایش داده می‌شود. ربات باید ادمین کانال باشد (توضیح بالا).",
        ),
        (
            "force_join_msg",
            "متن پیام عضویت اجباری",
            "textarea",
            "پیام بالای دکمه‌های اینلاین. اختیاری: {channels} — فهرست کامل متغیرها: /message-variables",
        ),
        ("btn_force_join", "متن پیش‌فرض دکمه لینک کانال", "text", "اگر عنوان کانال خالی باشد"),
        ("btn_force_join_check", "متن دکمه بررسی عضویت", "text", "دکمه «عضو شدم»"),
    ],
    "قوانین ورود به ربات": [
        ("terms_entry_enabled", "فعال", "toggle", "قبل از منوی اصلی — فقط کاربران"),
        (
            "terms_entry_text",
            "متن قوانین ورود",
            "textarea",
            "برای ایموجی پریمیوم، متن را از تنظیمات داخل ربات تلگرام ذخیره کنید",
        ),
        ("terms_entry_btn", "متن دکمه موافقت", "text", "برچسب دکمه اینلاین (ایموجی عادی؛ پریمیوم از ربات)"),
        (
            "btn_style_terms_entry",
            "رنگ دکمه موافقت",
            "btn_color",
            "همان رنگ‌بندی تلگرام؛ از تب رنگبندی هم قابل تغییر است",
        ),
        (
            "terms_entry_reaccept",
            "پذیرش مجدد با تغییر متن",
            "toggle",
            "اگر متن قوانین عوض شود دوباره موافقت بگیر",
        ),
    ],
    "قوانین خرید پلن کاربر": [
        ("terms_buy_user_enabled", "فعال", "toggle", "قبل از ثبت سفارش خرید کاربر"),
        (
            "terms_buy_user_text",
            "متن قوانین خرید کاربر",
            "textarea",
            "برای ایموجی پریمیوم از ویرایش داخل ربات استفاده کنید",
        ),
        ("terms_buy_user_btn", "متن دکمه موافقت", "text", ""),
        (
            "btn_style_terms_buy_user",
            "رنگ دکمه موافقت",
            "btn_color",
            "همان رنگ‌بندی تلگرام؛ از تب رنگبندی هم قابل تغییر است",
        ),
        (
            "terms_buy_user_reaccept",
            "پذیرش مجدد با تغییر متن",
            "toggle",
            "اگر متن قوانین عوض شود دوباره موافقت بگیر",
        ),
    ],
    "قوانین خرید پلن نماینده": [
        ("terms_buy_reseller_enabled", "فعال", "toggle", "قبل از ثبت درخواست/خرید نمایندگی"),
        (
            "terms_buy_reseller_text",
            "متن قوانین خرید نماینده",
            "textarea",
            "برای ایموجی پریمیوم از ویرایش داخل ربات استفاده کنید",
        ),
        ("terms_buy_reseller_btn", "متن دکمه موافقت", "text", ""),
        (
            "btn_style_terms_buy_reseller",
            "رنگ دکمه موافقت",
            "btn_color",
            "همان رنگ‌بندی تلگرام؛ از تب رنگبندی هم قابل تغییر است",
        ),
        (
            "terms_buy_reseller_reaccept",
            "پذیرش مجدد با تغییر متن",
            "toggle",
            "اگر متن قوانین عوض شود دوباره موافقت بگیر",
        ),
    ],
}

# Map tab id → which SETTING_GROUPS cards to show (menu/notifications/update/naming special)
TAB_SETTING_GROUPS: dict[str, list[str]] = {
    "menu": [],
    "welcome": ["خوش‌آمد و هویت"],
    "messages": ["متن پیام‌ها"],
    "buttons": ["متن دکمه‌های منو"],
    "colors": ["رنگ دکمه‌ها"],
    "qr": ["QR اشتراک"],
    "payment": [
        "روش‌های پرداخت",
        "کارت به کارت",
        "درگاه پرداخت",
        "درگاه آنلاین API",
        "تأیید خودکار کارت",
        "رمزارز",
        "استارز تلگرام",
        "متن دکمه‌های پرداخت",
        "حالت تعمیرات و رسید",
        "پاکسازی سفارش‌های معلق",
    ],
    "supports": ["متن پشتیبانی ربات"],
    "loyalty": ["متن دعوت دوستان"],
    "naming": ["نام‌گذاری سرویس در پاسارگارد"],
    "forcejoin": ["کانال اجباری"],
    "terms": ["قوانین ورود به ربات", "قوانین خرید پلن کاربر", "قوانین خرید پلن نماینده"],
    "billing": ["مدیریت PAYG"],
    "notifications": ["هشدار سرویس کاربر", "گزارش و عملیات"],
    "daily_report": ["گزارش روزانه"],
    "backup": ["بکاپ زمان‌بندی"],
    "pwa": [],
    "update": [],
    "ssl": [],
    "bot": [],
    "appearance": [],
}

TOGGLE_KEYS = {
    item[0]
    for fields in SETTING_GROUPS.values()
    for item in fields
    if len(item) >= 3 and item[2] == "toggle"
} | {
    # Managed on /resellers (not a bot-settings tab), still a boolean setting.
    "show_reseller_apply",
}

IMAGE_KEYS = {
    item[0]
    for fields in SETTING_GROUPS.values()
    for item in fields
    if len(item) >= 3 and item[2] == "image"
}

SECRET_KEYS = {
    item[0]
    for fields in SETTING_GROUPS.values()
    for item in fields
    if len(item) >= 3 and item[2] == "password"
}

# Sentinels: empty / keep-marker means "do not overwrite existing secret".
SECRET_KEEP_VALUES = {"", "••••", "****", "__keep__", "__unchanged__"}


def should_keep_secret_value(raw: str | None) -> bool:
    return str(raw if raw is not None else "").strip() in SECRET_KEEP_VALUES


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
        from app.services.payment_destinations import enrich_payment_settings

        data = enrich_payment_settings(data)
        _SETTINGS_CACHE[cache_key] = (now, dict(data))
        return data
    result = await session.execute(select(Setting))
    rows = result.scalars().all()
    data.update({r.key: r.value for r in rows})
    from app.services.payment_destinations import enrich_payment_settings

    data = enrich_payment_settings(data)
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

    # --- Loyalty / referral / terms / org rows that previously BLOCKED delete ---
    # These FKs have no ON DELETE CASCADE. Without this cleanup, session.commit()
    # raises IntegrityError and the user row stays forever (the real "حذف نمی‌شود"
    # bug). Must run BEFORE deleting UserService (spins/redemptions FK to services).
    from app.db.models import (
        ChargeCode,
        DeliveryFailure,
        FunnelEvent,
        LoyaltyDiscountEntitlement,
        LoyaltyReward,
        LuckyWheelPrize,
        LuckyWheelSpin,
        LuckyWheelUserState,
        OrgPrincipal,
        PaymentSettlement,
        PointsRule,
        PointsTransaction,
        ReferralEvent,
        RewardRedemption,
        TermsAcceptance,
    )

    await session.execute(
        update(BotUser).where(BotUser.id == user_id).values(owner_principal_id=None)
    )
    await session.execute(
        update(OrgPrincipal).where(OrgPrincipal.bot_user_id == user_id).values(bot_user_id=None)
    )
    await session.execute(
        update(PaymentSettlement)
        .where(PaymentSettlement.shop_owner_id == user_id)
        .values(shop_owner_id=None)
    )
    await session.execute(
        update(FunnelEvent).where(FunnelEvent.user_id == user_id).values(user_id=None)
    )
    await session.execute(
        update(FunnelEvent).where(FunnelEvent.reseller_id == user_id).values(reseller_id=None)
    )
    await session.execute(
        update(DeliveryFailure)
        .where(DeliveryFailure.reseller_id == user_id)
        .values(reseller_id=None)
    )
    await session.execute(
        update(ChargeCode).where(ChargeCode.reseller_id == user_id).values(reseller_id=None)
    )
    await session.execute(
        update(PointsRule).where(PointsRule.reseller_id == user_id).values(reseller_id=None)
    )
    await session.execute(
        update(LoyaltyReward).where(LoyaltyReward.reseller_id == user_id).values(reseller_id=None)
    )
    await session.execute(
        update(LuckyWheelPrize)
        .where(LuckyWheelPrize.reseller_id == user_id)
        .values(reseller_id=None)
    )
    await session.execute(
        update(LuckyWheelSpin).where(LuckyWheelSpin.reseller_id == user_id).values(reseller_id=None)
    )
    await session.execute(
        update(LuckyWheelUserState)
        .where(LuckyWheelUserState.reseller_id == user_id)
        .values(reseller_id=None)
    )

    await session.execute(
        delete(LoyaltyDiscountEntitlement).where(LoyaltyDiscountEntitlement.user_id == user_id)
    )
    await session.execute(delete(RewardRedemption).where(RewardRedemption.user_id == user_id))
    await session.execute(delete(LuckyWheelSpin).where(LuckyWheelSpin.user_id == user_id))
    await session.execute(
        delete(LuckyWheelUserState).where(LuckyWheelUserState.user_id == user_id)
    )
    pts = list(
        (
            await session.execute(
                select(PointsTransaction.id).where(PointsTransaction.user_id == user_id)
            )
        ).scalars().all()
    )
    if pts:
        await session.execute(
            update(PointsTransaction)
            .where(PointsTransaction.reversed_tx_id.in_(pts))
            .values(reversed_tx_id=None)
        )
        await session.execute(
            delete(PointsTransaction).where(PointsTransaction.user_id == user_id)
        )
    await session.execute(
        delete(ReferralEvent).where(
            (ReferralEvent.referrer_id == user_id) | (ReferralEvent.referred_id == user_id)
        )
    )
    await session.execute(delete(TermsAcceptance).where(TermsAcceptance.bot_user_id == user_id))

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
