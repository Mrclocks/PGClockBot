"""Telegram proof and explicit revocation, isolated from customer commerce handlers."""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.tg_utils import safe_edit_text
from app.services.customer_linking import (
    confirm_telegram_challenge, present_telegram_challenge, telegram_challenge_label, telegram_link_labels,
    unlink_telegram_customer,
)
from app.services.integration_policy import IntegrationError
from app.services.users import current_shop_reseller_id

router = Router(name="customer_linking")


@router.message(CommandStart(deep_link=True, magic=F.args.startswith("il_")))
async def open_challenge(message: Message, command: CommandObject, session: AsyncSession) -> None:
    if message.from_user is None or message.chat.type != "private":
        return
    try:
        challenge_id = await present_telegram_challenge(session, (command.args or "")[3:],
            sender_id=message.from_user.id, shop_id=current_shop_reseller_id())
        label = await telegram_challenge_label(session, challenge_id, sender_id=message.from_user.id)
    except IntegrationError as exc:
        await message.answer(str(exc))
        return
    await session.rollback()
    text = ("پیوند حساب تلگرام به مشتری وب‌سایت را تأیید می‌کنید؟\n"
            f"یکپارچه‌سازی: <code>{html.escape(label.integration_id)}</code>\n"
            f"مشتری: <code>{html.escape(label.customer_id)}</code>\n"
            "این پیوند سرویس‌ها، موجودی یا نقش شما را تغییر نمی‌دهد و اجازه ارسال اطلاعات اتصال نیست. "
            "پس از تأیید، تأیید جداگانه در وب‌سایت هم لازم است.")
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="تأیید پیوند حساب", callback_data=f"il:c:{challenge_id[4:]}"),
    ]]))


@router.callback_query(F.data.startswith("il:c:"))
async def confirm_challenge(callback: CallbackQuery, session: AsyncSession) -> None:
    try:
        await confirm_telegram_challenge(session, "lnk_" + callback.data[5:],
            sender_id=callback.from_user.id, shop_id=current_shop_reseller_id())
    except IntegrationError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer()
    await safe_edit_text(callback.message, "تأیید تلگرام ثبت شد. برای تکمیل پیوند، در وب‌سایت تأیید کنید.")


@router.message(Command("web_links"))
async def show_links(message: Message, session: AsyncSession) -> None:
    if message.from_user is None or message.chat.type != "private":
        return
    try:
        links = await telegram_link_labels(session, sender_id=message.from_user.id, shop_id=current_shop_reseller_id())
    except IntegrationError as exc:
        await message.answer(str(exc))
        return
    await session.rollback()
    if not links:
        await message.answer("پیوند فعالی در این فروشگاه وجود ندارد.")
        return
    text = "پیوندهای فعال وب‌سایت در این فروشگاه:\n" + "\n\n".join(
        f"یکپارچه‌سازی: <code>{html.escape(link.integration_id)}</code>\n"
        f"مشتری: <code>{html.escape(link.customer_id)}</code>" for link in links
    )
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"لغو پیوند {link.id}", callback_data=f"il:ask:{link.id}"),
    ] for link in links]))


@router.callback_query(F.data.startswith("il:ask:"))
async def ask_unlink(callback: CallbackQuery) -> None:
    suffix = callback.data[7:]
    if not suffix.isascii() or not suffix.isdecimal() or len(suffix) > 10:
        await callback.answer("درخواست معتبر نیست.", show_alert=True)
        return
    await callback.answer()
    await safe_edit_text(callback.message, "پیوند این حساب لغو شود؟ سرویس‌ها و موجودی‌ها حفظ می‌شوند.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="بله، پیوند لغو شود", callback_data=f"il:u:{suffix}"),
        ]]))


@router.callback_query(F.data.startswith("il:u:"))
async def unlink(callback: CallbackQuery, session: AsyncSession) -> None:
    suffix = callback.data[5:]
    if not suffix.isascii() or not suffix.isdecimal() or len(suffix) > 10:
        await callback.answer("درخواست معتبر نیست.", show_alert=True)
        return
    try:
        await unlink_telegram_customer(session, int(suffix), sender_id=callback.from_user.id, confirmed=True)
    except IntegrationError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer()
    await safe_edit_text(callback.message, "پیوند لغو شد؛ سرویس‌ها و موجودی‌ها حفظ شدند.")
