from __future__ import annotations

"""Send clean delivery messages (+ optional subscription QR photo) to users."""

import logging
from typing import Any

from aiogram import Bot
from aiogram.types import BufferedInputFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Payment, UserService
from app.services.formatting import format_message, format_toman, info_block, kv_line, service_card, copyable
from app.bot import keyboards as kb
from app.config import get_settings
from app.services.pasarguard import absolutize_subscription_url, get_pg
from app.services.qrcode_gen import make_subscription_qr
from app.services.message_variables import DOMAIN_ORDER, DOMAIN_WALLET, render_message_template
from app.services.users import get_all_settings, on

logger = logging.getLogger(__name__)


def _plan_name(order) -> str:
    try:
        plan = vars(order).get("plan")
        if plan is not None:
            return getattr(plan, "name", "") or ""
    except Exception:
        pass
    return ""


def _plan_type_label(order) -> str:
    """Persian plan-kind label for delivery messages (user or reseller)."""
    note = (getattr(order, "note", None) or "").strip()
    if note.startswith("svc_addon:"):
        return "بسته حجم/زمان"
    if note.startswith("renew:"):
        return "تمدید"
    if note.startswith("reseller_app:") or note.startswith("reseller_renew:"):
        return "اشتراک نمایندگی"
    if note.startswith("wholesale:") or note == "wholesale":
        return "فروش عمده"
    if note == "custom" or note.startswith("custom:"):
        return "دلخواه"
    try:
        plan = vars(order).get("plan")
        if plan is not None and bool(getattr(plan, "is_trial", False)):
            return "تست"
        # ResellerPlan billing_mode
        bm = (getattr(plan, "billing_mode", None) or "").strip().lower()
        if bm == "payg":
            return "PAYG"
        pk = (getattr(plan, "plan_kind", None) or "").strip().lower()
        if pk in {"addon_volume", "addon_users"}:
            return "بسته نماینده"
        if pk == "subscription" or bm == "fixed":
            return "اشتراک ثابت"
    except Exception:
        pass
    return "ثابت"


def _plan_delivery_line(order) -> str | None:
    """Always-shown plan type (+ name) line for delivery cards."""
    kind = _plan_type_label(order)
    name = (_plan_name(order) or "").strip()
    if name and kind:
        return kv_line("💎", "نوع پلن", f"<b>{kind}</b> — {name}")
    if name:
        return kv_line("💎", "پلن", name)
    if kind:
        return kv_line("💎", "نوع پلن", f"<b>{kind}</b>")
    return None


def _delivery_qr_header(ui: dict[str, str], order) -> str:
    """Short success header merged into the QR caption (avoids a duplicate text bubble)."""
    from app.services.rich_text import rich_plain_text

    title = rich_plain_text(ui.get("delivery_title")) or "✅ سرویس آماده است"
    parts: list[str] = [title]
    body = _rendered_purchase_success_body(ui, order).strip()
    if body:
        parts.append(body)
    if order is not None and getattr(order, "id", None) is not None:
        # Body templates often already include the order id — avoid a third repeat.
        if f"#{order.id}" not in body and f"سفارش #{order.id}" not in "\n".join(parts):
            parts.append(f"شماره سفارش: #{order.id}")
    plan_line = _plan_delivery_line(order)
    if plan_line:
        parts.append(plan_line)
    return "\n".join(parts).strip()


def _compose_caption(header: str | None, base: str, *, limit: int = 1024) -> str:
    header = (header or "").strip()
    base = (base or "").strip()
    if not header:
        return base[:limit]
    if not base:
        return header[:limit]
    joined = f"{header}\n\n{base}"
    if len(joined) <= limit:
        return joined
    room = limit - len(base) - 2
    if room < 48:
        return base[:limit]
    return f"{header[:room].rstrip()}\n\n{base}"


def _rendered_purchase_success_body(ui: dict[str, str], order) -> str:
    from app.services.rich_text import rich_plain_text

    plain = rich_plain_text(ui.get("purchase_success_text")).strip()
    if not plain:
        return f"سفارش #{order.id} با موفقیت فعال شد."
    try:
        body = render_message_template(
            plain,
            domain=DOMAIN_ORDER,
            order_id=order.id,
            plan_name=_plan_name(order),
            plan_type=_plan_type_label(order),
            shop_title=rich_plain_text(ui.get("shop_title")) or "",
            url=getattr(order, "subscription_url", None) or "",
        ).strip()
    except Exception:
        body = plain
    return body or f"سفارش #{order.id} با موفقیت فعال شد."


def _subscription_success_outbound(ui: dict[str, str], order) -> tuple[str, dict]:
    """Full outbound success card (title + body) with optional entities."""
    from app.services.rich_text import outbound_setting_text, rich_plain_text

    title = rich_plain_text(ui.get("delivery_title")) or "✅ سرویس آماده است"
    raw = ui.get("purchase_success_text")
    if not rich_plain_text(raw).strip():
        return format_message(title, f"سفارش #{order.id} با موفقیت فعال شد."), {}
    text, send_kw = outbound_setting_text(
        raw,
        title=title,
        domain=DOMAIN_ORDER,
        order_id=order.id,
        plan_name=_plan_name(order),
        plan_type=_plan_type_label(order),
        shop_title=rich_plain_text(ui.get("shop_title")) or "",
        url=getattr(order, "subscription_url", None) or "",
    )
    if not (text or "").strip():
        return format_message(title, f"سفارش #{order.id} با موفقیت فعال شد."), {}
    return text, send_kw


async def build_delivery_content(
    session: AsyncSession,
    payment: Payment | None,
    order,
    *,
    include_details: bool = True,
) -> dict[str, Any]:
    """Build title/body/markup/url/info for a successful delivery.

    For subscriptions, ``include_details=False`` yields a short success-only body
    (no service card / sub link); details stay available via sub_info/sub_url for QR.

    Wholesale (qty > 1): never attach a QR URL — links are listed in text only.

    When custom-emoji entities are packed on success texts, ``send_kw`` carries
    ``entities`` / ``parse_mode=None`` and HTML details go in ``detail_text``.
    """
    from app.services.rich_text import outbound_setting_text, rich_plain_text

    shop_rid = getattr(order, "reseller_id", None) if order is not None else None
    ui = await get_all_settings(session, reseller_id=shop_rid)
    markup = kb.back_home(ui)
    title = rich_plain_text(ui.get("delivery_title")) or "✅ سرویس آماده است"
    sub_url = None
    sub_info: dict | None = None

    # Ensure plan is available for نوع پلن / {plan_name} even if caller didn't load it.
    if order is not None and vars(order).get("plan") is None:
        plan_id = getattr(order, "plan_id", None)
        if plan_id:
            from app.db.models import Plan

            order.plan = await session.get(Plan, int(plan_id))

    if order and order.service_id:
        from app.services.orders import order_quantity

        qty = order_quantity(order)
        svc = await session.get(UserService, order.service_id)
        success_text, send_kw = _subscription_success_outbound(ui, order)
        detail_parts: list[str] = []

        plan_line = _plan_delivery_line(order)
        if plan_line:
            detail_parts.append(plan_line)

        if qty > 1:
            detail_parts.append(f"📦 تعداد سرویس تحویل‌شده: <b>{qty}</b>")
            siblings = (
                await session.execute(
                    select(UserService)
                    .where(UserService.remark == f"order:{order.id}")
                    .order_by(UserService.id)
                )
            ).scalars().all()
            if siblings:
                lines = []
                for i, s in enumerate(siblings, 1):
                    uname = s.pg_username or f"#{s.id}"
                    sib_url = absolutize_subscription_url(s.subscription_url) or s.subscription_url
                    if sib_url and on(ui.get("show_sub_link_in_text", "1")):
                        lines.append(
                            f"{i}. {copyable(uname)}\n{copyable(sib_url)}"
                        )
                    else:
                        lines.append(f"{i}. {copyable(uname)}")
                detail_parts.append("\n\n".join(lines))
            detail_html = "\n\n".join(detail_parts)
            if send_kw.get("entities"):
                return {
                    "title": title,
                    "text": success_text,
                    "send_kw": send_kw,
                    "detail_text": format_message("📦 جزئیات سفارش", detail_html)
                    if detail_html
                    else None,
                    "markup": markup,
                    "sub_url": None,
                    "sub_info": None,
                    "ui": ui,
                    "is_subscription": True,
                    "skip_qr": True,
                }
            body = "\n\n".join(
                [p for p in [_rendered_purchase_success_body(ui, order), *detail_parts] if p]
            )
            return {
                "title": title,
                "text": format_message(title, body),
                "send_kw": {},
                "detail_text": None,
                "markup": markup,
                "sub_url": None,
                "sub_info": None,
                "ui": ui,
                "is_subscription": True,
                "skip_qr": True,
            }

        if svc:
            from app.services.service_live_info import fetch_live_service_info

            info = await fetch_live_service_info(svc, client_factory=get_pg)
            info.setdefault("username", svc.pg_username)
            sub_info = info if not info.get("error") else None
            if include_details:
                detail_parts.append(service_card(info))
            sub_url = absolutize_subscription_url(svc.subscription_url) or svc.subscription_url
            if include_details and sub_url and on(ui.get("show_sub_link_in_text", "1")):
                detail_parts.append(
                    info_block(
                        [
                            "🔗 <b>لینک اشتراک</b>",
                            copyable(sub_url),
                        ]
                    )
                )
            markup = kb.service_actions(svc.id, ui)

        if send_kw.get("entities"):
            detail_html = "\n\n".join(detail_parts)
            return {
                "title": title,
                "text": success_text,
                "send_kw": send_kw,
                "detail_text": format_message("📦 جزئیات سرویس", detail_html)
                if detail_html
                else None,
                "markup": markup,
                "sub_url": sub_url,
                "sub_info": sub_info,
                "ui": ui,
                "is_subscription": True,
                "skip_qr": False,
            }

        body = "\n\n".join(
            [p for p in [_rendered_purchase_success_body(ui, order), *detail_parts] if p]
        )
        return {
            "title": title,
            "text": format_message(title, body),
            "send_kw": {},
            "detail_text": None,
            "markup": markup,
            "sub_url": sub_url,
            "sub_info": sub_info,
            "ui": ui,
            "is_subscription": True,
            "skip_qr": False,
        }

    if payment and payment.is_wallet_topup:
        title = rich_plain_text(ui.get("wallet_success_title")) or "💰 شارژ کیف پول"
        amount_txt = format_toman(payment.amount, get_settings().currency)
        text, send_kw = outbound_setting_text(
            ui.get("wallet_success_text")
            or "✅ مبلغ {amount} به کیف پول شما اضافه شد.",
            title=title,
            domain=DOMAIN_WALLET,
            amount=amount_txt,
            payment_id=payment.id,
            shop_title=rich_plain_text(ui.get("shop_title")) or "",
        )
        if not (text or "").strip():
            text = format_message(title, f"✅ کیف پول شما {amount_txt} شارژ شد.")
            send_kw = {}
        return {
            "title": title,
            "text": text,
            "send_kw": send_kw,
            "detail_text": None,
            "markup": markup,
            "sub_url": None,
            "sub_info": None,
            "ui": ui,
            "is_subscription": False,
            "skip_qr": True,
        }

    title = rich_plain_text(ui.get("payment_ok_title")) or "✅ پرداخت تأیید شد"
    body = info_block(
        [
            kv_line("🧾", "پرداخت", f"#{payment.id if payment else '—'}"),
            kv_line("✅", "وضعیت", "تأیید شد"),
        ]
    )
    return {
        "title": title,
        "text": format_message(title, body),
        "send_kw": {},
        "detail_text": None,
        "markup": markup,
        "sub_url": None,
        "sub_info": None,
        "ui": ui,
        "is_subscription": False,
        "skip_qr": True,
    }


async def _buyer_reply_markup(session: AsyncSession, payment: Payment | None, order):
    """Main customer reply keyboard so pay-method menus do not stick after delivery."""
    from app.bot.menu_nav import buyer_main_reply_keyboard
    from app.db.models import BotUser

    uid = None
    if order is not None:
        uid = getattr(order, "user_id", None)
    if uid is None and payment is not None:
        uid = getattr(payment, "user_id", None)
    if uid is None:
        return None
    user = await session.get(BotUser, int(uid))
    if not user:
        return None
    try:
        markup, _ui = await buyer_main_reply_keyboard(
            session,
            user,
            order=order,
            is_reseller_bot=False,
            reseller_owner_id=None,
        )
        return markup
    except Exception:
        return None


async def _send_delivery_guides(
    bot: Bot,
    chat_id: int,
    session: AsyncSession,
    *,
    order,
    ui: dict[str, str],
    shop_rid: int | None,
) -> None:
    """Inline «آموزش اتصال» — call only after QR / WireGuard / text delivery."""
    try:
        from app.bot.handlers.guides import delivery_guides_keyboard
        from app.db.models import BotUser
        from app.services.connection_guides import get_connection_guides, guides_for_audience

        buyer_id = getattr(order, "user_id", None) if order is not None else None
        audience = "user"
        if buyer_id:
            buyer = await session.get(BotUser, int(buyer_id))
            if buyer and (getattr(buyer, "role", None) or "").lower() == "reseller":
                audience = "reseller"
        if guides_for_audience(
            await get_connection_guides(session, reseller_id=shop_rid), audience
        ):
            await bot.send_message(
                chat_id,
                "برای راه‌اندازی، آموزش اتصال را ببینید:",
                reply_markup=delivery_guides_keyboard(ui, audience=audience),
                parse_mode="HTML",
            )
    except Exception:
        logger.debug("delivery guides button failed", exc_info=True)


async def _send_wireguard_documents(
    bot: Bot,
    chat_id: int,
    *,
    sub_url: str | None,
    username: str | None = None,
) -> int:
    """Send WireGuard ``.conf`` files when the subscription has them. Best-effort."""
    if not sub_url:
        return 0
    from app.services.wireguard_delivery import fetch_wireguard_files

    try:
        files = await fetch_wireguard_files(sub_url, username=username)
    except Exception:
        logger.warning("wireguard delivery fetch failed", exc_info=True)
        return 0
    sent = 0
    for item in files:
        try:
            caption = "🔐 کانفیگ WireGuard" if sent == 0 else None
            await bot.send_document(
                chat_id,
                document=BufferedInputFile(item.content, filename=item.filename),
                caption=caption,
            )
            sent += 1
        except Exception:
            logger.warning(
                "wireguard document send failed file=%s", item.filename, exc_info=True
            )
    return sent


async def send_delivery_to_user(
    bot: Bot,
    chat_id: int,
    session: AsyncSession,
    payment: Payment | None,
    order,
) -> str:
    """
    Deliver a paid order to the buyer.

    Preferred order for single subscriptions with QR enabled:
    1) QR photo (caption includes success + plan details — no separate ready bubble)
    2) WireGuard ``.conf`` files when PasarGuard exposes them
    3) Connection-guide button

    Wholesale (qty > 1) never sends QR — links stay in the text message.
    Always attaches the main customer reply keyboard so pay-method menus do not stick.
    """
    shop_rid = getattr(order, "reseller_id", None) if order is not None else None
    ui = await get_all_settings(session, reseller_id=shop_rid)
    reply_kb = await _buyer_reply_markup(session, payment, order)
    if order and order.note and str(order.note).startswith("svc_addon:"):
        import html as html_mod

        from app.db.models import ServiceAddonPack
        from app.services.service_addons import (
            format_amount_label,
            kind_label,
            parse_addon_note,
        )

        parsed = parse_addon_note(order.note)
        pack_name = "افزونه"
        detail = ""
        if parsed:
            pack_id, _svc_id, snap_kind, snap_amount = parsed
            pack = await session.get(ServiceAddonPack, pack_id)
            if pack:
                pack_name = pack.name
            use_kind = snap_kind or (pack.kind if pack else None)
            use_amount = (
                snap_amount
                if snap_amount is not None
                else (float(pack.amount) if pack else None)
            )
            if use_kind and use_amount is not None:
                detail = f"{kind_label(use_kind)}: +{format_amount_label(use_kind, use_amount)}"
        text = format_message(
            "✅ افزونه اعمال شد",
            "\n".join(
                [
                    f"سفارش #{order.id}",
                    f"بسته: {html_mod.escape(pack_name)}",
                    html_mod.escape(detail) if detail else "",
                    "به سرویس قبلی شما اضافه شد.",
                ]
            ).strip(),
        )
        try:
            await bot.send_message(
                chat_id, text, reply_markup=reply_kb, parse_mode="HTML"
            )
        except Exception:
            try:
                await bot.send_message(chat_id, text, parse_mode="HTML")
            except Exception:
                logger.debug("addon delivery notify failed", exc_info=True)
        return text
    if order and order.note and str(order.note).startswith("reseller_app:"):
        from app.db.models import BotUser, ResellerApplicationStatus
        from app.services.formatting import format_user_label
        from app.services.resellers import (
            format_credentials_message,
            get_application,
        )

        try:
            app_id = int(str(order.note).split(":", 1)[1])
        except Exception:
            app_id = 0
        app = await get_application(session, app_id) if app_id else None
        creds = order.__dict__.get("_reseller_app_creds")
        if isinstance(creds, dict) and (
            not app or app.status == ResellerApplicationStatus.APPROVED.value
        ):
            text = format_credentials_message(creds)
            try:
                await bot.send_message(
                    chat_id, text, reply_markup=reply_kb, parse_mode="HTML"
                )
            except Exception:
                try:
                    await bot.send_message(chat_id, text, parse_mode="HTML")
                except Exception:
                    logger.debug("reseller creds delivery failed", exc_info=True)
            user = await session.get(BotUser, order.user_id)
            notify = (
                f"✅ نمایندگی فعال شد #{app_id}\n"
                f"سفارش #{order.id}\n"
                f"کاربر: {format_user_label(user)}\n"
                f"(تأیید خودکار پس از پرداخت)"
            )
            for aid in get_settings().admin_ids:
                try:
                    await bot.send_message(aid, notify, parse_mode="HTML")
                except Exception:
                    logger.debug("reseller approve admin notify failed", exc_info=True)
            return text

        text = (
            "✅ هزینه نمایندگی پرداخت شد.\n"
            "درخواست شما ثبت شد و پس از تأیید ادمین، اطلاعات ورود برایتان ارسال می‌شود."
        )
        try:
            await bot.send_message(
                chat_id, text, reply_markup=reply_kb, parse_mode="HTML"
            )
        except Exception:
            try:
                await bot.send_message(chat_id, text, parse_mode="HTML")
            except Exception:
                logger.debug("reseller pending delivery failed", exc_info=True)
        if app_id:
            user = await session.get(BotUser, order.user_id)
            notify = (
                f"🤝 درخواست نمایندگی پرداخت‌شده #{app_id}\n"
                f"سفارش #{order.id}\n"
                f"کاربر: {format_user_label(user)}"
            )
            for aid in get_settings().admin_ids:
                try:
                    await bot.send_message(
                        aid,
                        notify,
                        reply_markup=kb.reseller_app_review(app_id),
                        parse_mode="HTML",
                    )
                except Exception:
                    logger.debug("reseller pending admin notify failed", exc_info=True)
        return text

    wholesale = False
    if order and order.service_id:
        from app.services.orders import order_quantity

        wholesale = order_quantity(order) > 1

    # Peek whether QR can carry the details (single subscription only).
    sub_url_peek = None
    if order and order.service_id and not wholesale:
        svc = await session.get(UserService, order.service_id)
        if svc:
            sub_url_peek = svc.subscription_url
    qr_enabled = on(ui.get("qr_enabled", "1"))
    use_short = bool(
        order
        and order.service_id
        and sub_url_peek
        and qr_enabled
        and not wholesale
    )

    payload = await build_delivery_content(
        session, payment, order, include_details=not use_short
    )
    text = payload["text"]
    send_kw = dict(payload.get("send_kw") or {})
    detail_text = payload.get("detail_text")
    ui = payload["ui"]
    sub_url = payload["sub_url"]
    sub_info = payload.get("sub_info")
    skip_qr = bool(payload.get("skip_qr")) or wholesale
    is_subscription = bool(payload.get("is_subscription"))

    send_markup = reply_kb
    notify_ok = False
    qr_sent = False
    wg_username = None
    if isinstance(sub_info, dict):
        wg_username = sub_info.get("username")

    # QR-first path: merge the ready card into the caption; skip the duplicate text bubble.
    if use_short and sub_url and not skip_qr:
        header = _delivery_qr_header(ui, order)
        qr_sent = await send_subscription_qr_photo(
            bot,
            chat_id,
            sub_url,
            ui,
            info=sub_info,
            caption_header=header,
            reply_markup=send_markup,
        )
        if qr_sent:
            notify_ok = True
            text = header

    if not qr_sent:
        # Short path without QR must expand to full details (QR was meant to carry them).
        if use_short:
            detailed = await build_delivery_content(
                session, payment, order, include_details=True
            )
            text = detailed["text"]
            send_kw = dict(detailed.get("send_kw") or {})
            detail_text = detailed.get("detail_text")
            sub_url = detailed.get("sub_url") or sub_url
            sub_info = detailed.get("sub_info") or sub_info
            if isinstance(sub_info, dict) and not wg_username:
                wg_username = sub_info.get("username")

        msg_kwargs = {"parse_mode": "HTML", **send_kw}
        try:
            await bot.send_message(
                chat_id, text, reply_markup=send_markup, **msg_kwargs
            )
            notify_ok = True
        except Exception:
            try:
                await bot.send_message(chat_id, text, **msg_kwargs)
                notify_ok = True
            except Exception:
                logger.error(
                    "delivery notify failed order=%s payment=%s chat_id=%s",
                    getattr(order, "id", None),
                    getattr(payment, "id", None) if payment else None,
                    chat_id,
                    exc_info=True,
                )

        if detail_text:
            try:
                await bot.send_message(
                    chat_id, detail_text, reply_markup=send_markup, parse_mode="HTML"
                )
            except Exception:
                try:
                    await bot.send_message(chat_id, detail_text, parse_mode="HTML")
                except Exception:
                    logger.debug("delivery detail_text send failed", exc_info=True)

        # Non-short path may still want a trailing QR (details already in text).
        if sub_url and not skip_qr and not use_short:
            qr_sent = await send_subscription_qr_photo(
                bot, chat_id, sub_url, ui, info=sub_info
            )

    if is_subscription and sub_url and not wholesale:
        await _send_wireguard_documents(
            bot, chat_id, sub_url=sub_url, username=wg_username
        )

    if is_subscription and (notify_ok or qr_sent):
        await _send_delivery_guides(
            bot, chat_id, session, order=order, ui=ui, shop_rid=shop_rid
        )

    if not notify_ok and not qr_sent:
        logger.error(
            "delivery fully failed to reach user order=%s chat_id=%s",
            getattr(order, "id", None),
            chat_id,
        )
    return text


async def send_subscription_qr_photo(
    bot: Bot,
    chat_id: int,
    sub_url: str,
    ui: dict[str, str] | None = None,
    *,
    info: dict | None = None,
    data_limit: float | int | None = None,
    expire: Any = None,
    username: str | None = None,
    caption_header: str | None = None,
    reply_markup: Any = None,
) -> bool:
    """Send QR photo for a subscription URL with full caption. Returns True if sent."""
    if not sub_url:
        return False
    ui = ui or {}
    if ui and not on(ui.get("qr_enabled", "1")):
        return False
    try:
        from app.services.notifications import build_qr_caption

        import asyncio

        buf = await asyncio.to_thread(
            make_subscription_qr,
            sub_url,
            background=(ui.get("qr_background") if ui else None) or None,
        )
        caption, caption_kw = build_qr_caption(
            sub_url=sub_url,
            ui=ui,
            info=info,
            data_limit=data_limit,
            expire=expire,
            username=username,
        )
        caption = _compose_caption(caption_header, caption)
        photo_kw: dict[str, Any] = {"parse_mode": "HTML", **caption_kw}
        if reply_markup is not None:
            photo_kw["reply_markup"] = reply_markup
        # Entities from a custom qr_caption only cover that prefix — drop when we prepend.
        if caption_header and photo_kw.get("entities"):
            photo_kw.pop("entities", None)
            photo_kw["parse_mode"] = "HTML"
        await bot.send_photo(
            chat_id,
            photo=BufferedInputFile(buf.read(), filename="subscription_qr.png"),
            caption=caption[:1024],
            **photo_kw,
        )
        return True
    except Exception:
        logger.debug("subscription QR send failed", exc_info=True)
        return False
