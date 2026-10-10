from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.tg_utils import safe_edit_text
from app.config import get_settings
from app.db.models import BotUser, Payment, PaymentMethod, PaymentStatus
from app.services.redact import user_safe_error
from app.services.formatting import format_message, format_toman, kv_line
from app.services.orders import create_wallet_topup
from app.services.receipts import submit_receipt
from app.services.users import get_all_settings, on
from app.services.wallet import list_activity
from app.services.message_variables import DOMAIN_PAYMENT, render_message_template

router = Router(name="wallet")

_TOPUP_METHODS = {
    "card": ("pay_card_enabled", PaymentMethod.CARD.value),
    "gateway": ("pay_gateway_enabled", PaymentMethod.GATEWAY.value),
    "psp": ("pay_psp_enabled", PaymentMethod.PSP.value),
    "crypto": ("pay_crypto_enabled", PaymentMethod.CRYPTO.value),
}

class WalletStates(StatesGroup):
    topup_amount = State()
    choose_method = State()
    waiting_receipt = State()
    gift_code = State()

async def prompt_gift_code(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    *,
    intro: str | None = None,
) -> None:
    ui = await get_all_settings(session)
    await state.set_state(WalletStates.gift_code)
    await message.answer(
        format_message(
            "🎁 کد هدیه",
            intro
            or "کد شارژ/هدیه را وارد کنید:\n(یا دستور <code>/gift کد</code>)",
        ),
        reply_markup=kb.cancel_reply(ui),
    )

async def redeem_gift_for_user(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    code: str,
    *,
    state: FSMContext | None = None,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> bool:
    """Redeem a charge code; returns True on success."""
    from app.services.ux20 import redeem_charge_code

    try:
        row, balance = await redeem_charge_code(session, user=db_user, code=code)
        await session.commit()
    except ValueError as e:
        await message.answer(format_message("⚠️ کد هدیه", user_safe_error(e)))
        return False
    except Exception as e:
        await message.answer(format_message("❌ خطا", user_safe_error(e)))
        return False
    if state is not None:
        await state.clear()
    from app.bot.menu_nav import restore_main_reply

    await restore_main_reply(
        message,
        session,
        db_user,
        text=format_message(
            "✅ شارژ شد",
            f"کد <code>{row.code}</code> اعمال شد.\n"
            f"مبلغ: <b>{format_toman(row.amount, get_settings().currency)}</b>\n"
            f"موجودی جدید: <b>{format_toman(balance, get_settings().currency)}</b>",
        ),
        state=state,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    return True

@router.message(Command("gift"))
async def cmd_gift(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    code = (command.args or "").strip()
    if code:
        await redeem_gift_for_user(
            message,
            session,
            db_user,
            code,
            state=state,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        return
    await prompt_gift_code(message, state, session)

@router.message(WalletStates.gift_code)
async def wallet_gift_code(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    from app.bot.menu_nav import restore_main_reply

    ui = await get_all_settings(session)
    if kb.is_cancel_text(message.text) or kb.is_home_text(message.text, ui):
        await restore_main_reply(
            message,
            session,
            db_user,
            text="لغو شد.",
            state=state,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        return
    code = (message.text or "").strip()
    if not code:
        await message.answer(
            "کد را وارد کنید یا انصراف بزنید.",
            reply_markup=kb.cancel_reply(ui),
        )
        return
    await redeem_gift_for_user(
        message,
        session,
        db_user,
        code,
        state=state,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )

@router.callback_query(F.data == "wallet:home")
async def wallet_home(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    await callback.answer()
    ui = await get_all_settings(session)
    from app.services.wallet import wallet_balance_for_context

    bal = await wallet_balance_for_context(session, db_user)
    text = format_message(
        "👛 کیف پول",
        "\n".join(
            [
                kv_line(
                    "💵",
                    "موجودی",
                    f"<b>{format_toman(bal, get_settings().currency)}</b>",
                ),
            ]
        ),
    )
    if callback.message:
        from app.bot.nav_inline import wallet_hub_keyboard

        await safe_edit_text(
            callback.message, text, reply_markup=wallet_hub_keyboard(ui)
        )

@router.callback_query(F.data == "wallet:tx")
async def wallet_tx(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    await callback.answer()
    ui = await get_all_settings(session)
    txs = await list_activity(session, db_user.id, limit=15)
    if not txs:
        body = "تراکنشی نیست."
    else:
        lines = []
        for t in txs:
            sign = "+" if t.amount >= 0 else ""
            lines.append(
                f"{sign}{format_toman(t.amount, get_settings().currency)} — {t.reason}"
            )
        body = "\n".join(lines)
    if callback.message:
        from app.bot.nav_inline import wallet_hub_keyboard

        await safe_edit_text(
            callback.message,
            format_message("📜 تراکنش‌ها", body),
            reply_markup=wallet_hub_keyboard(ui),
        )

@router.callback_query(F.data == "wallet:topup")
async def wallet_topup(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    ui = await get_all_settings(session)
    can_topup = any(
        on(ui.get(k))
        for k in (
            "pay_card_enabled",
            "pay_gateway_enabled",
            "pay_psp_enabled",
            "pay_crypto_enabled",
        )
    )
    if not can_topup:
        await callback.answer("روش شارژ فعالی تنظیم نشده", show_alert=True)
        return
    await callback.answer()
    if not callback.message:
        return
    from app.bot.nav_inline import present_inline_only, wallet_topup_presets_keyboard

    await state.set_state(WalletStates.topup_amount)
    await present_inline_only(
        callback.message,
        text=format_message(
            "➕ شارژ کیف پول",
            "مبلغ را انتخاب کنید یا «مبلغ دیگر» را بزنید:",
        ),
        inline=wallet_topup_presets_keyboard(ui),
    )
    return

async def present_topup_methods(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    amount: int,
    *,
    is_reseller_bot: bool,
    reseller_owner_id: int | None,
    heal_main: bool = False,
) -> None:
    """After amount is known — show pay methods on the inline panel.

    When ``heal_main`` is set (free-text amount left ``cancel_reply`` on the
    ReplyKeyboard), restore the stable main keyboard *after* the inline methods
    bubble so iOS keeps the custom menu.
    """
    ui = await get_all_settings(session)
    await state.set_state(WalletStates.choose_method)
    await state.update_data(topup_amount=amount)
    from app.bot import menu_nav as nav
    from app.bot.nav_inline import present_inline_only, topup_methods_keyboard

    body = format_message(
        "➕ شارژ کیف پول",
        f"{kv_line('💰', 'مبلغ', f'<b>{format_toman(amount, get_settings().currency)}</b>')}\n\n"
        + "روش واریز را انتخاب کنید:",
    )
    await nav.set_nav_level(state, nav.NAV_TOPUP_PAY, push=True)
    await present_inline_only(
        message, text=body, inline=topup_methods_keyboard(ui)
    )
    if heal_main:
        from app.bot.menu_nav import build_main_reply_keyboard
        from app.bot.tg_utils import attach_reply_keyboard

        main_kb, _, _ = await build_main_reply_keyboard(
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        await attach_reply_keyboard(
            message,
            main_kb,
            text="مبلغ ثبت شد — روش واریز را از دکمه‌های پیام بالا انتخاب کنید.",
        )
    return

@router.message(WalletStates.topup_amount)
async def wallet_topup_amount(message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser,
    is_reseller_bot: bool = False, reseller_owner_id: int | None = None):
    from app.bot.menu_nav import restore_main_reply

    ui = await get_all_settings(session)
    if kb.is_cancel_text(message.text) or kb.is_home_text(message.text, ui):
        await restore_main_reply(
            message,
            session,
            db_user,
            text="لغو شد.",
            state=state,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        return
    try:
        amount = int((message.text or "").replace(",", "").replace("٬", "").strip())
        if amount < 1000:
            raise ValueError
    except ValueError:
        await message.answer(
            format_message("⚠️ خطا", "مبلغ معتبر وارد کنید (حداقل ۱٬۰۰۰)."),
            reply_markup=kb.cancel_reply(ui),
        )
        return
    # Free-text path used cancel_reply — heal stable main KB after methods.
    await present_topup_methods(
        message,
        session,
        db_user,
        state,
        amount,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
        heal_main=True,
    )

async def _topup_instructions(
    session: AsyncSession,
    payment: Payment,
    method: str,
    ui: dict | None = None,
    *,
    card: dict | None = None,
    gateway: dict | None = None,
    wallet: dict | None = None,
) -> tuple[str, InlineKeyboardMarkup, dict]:
    from app.services.payment_destinations import (
        enabled_cards,
        enabled_crypto_wallets,
        enabled_gateways,
    )
    from app.services.rich_text import outbound_setting_text, rich_plain_text

    if ui is None:
        ui = await get_all_settings(session)
    amount = format_toman(payment.amount, get_settings().currency)
    rows: list[list[InlineKeyboardButton]] = []
    shop_title = rich_plain_text(ui.get("shop_title")) or ""
    append = f"\n\nسپس عکس رسید را بفرستید.\n(پرداخت #{payment.id})"
    raw = ""
    title = ""
    fallback = ""
    kwargs: dict = {
        "amount": amount,
        "shop_title": shop_title,
        "payment_id": payment.id,
    }
    if method == PaymentMethod.CARD.value:
        card = card or (enabled_cards(ui)[0] if enabled_cards(ui) else None)
        card_num = (card or {}).get("number") or ui.get("card_number") or "—"
        holder = (card or {}).get("holder") or ui.get("card_holder") or "—"
        raw = ui.get("card_pay_text") or ""
        title = "💳 کارت به کارت"
        kwargs.update(card=card_num, holder=holder)
        fallback = (
            f"مبلغ {amount} را کارت به کارت کنید:\n"
            f"<code>{card_num}</code>\n{holder}"
        )
    elif method == PaymentMethod.GATEWAY.value:
        gateway = gateway or (enabled_gateways(ui)[0] if enabled_gateways(ui) else None)
        name = (gateway or {}).get("name") or ui.get("gateway_name") or "درگاه"
        link = ((gateway or {}).get("link") or ui.get("gateway_link") or "").strip()
        if link:
            try:
                link = render_message_template(
                    link,
                    domain=DOMAIN_PAYMENT,
                    amount=payment.amount,
                    order_id=0,
                    payment_id=payment.id,
                    html=False,
                )
            except Exception:
                pass
        raw = ui.get("gateway_pay_text") or ""
        title = f"🌐 {name}"
        kwargs.update(order_id=0, gateway_name=name)
        fallback = f"مبلغ {amount} را از طریق {name} پرداخت کنید."
        if link.startswith("http://") or link.startswith("https://"):
            rows.append([InlineKeyboardButton(text=f"ورود به {name}", url=link)])
    else:
        wallet = wallet or (enabled_crypto_wallets(ui)[0] if enabled_crypto_wallets(ui) else None)
        address = ((wallet or {}).get("address") or ui.get("crypto_address") or "").strip() or "—"
        asset = (wallet or {}).get("asset") or ui.get("crypto_asset") or "USDT"
        network = (wallet or {}).get("network") or ui.get("crypto_network") or "—"
        raw = ui.get("crypto_pay_text") or ""
        title = "💎 رمزارز"
        kwargs.update(asset=asset, network=network, address=address)
        fallback = f"{asset}: <code>{address}</code>\nمبلغ تقریبی {amount}"
    try:
        text, send_kw = outbound_setting_text(
            raw,
            title=title,
            domain=DOMAIN_PAYMENT,
            append=append,
            **kwargs,
        )
    except Exception:
        text = format_message(title, fallback + append)
        send_kw = {}
    rows.append([InlineKeyboardButton(text=ui.get("btn_back") or "بازگشت", callback_data="wallet:home")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows), send_kw

@router.callback_query(
    # Picker buttons are wtop:card:0:<dest_id> (order_id placeholder 0), matching shop pay:* shape.
    F.data.regexp(r"^wtop:(card|gateway|psp|crypto)(?::\d+(?::\w+)?)?$"),
    WalletStates.choose_method,
)
async def wtop_choose_method(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext, db_user: BotUser
):
    from app.services.payment_destinations import (
        card_by_id,
        crypto_by_id,
        enabled_cards,
        enabled_crypto_wallets,
        enabled_gateways,
        gateway_by_id,
        inline_picker_markup,
    )

    ui = await get_all_settings(session)
    parts = (callback.data or "").split(":")
    key = parts[1]
    # wtop:KEY | wtop:KEY:ORDER_ID | wtop:KEY:ORDER_ID:DEST_ID
    dest_id = parts[3] if len(parts) > 3 else None
    flag, method = _TOPUP_METHODS[key]
    if not on(ui.get(flag)):
        await callback.answer("غیرفعال است", show_alert=True)
        return
    data = await state.get_data()
    amount = int(data.get("topup_amount") or 0)
    if amount < 1000:
        await callback.answer("ابتدا مبلغ شارژ را وارد کنید", show_alert=True)
        return

    if key == "psp":
        from app.services.payment_settlement import create_psp_checkout

        payment = await create_wallet_topup(session, db_user.id, amount, method=method)
        try:
            settlement = await create_psp_checkout(
                session, payment, description=f"شارژ کیف پول #{payment.id}"
            )
        except ValueError as e:
            await callback.answer(user_safe_error(e), show_alert=True)
            return
        await callback.answer()
        amount_txt = format_toman(amount, get_settings().currency)
        try:
            body = render_message_template(
                ui.get("psp_pay_text") or "",
                domain=DOMAIN_PAYMENT,
                amount=amount_txt,
                order_id=0,
                payment_id=payment.id,
                shop_title=ui.get("shop_title") or "",
            )
        except Exception:
            body = f"مبلغ {amount_txt} را از درگاه آنلاین پرداخت کنید."
        body += f"\n\n(پرداخت #{payment.id})"
        rows: list[list[InlineKeyboardButton]] = []
        link = (settlement.checkout_url or "").strip()
        if link.startswith("http://") or link.startswith("https://"):
            rows.append([InlineKeyboardButton(text="🏦 ورود به درگاه", url=link)])
        rows.append(
            [InlineKeyboardButton(text=ui.get("btn_back") or "بازگشت", callback_data="wallet:home")]
        )
        await state.clear()
        if callback.message:
            await safe_edit_text(
                callback.message,
                format_message("🏦 درگاه آنلاین", body),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )
        return

    card = gateway = wallet = None
    if key == "card":
        items = enabled_cards(ui)
        if not items:
            await callback.answer("کارت تنظیم نشده", show_alert=True)
            return
        if len(items) > 1 and not dest_id:
            await callback.answer()
            markup = inline_picker_markup("card", items, order_id=0, prefix="wtop", ui=ui)
            if callback.message and markup:
                await safe_edit_text(
                    callback.message,
                    format_message("💳 کارت", "یکی از کارت‌ها را انتخاب کنید:"),
                    reply_markup=markup,
                )
            return
        card = card_by_id(ui, dest_id) if dest_id else items[0]
        if not card:
            await callback.answer("کارت نامعتبر", show_alert=True)
            return
    elif key == "gateway":
        items = enabled_gateways(ui)
        if not items:
            await callback.answer("درگاه تنظیم نشده", show_alert=True)
            return
        if len(items) > 1 and not dest_id:
            await callback.answer()
            markup = inline_picker_markup("gateway", items, order_id=0, prefix="wtop", ui=ui)
            if callback.message and markup:
                await safe_edit_text(
                    callback.message,
                    format_message("🌐 درگاه", "یکی از درگاه‌ها را انتخاب کنید:"),
                    reply_markup=markup,
                )
            return
        gateway = gateway_by_id(ui, dest_id) if dest_id else items[0]
        if not gateway:
            await callback.answer("درگاه نامعتبر", show_alert=True)
            return
    else:
        items = enabled_crypto_wallets(ui)
        if not items:
            await callback.answer("ولت تنظیم نشده", show_alert=True)
            return
        if len(items) > 1 and not dest_id:
            await callback.answer()
            markup = inline_picker_markup("crypto", items, order_id=0, prefix="wtop", ui=ui)
            if callback.message and markup:
                await safe_edit_text(
                    callback.message,
                    format_message("💎 رمزارز", "یکی از آدرس‌ها را انتخاب کنید:"),
                    reply_markup=markup,
                )
            return
        wallet = crypto_by_id(ui, dest_id) if dest_id else items[0]
        if not wallet:
            await callback.answer("ولت نامعتبر", show_alert=True)
            return

    payment = await create_wallet_topup(session, db_user.id, amount, method=method)
    if key == "card" and on(ui.get("pay_card_auto_enabled")):
        try:
            from app.services.payment_settlement import create_card_auto_awaiting

            await create_card_auto_awaiting(session, payment)
        except ValueError as e:
            await callback.answer(user_safe_error(e), show_alert=True)
            return
    await callback.answer()
    from app.services.rich_text import rich_plain_text

    text, markup, send_kw = await _topup_instructions(
        session, payment, method, ui=ui, card=card, gateway=gateway, wallet=wallet
    )
    if key == "card" and on(ui.get("pay_card_auto_enabled")):
        hint = rich_plain_text(ui.get("card_auto_hint_text")).strip()
        if hint:
            text = text + f"\n\n{hint}"
    await state.set_state(WalletStates.waiting_receipt)
    await state.update_data(payment_id=payment.id, topup_amount=None)
    if callback.message:
        await safe_edit_text(callback.message, text, reply_markup=markup, **send_kw)
        await callback.message.answer(
            "عکس رسید را بفرستید یا انصراف بزنید:",
            reply_markup=kb.cancel_reply(ui),
        )

@router.message(WalletStates.choose_method)
async def wallet_choose_method_cancel(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    from app.bot.menu_nav import restore_main_reply

    ui = await get_all_settings(session)
    if kb.is_cancel_text(message.text) or kb.is_home_text(message.text, ui):
        await restore_main_reply(
            message,
            session,
            db_user,
            text="لغو شد.",
            state=state,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        return
    await message.answer(
        "روش واریز را از کیبورد پایین انتخاب کنید یا انصراف بزنید.",
        reply_markup=kb.cancel_reply(ui),
    )

@router.message(WalletStates.waiting_receipt, F.photo)
async def wallet_receipt_photo(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    from app.bot.menu_nav import restore_main_reply

    data = await state.get_data()
    payment = await session.get(Payment, data.get("payment_id"))
    if not payment or payment.user_id != db_user.id:
        await restore_main_reply(
            message,
            session,
            db_user,
            text=format_message("خطا", "پرداخت پیدا نشد."),
            state=state,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        return
    photo = message.photo[-1]
    await state.clear()
    text = await submit_receipt(
        session,
        payment,
        bot=message.bot,
        file_id=photo.file_id,
        file_unique_id=getattr(photo, "file_unique_id", None),
        user_tg_id=message.from_user.id if message.from_user else None,
    )
    # Always leave payment/cancel keyboards — delivery already attaches main KB on auto-approve
    if text:
        await restore_main_reply(
            message,
            session,
            db_user,
            text=text,
            state=state,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
    else:
        from app.bot.menu_nav import clear_checkout_nav

        await clear_checkout_nav(state)

@router.message(WalletStates.waiting_receipt)
async def wallet_receipt_cancel(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    from app.bot.menu_nav import restore_main_reply

    ui = await get_all_settings(session)
    if kb.is_cancel_text(message.text) or kb.is_home_text(message.text, ui):
        await restore_main_reply(
            message,
            session,
            db_user,
            text="لغو شد.",
            state=state,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        return
    await message.answer(
        "عکس رسید را بفرستید یا انصراف بزنید.",
        reply_markup=kb.cancel_reply(ui),
    )

@router.message(F.photo)
async def generic_receipt(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    """Attach photo to latest awaiting payment for this user."""
    from app.bot.menu_nav import restore_main_reply

    current = await state.get_state()
    if current:
        return
    result = await session.execute(
        select(Payment)
        .where(
            Payment.user_id == db_user.id,
            Payment.status == PaymentStatus.PENDING.value,
            Payment.receipt_file_id.is_(None),
        )
        .order_by(Payment.id.desc())
        .limit(1)
    )
    payment = result.scalar_one_or_none()
    if not payment:
        return
    photo = message.photo[-1]
    text = await submit_receipt(
        session,
        payment,
        bot=message.bot,
        file_id=photo.file_id,
        file_unique_id=getattr(photo, "file_unique_id", None),
        user_tg_id=message.from_user.id if message.from_user else None,
    )
    # Leave pay-method / cancel reply keyboards after receipt (success or pending review)
    if text:
        await restore_main_reply(
            message,
            session,
            db_user,
            text=text,
            state=state,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
            as_user=True,
        )
    else:
        from app.bot.menu_nav import clear_checkout_nav

        await clear_checkout_nav(state)
