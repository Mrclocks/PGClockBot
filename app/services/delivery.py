from __future__ import annotations

"""Send clean delivery messages (+ optional subscription QR photo) to users."""

from typing import Any

from aiogram import Bot
from aiogram.types import BufferedInputFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Payment, UserService
from app.services.formatting import format_message, format_toman, info_block, kv_line, service_card, copyable
from app.bot import keyboards as kb
from app.config import get_settings
from app.services.pasarguard import get_pg
from app.services.qrcode_gen import make_subscription_qr
from app.services.users import get_all_settings, on


def _subscription_success_body(ui: dict[str, str], order) -> str:
    try:
        success = (ui.get("purchase_success_text") or "").format(order_id=order.id)
    except Exception:
        success = f"سفارش #{order.id} با موفقیت فعال شد."
    return (success or "").strip()


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
    """
    shop_rid = getattr(order, "reseller_id", None) if order is not None else None
    ui = await get_all_settings(session, reseller_id=shop_rid)
    markup = kb.back_home(ui)
    title = ui.get("delivery_title") or "✅ سرویس آماده است"
    sub_url = None
    sub_info: dict | None = None
    body_parts: list[str] = []

    if order and order.service_id:
        from app.services.orders import order_quantity

        qty = order_quantity(order)
        svc = await session.get(UserService, order.service_id)
        success = _subscription_success_body(ui, order)
        if success:
            body_parts.append(success)
        if qty > 1:
            body_parts.append(f"📦 تعداد سرویس تحویل‌شده: <b>{qty}</b>")
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
                    if s.subscription_url and on(ui.get("show_sub_link_in_text", "1")):
                        lines.append(
                            f"{i}. {copyable(uname)}\n{copyable(s.subscription_url)}"
                        )
                    else:
                        lines.append(f"{i}. {copyable(uname)}")
                body_parts.append("\n\n".join(lines))
            markup = kb.back_home(ui)
            # Wholesale: no QR (would only cover the first link)
            body = "\n\n".join(body_parts)
            return {
                "title": title,
                "text": format_message(title, body),
                "markup": markup,
                "sub_url": None,
                "sub_info": None,
                "ui": ui,
                "is_subscription": True,
                "skip_qr": True,
            }

        if svc and svc.subscription_token:
            try:
                info = await get_pg().subscription_info(svc.subscription_token)
                sub_info = info if isinstance(info, dict) else None
                if include_details:
                    body_parts.append(service_card(info))
            except Exception:
                if include_details and svc.pg_username:
                    body_parts.append(f"👤 {copyable(svc.pg_username)}")
            sub_url = svc.subscription_url
            if include_details and sub_url and on(ui.get("show_sub_link_in_text", "1")):
                body_parts.append(
                    info_block(
                        [
                            "🔗 <b>لینک اشتراک</b>",
                            copyable(sub_url),
                        ]
                    )
                )
            markup = kb.service_actions(svc.id, ui)
        body = "\n\n".join(body_parts)
        return {
            "title": title,
            "text": format_message(title, body),
            "markup": markup,
            "sub_url": sub_url,
            "sub_info": sub_info,
            "ui": ui,
            "is_subscription": True,
            "skip_qr": False,
        }

    if payment and payment.is_wallet_topup:
        title = ui.get("wallet_success_title") or "💰 شارژ کیف پول"
        body = (
            ui.get("wallet_success_text")
            or "✅ مبلغ {amount} به کیف پول شما اضافه شد."
        )
        try:
            body = body.format(
                amount=format_toman(payment.amount, get_settings().currency),
                payment_id=payment.id,
            )
        except Exception:
            body = f"✅ کیف پول شما {format_toman(payment.amount, get_settings().currency)} شارژ شد."
        return {
            "title": title,
            "text": format_message(title, body),
            "markup": markup,
            "sub_url": None,
            "sub_info": None,
            "ui": ui,
            "is_subscription": False,
            "skip_qr": True,
        }

    title = ui.get("payment_ok_title") or "✅ پرداخت تأیید شد"
    body = info_block(
        [
            kv_line("🧾", "پرداخت", f"#{payment.id if payment else '—'}"),
            kv_line("✅", "وضعیت", "تأیید شد"),
        ]
    )
    return {
        "title": title,
        "text": format_message(title, body),
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
        markup, _ui = await buyer_main_reply_keyboard(session, user, order=order)
        return markup
    except Exception:
        return None


async def send_delivery_to_user(
    bot: Bot,
    chat_id: int,
    session: AsyncSession,
    payment: Payment | None,
    order,
) -> str:
    """
    Send delivery text to user; if subscription URL exists and QR is enabled,
    also send QR as a photo. Returns the HTML text that was sent.

    Wholesale (qty > 1) never sends QR — all links are in the text message.

    Always attaches the main customer reply keyboard so the user is not left on
    payment-method menus after a successful purchase / receipt approval.
    """
    shop_rid = getattr(order, "reseller_id", None) if order is not None else None
    ui = await get_all_settings(session, reseller_id=shop_rid)
    reply_kb = await _buyer_reply_markup(session, payment, order)
    if order and order.note and str(order.note).startswith("reseller_app:"):
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
                pass
        try:
            app_id = int(str(order.note).split(":", 1)[1])
        except Exception:
            app_id = 0
        if app_id:
            from app.db.models import BotUser
            from app.services.formatting import format_user_label

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
                    pass
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
    ui = payload["ui"]
    sub_url = payload["sub_url"]
    sub_info = payload.get("sub_info")
    skip_qr = bool(payload.get("skip_qr")) or wholesale

    # Prefer main reply keyboard over legacy empty inline stubs.
    send_markup = reply_kb
    try:
        await bot.send_message(
            chat_id, text, reply_markup=send_markup, parse_mode="HTML"
        )
    except Exception:
        try:
            await bot.send_message(chat_id, text, parse_mode="HTML")
        except Exception:
            pass

    qr_sent = False
    if sub_url and not skip_qr:
        qr_sent = await send_subscription_qr_photo(
            bot, chat_id, sub_url, ui, info=sub_info
        )

    # QR disabled / missing URL / send failed → ensure details still reach the user.
    if use_short and not qr_sent:
        detailed = await build_delivery_content(
            session, payment, order, include_details=True
        )
        if detailed["text"] != text:
            try:
                await bot.send_message(chat_id, detailed["text"], parse_mode="HTML")
            except Exception:
                pass
            text = detailed["text"]

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
        caption = build_qr_caption(
            sub_url=sub_url,
            ui=ui,
            info=info,
            data_limit=data_limit,
            expire=expire,
            username=username,
        )
        await bot.send_photo(
            chat_id,
            photo=BufferedInputFile(buf.read(), filename="subscription_qr.png"),
            caption=caption[:1024],
            parse_mode="HTML",
        )
        return True
    except Exception:
        return False
