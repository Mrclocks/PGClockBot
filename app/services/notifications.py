from __future__ import annotations

"""Admin Telegram notification preferences and dispatch."""

from typing import Any

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import BotUser, Order, Payment
from app.services.message_variables import DOMAIN_QR, render_message_template
from app.services.formatting import (
    format_bytes,
    format_bytes_ratio,
    format_expire,
    format_message,
    format_toman,
    format_user_label,
    info_block,
    kv_line,
)
from app.services.users import get_all_settings, on, set_settings_bulk


def ticket_action_markup(ticket_id: int) -> InlineKeyboardMarkup:
    """Reply / close buttons under ticket notification messages."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💬 پاسخ",
                    callback_data=f"tkt:reply:{int(ticket_id)}",
                ),
                InlineKeyboardButton(
                    text="🗃 بستن",
                    callback_data=f"tkt:close:{int(ticket_id)}",
                ),
            ]
        ]
    )


# (setting_key, title, description, default "1"|"0")
NOTIFY_PREFS: list[tuple[str, str, str, str]] = [
    (
        "notify_new_subscription",
        "اشتراک جدید",
        "وقتی سرویس/اشتراک جدید تحویل شد (خرید کیف‌پول یا تأیید پرداخت)",
        "1",
    ),
    (
        "notify_pending_approval",
        "نیاز به تأیید",
        "رسید کارت‌به‌کارت یا شارژ کیف که باید تأیید/رد شود — با دکمه تأیید و رد",
        "1",
    ),
    (
        "notify_new_order",
        "سفارش جدید",
        "وقتی کاربر سفارش می‌سازد و روش پرداخت را انتخاب می‌کند",
        "0",
    ),
    (
        "notify_wallet_topup",
        "شارژ کیف پول",
        "اطلاع از شارژ موفق کیف پول (بعد از تأیید)",
        "1",
    ),
    (
        "notify_new_ticket",
        "تیکت پشتیبانی",
        "وقتی کاربر تیکت جدید ثبت می‌کند یا پیام می‌فرستد — با دکمه پاسخ و بستن",
        "1",
    ),
    (
        "notify_auto_approve",
        "تأیید خودکار",
        "وقتی رسید به‌صورت خودکار تأیید می‌شود",
        "1",
    ),
    (
        "notify_account_edits",
        "ویرایش حساب کاربران",
        "مسدود/رفع مسدودی، تغییر نقش، لغو نمایندگی و حذف کاربر — رونوشت برای ادمین اصلی",
        "1",
    ),
]

# Platform-owner only — never exposed or stored for reseller / sub-admin shops.
PLATFORM_ONLY_NOTIFY_KEYS: frozenset[str] = frozenset(
    {
        "notify_account_edits",
        "notify_wallet_topup",
    }
)

# Shop notify key → at least one of these feature perms is required to see/toggle it.
SHOP_NOTIFY_PERM_MAP: dict[str, frozenset[str]] = {
    "notify_new_subscription": frozenset({"orders", "payments"}),
    "notify_pending_approval": frozenset({"payments"}),
    "notify_new_order": frozenset({"orders"}),
    "notify_new_ticket": frozenset({"tickets"}),
    "notify_auto_approve": frozenset({"payments"}),
}

_SHOP_NOTIFY_DEFAULTS: dict[str, str] = {
    key: default
    for key, _, _, default in NOTIFY_PREFS
    if key not in PLATFORM_ONLY_NOTIFY_KEYS and key in SHOP_NOTIFY_PERM_MAP
}


def shop_notify_catalog(perms: list[str] | set[str] | None) -> list[tuple[str, str, str, str]]:
    """NOTIFY_PREFS subset a shop staff member may customize, based on feature ACL."""
    from app.services.resellers import with_shop_settings

    perm_set = set(with_shop_settings(list(perms or [])))
    out: list[tuple[str, str, str, str]] = []
    for key, title, help_text, default in NOTIFY_PREFS:
        if key in PLATFORM_ONLY_NOTIFY_KEYS:
            continue
        needed = SHOP_NOTIFY_PERM_MAP.get(key)
        if not needed:
            continue
        if needed & perm_set:
            out.append((key, title, help_text, default))
    return out


def shop_notify_allowed_keys(perms: list[str] | set[str] | None) -> set[str]:
    return {key for key, *_ in shop_notify_catalog(perms)}


async def get_notify_prefs(session: AsyncSession) -> dict[str, bool]:
    """Platform-owner prefs from global Setting (never ResellerSetting)."""
    # reseller_id=0 forces platform scope even if a shop ContextVar is active
    ui = await get_all_settings(session, reseller_id=0)
    return {key: on(ui.get(key, default)) for key, _, _, default in NOTIFY_PREFS}


async def get_shop_notify_prefs(session: AsyncSession, reseller_id: int) -> dict[str, bool]:
    """Shop prefs from ResellerSetting only — never inherit owner/global notify_*."""
    from app.db.models import ResellerSetting

    rid = int(reseller_id)
    keys = list(_SHOP_NOTIFY_DEFAULTS.keys())
    result = await session.execute(
        select(ResellerSetting).where(
            ResellerSetting.reseller_user_id == rid,
            ResellerSetting.key.in_(keys),
        )
    )
    rows = {r.key: r.value for r in result.scalars().all()}
    return {key: on(rows.get(key, _SHOP_NOTIFY_DEFAULTS[key])) for key in keys}


async def save_notify_prefs(session: AsyncSession, form: dict[str, Any]) -> None:
    """Save platform-owner prefs to global Setting only."""
    payload = {
        key: ("1" if form.get(f"s_{key}") in {"1", "on", "true", True} else "0")
        for key, _, _, _ in NOTIFY_PREFS
    }
    # reseller_id=0 forces platform Setting even inside a shop request context
    await set_settings_bulk(session, payload, reseller_id=0)


async def save_shop_notify_prefs(
    session: AsyncSession,
    reseller_id: int,
    form: dict[str, Any],
    *,
    allowed_keys: set[str],
) -> None:
    """Save shop prefs to ResellerSetting. Rejects platform-only / out-of-ACL keys."""
    rid = int(reseller_id)
    safe = {
        key
        for key in allowed_keys
        if key in _SHOP_NOTIFY_DEFAULTS and key not in PLATFORM_ONLY_NOTIFY_KEYS
    }
    payload = {
        key: ("1" if form.get(f"s_{key}") in {"1", "on", "true", True} else "0")
        for key in safe
    }
    if not payload:
        return
    await set_settings_bulk(session, payload, reseller_id=rid)


async def notify_enabled(
    session: AsyncSession,
    key: str,
    *,
    reseller_id: int | None = None,
) -> bool:
    if reseller_id is not None:
        if key in PLATFORM_ONLY_NOTIFY_KEYS or key not in _SHOP_NOTIFY_DEFAULTS:
            return False
        prefs = await get_shop_notify_prefs(session, int(reseller_id))
        return bool(prefs.get(key, False))
    prefs = await get_notify_prefs(session)
    return bool(prefs.get(key, False))


async def _send_to_chats(
    bot: Bot,
    chat_ids: list[int],
    text: str,
    *,
    markup: InlineKeyboardMarkup | None = None,
    photo: str | None = None,
) -> int:
    import asyncio
    import logging

    log = logging.getLogger(__name__)
    targets: list[int] = []
    seen: set[int] = set()
    for raw in chat_ids:
        try:
            aid = int(raw)
        except (TypeError, ValueError):
            continue
        if aid <= 0 or aid in seen:
            continue
        seen.add(aid)
        targets.append(aid)

    if not targets:
        return 0

    ok = 0
    lock = asyncio.Lock()

    async def _one(admin_id: int) -> None:
        nonlocal ok
        try:
            if photo:
                await bot.send_photo(
                    admin_id,
                    photo=photo,
                    caption=text[:1024],
                    reply_markup=markup,
                    parse_mode="HTML",
                )
            else:
                await bot.send_message(
                    admin_id, text, reply_markup=markup, parse_mode="HTML"
                )
            async with lock:
                ok += 1
        except Exception:
            log.warning("notify send failed chat_id=%s", admin_id, exc_info=True)
            if photo:
                try:
                    await bot.send_message(
                        admin_id, text, reply_markup=markup, parse_mode="HTML"
                    )
                    async with lock:
                        ok += 1
                except Exception:
                    pass

    await asyncio.gather(
        *(_one(admin_id) for admin_id in targets),
        return_exceptions=True,
    )
    return ok


async def _send_admins(
    bot: Bot,
    text: str,
    *,
    markup: InlineKeyboardMarkup | None = None,
    photo: str | None = None,
    extra_chat_ids: list[int] | None = None,
) -> None:
    """Legacy helper: platform ADMIN_IDS + optional extras (same gate already applied)."""
    await _send_to_chats(
        bot,
        list(get_settings().admin_ids) + list(extra_chat_ids or []),
        text,
        markup=markup,
        photo=photo,
    )


# Distinguishes "caller omitted ticket scope" from "explicit platform ticket (None)".
_TICKET_RID_UNSET = object()


async def _resolve_shop_reseller_id(
    session: AsyncSession,
    *,
    order: Order | None = None,
    payment: Payment | None = None,
    ticket_user_id: int | None = None,
    ticket_reseller_id: object = _TICKET_RID_UNSET,
) -> int | None:
    if order is not None and order.reseller_id:
        return int(order.reseller_id)
    if payment is not None:
        if payment.is_wallet_topup:
            return None
        if payment.order_id:
            ord_row = await session.get(Order, payment.order_id)
            if ord_row and ord_row.reseller_id:
                return int(ord_row.reseller_id)
    # Explicit Ticket.reseller_id (including None = platform) is authoritative
    if ticket_reseller_id is not _TICKET_RID_UNSET:
        return int(ticket_reseller_id) if ticket_reseller_id is not None else None
    # Legacy fallback only when ticket scope was not passed
    if ticket_user_id is not None:
        user = await session.get(BotUser, int(ticket_user_id))
        if user and user.reseller_id:
            return int(user.reseller_id)
    return None


async def _shop_recipient_chat_ids(
    session: AsyncSession,
    reseller_id: int,
    notify_key: str,
) -> list[int]:
    """Owner + bot_admin_ids for a shop, if profile may receive this notify key.

    Ticket notifies always target the shop owner (+ bot admins) — feature ACL must
    not silently drop delivery (empty web_permissions previously blocked all DMs).
    """
    import logging

    from app.db.models import ResellerProfile
    from app.services.reseller_access import parse_telegram_ids
    from app.services.resellers import has_bot_perm

    log = logging.getLogger(__name__)
    if notify_key in PLATFORM_ONLY_NOTIFY_KEYS:
        return []
    needed = SHOP_NOTIFY_PERM_MAP.get(notify_key)
    if not needed:
        return []

    profile = (
        await session.execute(
            select(ResellerProfile).where(ResellerProfile.user_id == int(reseller_id))
        )
    ).scalar_one_or_none()
    if profile is None or not profile.is_active:
        log.warning("shop notify: no active profile rid=%s key=%s", reseller_id, notify_key)
        return []
    # Ticket DMs are mandatory for the shop owner; other keys still respect ACL
    if notify_key != "notify_new_ticket":
        if not any(has_bot_perm(profile, p) for p in needed):
            log.warning(
                "shop notify: ACL denied rid=%s key=%s needed=%s",
                reseller_id,
                notify_key,
                sorted(needed),
            )
            return []

    from app.services.platform_identity import (
        deliverable_telegram_id,
        is_synthetic_telegram_id,
    )

    ids: list[int] = []
    seen: set[int] = set()
    owner = await session.get(BotUser, int(reseller_id))
    if owner and owner.telegram_id is not None:
        raw_tid = int(owner.telegram_id)
        tid = deliverable_telegram_id(raw_tid)
        if tid is not None:
            seen.add(tid)
            ids.append(tid)
        elif is_synthetic_telegram_id(raw_tid):
            log.warning(
                "shop notify: owner telegram_id is synthetic/invalid rid=%s tid=%s",
                reseller_id,
                raw_tid,
            )
    for raw in parse_telegram_ids(profile.bot_admin_ids):
        tid = deliverable_telegram_id(raw)
        if tid is not None and tid not in seen:
            seen.add(tid)
            ids.append(tid)
    if not ids:
        log.warning(
            "shop notify: no deliverable chat ids rid=%s key=%s",
            reseller_id,
            notify_key,
        )
    return ids


async def _dispatch_dual_notify(
    bot: Bot,
    session: AsyncSession,
    key: str,
    text: str,
    *,
    markup: InlineKeyboardMarkup | None = None,
    photo: str | None = None,
    order: Order | None = None,
    payment: Payment | None = None,
    ticket_user_id: int | None = None,
    ticket_reseller_id: object = _TICKET_RID_UNSET,
    platform: bool = True,
    shop: bool = True,
) -> int:
    """Route staff notifications with hard shop isolation.

    Returns the number of successful Telegram deliveries (0 = nobody got it).

    If the event belongs to a reseller shop, ONLY that shop's staff are notified
    (via the shop bot) — platform ADMIN_IDS never receive shop-scoped events.
    Platform customers (no reseller_id) notify ADMIN_IDS only.
    """
    import logging

    log = logging.getLogger(__name__)
    rid: int | None = None
    if shop and key not in PLATFORM_ONLY_NOTIFY_KEYS:
        rid = await _resolve_shop_reseller_id(
            session,
            order=order,
            payment=payment,
            ticket_user_id=ticket_user_id,
            ticket_reseller_id=ticket_reseller_id,
        )

    # --- Shop-scoped: never fan-out to platform owner ---
    if rid is not None:
        # Ticket DMs always attempt delivery; other keys respect shop notify prefs
        if key != "notify_new_ticket" and not await notify_enabled(session, key, reseller_id=rid):
            log.warning("shop notify disabled key=%s rid=%s", key, rid)
            return 0
        targets = await _shop_recipient_chat_ids(session, rid, key)
        if not targets:
            return 0
        from app.services.reseller_bots import open_notify_bot_for_reseller
        from app.services.resellers import get_reseller_profile

        shop_bot = None
        should_close = False
        # Prefer the live bot that received the user update when it is this shop
        profile = await get_reseller_profile(session, int(rid))
        live_token = (getattr(bot, "token", None) or "").strip()
        shop_token = ((profile.bot_token if profile else None) or "").strip()
        if live_token and shop_token and live_token == shop_token:
            shop_bot, should_close = bot, False
        else:
            shop_bot, should_close = await open_notify_bot_for_reseller(session, rid)
        if shop_bot is None:
            log.warning("shop notify: no bot for rid=%s key=%s", rid, key)
            return 0
        try:
            return await _send_to_chats(
                shop_bot, targets, text, markup=markup, photo=photo
            )
        finally:
            if should_close:
                try:
                    await shop_bot.session.close()
                except Exception:
                    pass

    # --- Platform-only customers / wallet / account edits ---
    if platform and await notify_enabled(session, key):
        return await _send_to_chats(
            bot,
            list(get_settings().admin_ids),
            text,
            markup=markup,
            photo=photo,
        )
    return 0


async def _ticket_reseller_chat_ids(session: AsyncSession, ticket_user_id: int) -> list[int]:
    """Telegram ids of the customer's reseller shop (owner + bot admins) if tickets allowed."""
    rid = await _resolve_shop_reseller_id(session, ticket_user_id=ticket_user_id)
    if not rid:
        return []
    return await _shop_recipient_chat_ids(session, rid, "notify_new_ticket")


async def _shop_owner_chat_ids(
    session: AsyncSession,
    *,
    order: Order | None = None,
    payment: Payment | None = None,
) -> list[int]:
    """Backward-compatible helper — prefer _dispatch_dual_notify for new code."""
    rid = await _resolve_shop_reseller_id(session, order=order, payment=payment)
    if not rid:
        return []
    # Generic recipients without a specific key filter beyond payments/orders presence
    from app.db.models import ResellerProfile
    from app.services.reseller_access import parse_telegram_ids

    profile = (
        await session.execute(select(ResellerProfile).where(ResellerProfile.user_id == rid))
    ).scalar_one_or_none()
    if profile is not None and not profile.is_active:
        return []
    ids: list[int] = []
    seen: set[int] = set()
    owner = await session.get(BotUser, rid)
    if owner and owner.telegram_id:
        tid = int(owner.telegram_id)
        seen.add(tid)
        ids.append(tid)
    if profile is not None:
        for tid in parse_telegram_ids(profile.bot_admin_ids):
            if tid not in seen:
                seen.add(tid)
                ids.append(tid)
    return ids


def _approval_markup(
    *,
    order_id: int | None = None,
    payment_id: int | None = None,
    ui: dict | None = None,
) -> InlineKeyboardMarkup:
    """Review buttons. Prefer payrev (shop + platform can use); ordrev is platform-only."""
    from app.services.button_styles import style_kwargs

    if payment_id is not None:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ تأیید",
                        callback_data=f"payrev:ok:{payment_id}",
                        **style_kwargs(ui, "confirm", fallback="success"),
                    ),
                    InlineKeyboardButton(
                        text="❌ رد",
                        callback_data=f"payrev:no:{payment_id}",
                        **style_kwargs(ui, "reject", fallback="danger"),
                    ),
                ]
            ]
        )
    assert order_id is not None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ تأیید",
                    callback_data=f"ordrev:ok:{order_id}",
                    **style_kwargs(ui, "confirm", fallback="success"),
                ),
                InlineKeyboardButton(
                    text="❌ رد",
                    callback_data=f"ordrev:no:{order_id}",
                    **style_kwargs(ui, "reject", fallback="danger"),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📋 جزئیات سفارش",
                    callback_data=f"adm:order:{order_id}",
                )
            ],
        ]
    )


async def notify_new_subscription(
    bot: Bot,
    session: AsyncSession,
    *,
    order: Order,
    user_tg_id: int | None,
    user_name: str | None = None,
    plan_name: str | None = None,
    needs_approval: bool = False,
) -> None:
    """Inform platform admins and/or shop staff that a subscription was delivered."""
    settings = get_settings()
    if not user_name:
        user = await session.get(BotUser, order.user_id) if order.user_id else None
        user_name = format_user_label(user, telegram_id=user_tg_id)
    lines = [
        kv_line("🧾", "سفارش", f"#{order.id}"),
        kv_line("👤", "کاربر", user_name or "—"),
        kv_line("💰", "مبلغ", format_toman(order.amount, settings.currency)),
    ]
    if plan_name:
        lines.append(kv_line("💎", "پلن", plan_name))
    if order.payment_method:
        method = "کیف پول" if order.payment_method == "wallet" else "کارت به کارت"
        lines.append(kv_line("💳", "پرداخت", method))
    lines.append(kv_line("📌", "وضعیت", "نیاز به تأیید" if needs_approval else "تحویل‌شده"))
    text = format_message("🆕 اشتراک جدید", info_block(lines))
    markup = _approval_markup(order_id=order.id) if needs_approval else None
    await _dispatch_dual_notify(
        bot,
        session,
        "notify_new_subscription",
        text,
        markup=markup,
        order=order,
    )


async def notify_pending_approval(
    bot: Bot,
    session: AsyncSession,
    payment: Payment,
    user_tg_id: int | None,
    *,
    user_name: str | None = None,
) -> None:
    settings = get_settings()
    user = await session.get(BotUser, payment.user_id) if payment.user_id else None
    user_label = user_name or format_user_label(user, telegram_id=user_tg_id)

    lines = [
        kv_line("🧾", "پرداخت", f"#{payment.id}"),
        kv_line("💰", "مبلغ", format_toman(payment.amount, settings.currency)),
        kv_line("👤", "کاربر", user_label),
    ]

    if payment.is_wallet_topup:
        lines.append(kv_line("📦", "نوع", "شارژ کیف پول"))
        lines.append(kv_line("📝", "بابت", "افزایش موجودی کیف پول کاربر"))
    else:
        order = None
        if payment.order_id:
            order = await session.get(Order, payment.order_id)
        kind, detail_lines = await _pending_order_detail_lines(session, order, payment)
        lines.append(kv_line("📦", "نوع", kind))
        if payment.order_id:
            lines.append(kv_line("🛒", "سفارش", f"#{payment.order_id}"))
        lines.extend(detail_lines)

    text = format_message("⏳ نیاز به تأیید", info_block(lines) + "\n\nاز دکمه‌های زیر تأیید یا رد کنید.")
    # Always payrev so shop staff can act on their own bot; ordrev is platform-only.
    markup = _approval_markup(payment_id=payment.id)
    # Wallet top-ups are platform-only; shop prefs never apply.
    # Local Mini App uploads are not Telegram file_ids — text-only notify.
    photo = payment.receipt_file_id
    if photo and str(photo).startswith("local:"):
        photo = None
        text = text + "\n\n📎 رسید در مینی‌اپ / پنل وب قابل مشاهده است."
    await _dispatch_dual_notify(
        bot,
        session,
        "notify_pending_approval",
        text,
        markup=markup,
        photo=photo,
        payment=payment,
        shop=not payment.is_wallet_topup,
    )


async def _pending_order_detail_lines(
    session: AsyncSession,
    order: Order | None,
    payment: Payment,
) -> tuple[str, list[str]]:
    """Return (kind_label, extra kv lines) describing what the receipt is for."""
    from app.db.models import Plan
    from app.services.orders import order_quantity

    if order is None:
        return "خرید اشتراک", [kv_line("📝", "بابت", "رسید خرید — جزئیات سفارش در دسترس نیست")]

    note = (order.note or "").strip()
    qty = order_quantity(order)
    plan = await session.get(Plan, order.plan_id) if order.plan_id else None
    plan_name = (plan.name if plan else None) or "—"
    extras: list[str] = []

    if note.startswith("reseller_app:"):
        kind = "درخواست نمایندگی"
        extras.append(kv_line("📝", "بابت", "پرداخت هزینه پلن نمایندگی"))
        plan_name = "—"
        try:
            app_id = int(note.split(":", 1)[1])
        except ValueError:
            app_id = 0
        if app_id:
            from app.db.models import ResellerApplication, ResellerPlan
            from app.services.resellers import (
                format_reseller_plan_apply_detail,
                reseller_billing_mode_label,
                reseller_plan_mode_of,
            )

            app = await session.get(ResellerApplication, app_id)
            rplan = (
                await session.get(ResellerPlan, app.plan_id)
                if app and app.plan_id
                else None
            )
            if rplan is not None:
                plan_name = rplan.name or "—"
                extras.append(kv_line("💎", "پلن", plan_name))
                extras.append(
                    kv_line(
                        "🏷",
                        "نوع",
                        reseller_billing_mode_label(reseller_plan_mode_of(rplan)),
                    )
                )
                # PAYG / fixed detail lines (rate, groups)
                detail = format_reseller_plan_apply_detail(
                    rplan, currency=get_settings().currency
                )
                for line in (detail or "").splitlines():
                    raw = (line or "").strip()
                    if not raw or raw.startswith("نوع:") or raw.startswith("قیمت ورود:"):
                        continue
                    # Strip simple HTML tags from detail for notify kv lines
                    plain = (
                        raw.replace("<b>", "")
                        .replace("</b>", "")
                        .replace("<code>", "")
                        .replace("</code>", "")
                    )
                    if ":" in plain:
                        label, _, val = plain.partition(":")
                        extras.append(kv_line("•", label.strip(), val.strip() or "—"))
                return kind, extras
        extras.append(kv_line("💎", "پلن", plan_name))
        return kind, extras

    if note.startswith("renew:"):
        kind = "تمدید اشتراک"
        extras.append(kv_line("📝", "بابت", f"تمدید سرویس با پلن «{plan_name}»"))
    elif qty > 1 or note.startswith("wholesale:"):
        kind = "خرید عمده"
        extras.append(
            kv_line("📝", "بابت", f"خرید عمده {qty} سرویس از پلن «{plan_name}»")
        )
        extras.append(kv_line("📦", "تعداد", str(qty)))
    else:
        kind = "خرید اشتراک"
        extras.append(kv_line("📝", "بابت", f"خرید پلن «{plan_name}»"))

    extras.append(kv_line("💎", "پلن", plan_name))
    if plan is not None:
        if plan.data_limit_gb is not None:
            extras.append(kv_line("📶", "حجم پلن", f"{plan.data_limit_gb} گیگ"))
        if plan.duration_days:
            extras.append(kv_line("📅", "مدت پلن", f"{plan.duration_days} روز"))
    if order.payment_method:
        method_map = {
            "wallet": "کیف پول",
            "card": "کارت به کارت",
            "gateway": "درگاه",
            "crypto": "رمزارز",
            "stars": "استارز",
        }
        extras.append(
            kv_line(
                "💳",
                "روش",
                method_map.get(str(order.payment_method), str(order.payment_method)),
            )
        )
    return kind, extras


async def notify_new_order(
    bot: Bot,
    session: AsyncSession,
    *,
    order: Order,
    user_tg_id: int | None,
    plan_name: str | None = None,
    user_name: str | None = None,
) -> None:
    settings = get_settings()
    if not user_name:
        user = await session.get(BotUser, order.user_id) if order.user_id else None
        user_name = format_user_label(user, telegram_id=user_tg_id)
    lines = [
        kv_line("🧾", "سفارش", f"#{order.id}"),
        kv_line("👤", "کاربر", user_name or "—"),
        kv_line("💰", "مبلغ", format_toman(order.amount, settings.currency)),
    ]
    if plan_name:
        lines.append(kv_line("💎", "پلن", plan_name))
    text = format_message("🛒 سفارش جدید", info_block(lines))
    await _dispatch_dual_notify(
        bot,
        session,
        "notify_new_order",
        text,
        order=order,
    )


async def notify_wallet_topup_ok(
    bot: Bot,
    session: AsyncSession,
    payment: Payment,
    user_tg_id: int | None,
) -> None:
    if not await notify_enabled(session, "notify_wallet_topup"):
        return
    settings = get_settings()
    user = await session.get(BotUser, payment.user_id) if payment.user_id else None
    text = format_message(
        "💰 شارژ کیف پول",
        info_block(
            [
                kv_line("🧾", "پرداخت", f"#{payment.id}"),
                kv_line("💵", "مبلغ", format_toman(payment.amount, settings.currency)),
                kv_line("👤", "کاربر", format_user_label(user, telegram_id=user_tg_id)),
                kv_line("✅", "وضعیت", "تأیید و واریز شد"),
            ]
        ),
    )
    await _dispatch_dual_notify(
        bot,
        session,
        "notify_wallet_topup",
        text,
        payment=payment,
        shop=False,
    )


async def notify_new_ticket(
    bot: Bot,
    session: AsyncSession,
    *,
    ticket_id: int,
    subject: str,
    user_name: str | None,
    ticket_user_id: int | None = None,
    ticket_reseller_id: object = _TICKET_RID_UNSET,
) -> int:
    import html as html_mod

    text = format_message(
        "🎫 تیکت جدید",
        info_block(
            [
                kv_line("🔢", "شماره", f"#{ticket_id}"),
                kv_line("👤", "از", html_mod.escape(user_name or "—")),
                kv_line("📝", "موضوع", html_mod.escape(subject or "—")),
            ]
        ),
    )
    return await _dispatch_dual_notify(
        bot,
        session,
        "notify_new_ticket",
        text,
        markup=ticket_action_markup(ticket_id),
        ticket_user_id=ticket_user_id,
        ticket_reseller_id=ticket_reseller_id,
    )


async def notify_ticket_message(
    bot: Bot,
    session: AsyncSession,
    *,
    ticket_id: int,
    subject: str | None,
    body: str,
    from_staff: bool,
    ticket_user_id: int,
    actor_name: str | None = None,
    ticket_reseller_id: object = _TICKET_RID_UNSET,
) -> None:
    """Notify the other party of a ticket reply — always with پاسخ / بستن buttons."""
    import html as html_mod

    markup = ticket_action_markup(ticket_id)
    preview = (body or "").strip()
    if len(preview) > 500:
        preview = preview[:500] + "…"

    if from_staff:
        user = await session.get(BotUser, int(ticket_user_id))
        if not user or not user.telegram_id:
            return
        text = format_message(
            "💬 پاسخ پشتیبانی",
            info_block(
                [
                    kv_line("🔢", "تیکت", f"#{ticket_id}"),
                    kv_line("📝", "موضوع", html_mod.escape(subject or "—")),
                    kv_line("💬", "پیام", html_mod.escape(preview or "—")),
                ]
            ),
        )
        send_bot = bot
        close_bot = False
        if ticket_reseller_id is not _TICKET_RID_UNSET:
            rid = int(ticket_reseller_id) if ticket_reseller_id is not None else None
        else:
            from app.db.models import Ticket as TicketModel

            trow = await session.get(TicketModel, int(ticket_id))
            rid = int(trow.reseller_id) if trow is not None and trow.reseller_id else None
        if rid:
            from app.services.reseller_bots import open_notify_bot_for_reseller

            shop_bot, should_close = await open_notify_bot_for_reseller(session, int(rid))
            if shop_bot is not None:
                send_bot = shop_bot
                close_bot = should_close
        try:
            await send_bot.send_message(
                int(user.telegram_id), text, reply_markup=markup, parse_mode="HTML"
            )
        except Exception:
            pass
        finally:
            if close_bot:
                try:
                    await send_bot.session.close()
                except Exception:
                    pass
        return

    text = format_message(
        "🎫 پیام جدید تیکت",
        info_block(
            [
                kv_line("🔢", "تیکت", f"#{ticket_id}"),
                kv_line("👤", "از", html_mod.escape(actor_name or "کاربر")),
                kv_line("📝", "موضوع", html_mod.escape(subject or "—")),
                kv_line("💬", "پیام", html_mod.escape(preview or "—")),
            ]
        ),
    )
    await _dispatch_dual_notify(
        bot,
        session,
        "notify_new_ticket",
        text,
        markup=markup,
        ticket_user_id=ticket_user_id,
        ticket_reseller_id=ticket_reseller_id,
    )


async def notify_auto_approve(
    bot: Bot,
    session: AsyncSession,
    payment: Payment,
) -> None:
    text = format_message(
        "⚡ تأیید خودکار",
        info_block([kv_line("🧾", "پرداخت", f"#{payment.id}"), kv_line("✅", "نتیجه", "خودکار تأیید شد")]),
    )
    await _dispatch_dual_notify(
        bot,
        session,
        "notify_auto_approve",
        text,
        payment=payment,
        shop=not payment.is_wallet_topup,
    )


def build_qr_caption(
    *,
    sub_url: str,
    ui: dict[str, str] | None = None,
    info: dict | None = None,
    data_limit: float | int | None = None,
    expire: Any = None,
    username: str | None = None,
) -> str:
    """
    Full QR caption: custom template (optional) + link + volume + expire.
    Telegram caption limit is 1024 chars.
    """
    ui = ui or {}
    custom = (ui.get("qr_caption") or "").strip()
    if custom:
        try:
            custom = render_message_template(
                custom, domain=DOMAIN_QR, url=sub_url
            )
        except Exception:
            pass

    used = None
    limit = data_limit
    exp = expire
    uname = username
    if info:
        used = info.get("used_traffic")
        if limit is None:
            limit = info.get("data_limit")
        if exp is None:
            exp = info.get("expire")
        if not uname:
            uname = info.get("username")

    lines: list[str] = []
    if custom:
        lines.append(custom)
    else:
        lines.append("📱 <b>QR اشتراک</b>")
        lines.append("<i>با دوربین اسکن کنید یا در کلاینت Import کنید</i>")

    lines.append("")
    if uname:
        from app.services.formatting import copyable

        lines.append(f"👤 {copyable(uname)}")
    if used is not None or limit is not None:
        vol = (
            format_bytes_ratio(used, limit, joiner=" از ")
            if used is not None
            else format_bytes(limit)
        )
        lines.append(f"📦 حجم: <b>{vol}</b>")
    if exp is not None or info is not None:
        lines.append(f"⏱ زمان: <b>{format_expire(exp)}</b>")
    # Honor panel toggle «نمایش لینک در کپشن QR» (same as delivery text path)
    from app.services.formatting import copyable
    from app.services.users import on as _on

    if _on(ui.get("show_sub_link_in_text", "1")):
        lines.append("")
        lines.append("🔗 لینک اشتراک:")
        lines.append(copyable(sub_url))
    caption = "\n".join(lines).strip()
    return caption[:1024]


# —— Account moderation notices (block / role / revoke / delete) ——

ROLE_LABELS_FA: dict[str, str] = {
    "user": "کاربر",
    "reseller": "نماینده",
    "admin": "مدیر",
    "pg_staff": "ادمین پاسارگارد",
}

_EVENT_TITLES_FA: dict[str, str] = {
    "block": "🚫 حساب شما مسدود شد",
    "unblock": "✅ مسدودیت برداشته شد",
    "role": "🔄 نقش شما تغییر کرد",
    "reseller_revoke": "❌ نمایندگی شما حذف شد",
    "user_delete": "🗑 حساب کاربری حذف شد",
}

_EVENT_ADMIN_TITLES_FA: dict[str, str] = {
    "block": "🚫 مسدودسازی کاربر",
    "unblock": "✅ رفع مسدودی کاربر",
    "role": "🔄 تغییر نقش کاربر",
    "reseller_revoke": "❌ لغو نمایندگی",
    "user_delete": "🗑 حذف کاربر",
}


def role_label_fa(role: str | None) -> str:
    key = (role or "").strip()
    return ROLE_LABELS_FA.get(key, key or "—")


def actor_label_from_staff(staff: dict | None) -> str | None:
    if not staff:
        return None
    for key in ("username", "name", "full_name", "web_username"):
        val = (staff.get(key) or "").strip() if isinstance(staff.get(key), str) else ""
        if val:
            return val
    role = staff.get("role")
    if role:
        return role_label_fa(str(role))
    return "ادمین پنل"


def format_account_edit_subject(
    *,
    event: str,
    reason: str | None = None,
    old_role: str | None = None,
    new_role: str | None = None,
) -> str:
    """HTML message for the affected user."""
    reason = (reason or "").strip()
    title = _EVENT_TITLES_FA.get(event, "ℹ️ به‌روزرسانی حساب")
    lines: list[str] = []

    if event == "block":
        lines.append("دسترسی شما به ربات فعلاً غیرفعال است.")
    elif event == "unblock":
        lines.append("حساب شما دوباره فعال شد و می‌توانید از ربات استفاده کنید.")
    elif event == "role":
        lines.append(kv_line("📌", "نقش قبلی", role_label_fa(old_role)))
        lines.append(kv_line("📌", "نقش جدید", role_label_fa(new_role)))
    elif event == "reseller_revoke":
        lines.append("دسترسی پنل نماینده و ادمین پاسارگارد مرتبط لغو شده است.")
        lines.append("نقش شما به «کاربر» برگشت.")
    elif event == "user_delete":
        lines.append("حساب و داده‌های مرتبط شما از ربات حذف شد.")
    else:
        lines.append("وضعیت حساب شما به‌روز شد.")

    if reason:
        lines.append("")
        lines.append(kv_line("📝", "علت", reason))
    lines.append("")
    lines.append("در صورت نیاز با پشتیبانی در ارتباط باشید.")
    return format_message(title, "\n".join(lines))


def format_account_edit_admin(
    *,
    event: str,
    user: BotUser,
    reason: str | None = None,
    old_role: str | None = None,
    new_role: str | None = None,
    actor: str | None = None,
) -> str:
    """HTML mirror for platform admins."""
    reason = (reason or "").strip()
    title = _EVENT_ADMIN_TITLES_FA.get(event, "ℹ️ ویرایش حساب")
    name = user.full_name or user.username or "—"
    lines = [
        kv_line("👤", "کاربر", f"{name} (<code>{user.telegram_id}</code>)"),
        kv_line("🏷", "نقش فعلی", role_label_fa(user.role)),
    ]
    if event == "role":
        lines.append(kv_line("📤", "از", role_label_fa(old_role)))
        lines.append(kv_line("📥", "به", role_label_fa(new_role)))
    elif event == "block":
        lines.append(kv_line("📌", "وضعیت", "مسدود شد"))
    elif event == "unblock":
        lines.append(kv_line("📌", "وضعیت", "رفع مسدودی"))
    elif event == "reseller_revoke":
        lines.append(kv_line("📌", "عملیات", "لغو نمایندگی"))
    elif event == "user_delete":
        lines.append(kv_line("📌", "عملیات", "حذف کامل حساب"))
    if reason:
        lines.append(kv_line("📝", "علت", reason))
    if actor:
        lines.append(kv_line("🛠", "توسط", actor))
    return format_message(title, info_block(lines))


async def _send_to_user_chat(
    session: AsyncSession,
    user: BotUser,
    text: str,
) -> bool:
    """Notify the user via main bot and, when available, their shop/reseller bot.

    Block / unblock / delete / role messages must reach Telegram even if the person
    primarily uses a dedicated shop bot — and vice versa.
    """
    if not user.telegram_id:
        return False
    from app.bot import create_bot
    from app.services.reseller_bots import open_notify_bot_for_reseller

    chat_id = int(user.telegram_id)
    sent = False
    tokens_used: set[str] = set()

    # 1) Always try the main platform bot
    try:
        bot = create_bot()
        try:
            await bot.send_message(chat_id, text, parse_mode="HTML")
            sent = True
            tok = getattr(bot, "token", None)
            if tok:
                tokens_used.add(str(tok))
        finally:
            await bot.session.close()
    except Exception:
        pass

    # 2) Also shop bot: customer of a shop, or own reseller dedicated bot
    shop_targets: list[int] = []
    reseller_id = getattr(user, "reseller_id", None)
    if reseller_id:
        shop_targets.append(int(reseller_id))
    if getattr(user, "role", None) == "reseller" and int(user.id) not in shop_targets:
        shop_targets.append(int(user.id))

    for rid in shop_targets:
        shop_bot = None
        should_close = False
        try:
            shop_bot, should_close = await open_notify_bot_for_reseller(session, rid)
            if shop_bot is None:
                continue
            tok = str(getattr(shop_bot, "token", "") or "")
            if tok and tok in tokens_used:
                continue
            await shop_bot.send_message(chat_id, text, parse_mode="HTML")
            sent = True
            if tok:
                tokens_used.add(tok)
        except Exception:
            pass
        finally:
            if should_close and shop_bot is not None:
                try:
                    await shop_bot.session.close()
                except Exception:
                    pass

    return sent


async def notify_account_edit(
    session: AsyncSession,
    *,
    user: BotUser,
    event: str,
    reason: str | None = None,
    old_role: str | None = None,
    new_role: str | None = None,
    actor: str | None = None,
    notify_subject: bool = True,
) -> dict[str, bool]:
    """Notify the affected user and (optionally) platform admins.

    Returns ``{"subject": bool, "admins": bool}``.
    """
    result = {"subject": False, "admins": False}
    reason = (reason or "").strip() or None

    subject_text = format_account_edit_subject(
        event=event,
        reason=reason,
        old_role=old_role,
        new_role=new_role,
    )
    admin_text = format_account_edit_admin(
        event=event,
        user=user,
        reason=reason,
        old_role=old_role,
        new_role=new_role,
        actor=actor,
    )

    if notify_subject:
        result["subject"] = await _send_to_user_chat(session, user, subject_text)

    if await notify_enabled(session, "notify_account_edits"):
        try:
            from app.bot import create_bot

            bot = create_bot()
            try:
                await _send_admins(bot, admin_text)
                result["admins"] = True
            finally:
                await bot.session.close()
        except Exception:
            result["admins"] = False

    return result
