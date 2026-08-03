from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.tg_utils import safe_edit_text
from app.config import get_settings
from app.db.models import BotUser, Payment, PaymentMethod, PaymentStatus
from app.services.formatting import format_message, format_toman, kv_line
from app.services.orders import attach_receipt, create_wallet_topup
from app.services.receipts import process_receipt
from app.services.users import get_all_settings, on
from app.services.wallet import list_activity

router = Router(name="wallet")

_TOPUP_METHODS = {
    "card": ("pay_card_enabled", PaymentMethod.CARD.value),
    "gateway": ("pay_gateway_enabled", PaymentMethod.GATEWAY.value),
    "crypto": ("pay_crypto_enabled", PaymentMethod.CRYPTO.value),
}


class WalletStates(StatesGroup):
    topup_amount = State()
    choose_method = State()
    waiting_receipt = State()


@router.callback_query(F.data == "wallet:home")
async def wallet_home(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    await callback.answer()
    ui = await get_all_settings(session)
    text = format_message(
        "👛 کیف پول",
        "\n".join(
            [
                kv_line(
                    "💵",
                    "موجودی",
                    f"<b>{format_toman(db_user.wallet_balance, get_settings().currency)}</b>",
                ),
            ]
        ),
    )
    if callback.message:
        await safe_edit_text(callback.message, text, reply_markup=None)
        await callback.message.answer("کیف پول:", reply_markup=kb.wallet_reply_keyboard(ui))


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
        await safe_edit_text(
            callback.message,
            format_message("📜 تراکنش‌ها", body),
            reply_markup=None,
        )
        await callback.message.answer("کیف پول:", reply_markup=kb.wallet_reply_keyboard(ui))


@router.callback_query(F.data == "wallet:topup")
async def wallet_topup(callback: CallbackQuery, state: FSMContext, session: AsyncSession):
    ui = await get_all_settings(session)
    can_topup = any(
        on(ui.get(k))
        for k in ("pay_card_enabled", "pay_gateway_enabled", "pay_crypto_enabled")
    )
    if not can_topup:
        await callback.answer("روش شارژ فعالی تنظیم نشده", show_alert=True)
        return
    await callback.answer()
    await state.set_state(WalletStates.topup_amount)
    if callback.message:
        await callback.message.answer(
            format_message("➕ شارژ کیف پول", "مبلغ شارژ را به تومان وارد کنید:"),
            reply_markup=kb.cancel_reply(),
        )


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
    await state.set_state(WalletStates.choose_method)
    await state.update_data(topup_amount=amount)
    from app.bot import menu_nav as nav

    await nav.show_nav_keyboard(
        message,
        session,
        db_user,
        nav.NAV_TOPUP_PAY,
        text=format_message(
            "➕ شارژ کیف پول",
            f"{kv_line('💰', 'مبلغ', f'<b>{format_toman(amount, get_settings().currency)}</b>')}\n\n"
            "روش واریز را از کیبورد پایین انتخاب کنید:",
        ),
        state=state,
        push=True,
    )


async def _topup_instructions(
    session: AsyncSession,
    payment: Payment,
    method: str,
    ui: dict | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    if ui is None:
        ui = await get_all_settings(session)
    amount = format_toman(payment.amount, get_settings().currency)
    rows: list[list[InlineKeyboardButton]] = []
    if method == PaymentMethod.CARD.value:
        try:
            body = ui["card_pay_text"].format(
                amount=amount,
                card=ui.get("card_number") or "—",
                holder=ui.get("card_holder") or "—",
            )
        except Exception:
            body = (
                f"مبلغ {amount} را کارت به کارت کنید:\n"
                f"<code>{ui.get('card_number') or '—'}</code>\n{ui.get('card_holder') or ''}"
            )
        title = "💳 کارت به کارت"
    elif method == PaymentMethod.GATEWAY.value:
        name = ui.get("gateway_name") or "درگاه"
        link = (ui.get("gateway_link") or "").strip()
        if link:
            try:
                link = link.format(amount=payment.amount, order_id=0, payment_id=payment.id)
            except Exception:
                pass
        try:
            body = (ui.get("gateway_pay_text") or "").format(
                amount=amount, order_id=0, name=name
            )
        except Exception:
            body = f"مبلغ {amount} را از طریق {name} پرداخت کنید."
        if link.startswith("http://") or link.startswith("https://"):
            rows.append([InlineKeyboardButton(text=f"ورود به {name}", url=link)])
        title = f"🌐 {name}"
    else:
        address = (ui.get("crypto_address") or "").strip() or "—"
        try:
            body = (ui.get("crypto_pay_text") or "").format(
                amount=amount,
                asset=ui.get("crypto_asset") or "USDT",
                network=ui.get("crypto_network") or "—",
                address=address,
            )
        except Exception:
            body = f"{ui.get('crypto_asset') or 'USDT'}: <code>{address}</code>\nمبلغ تقریبی {amount}"
        title = "💎 رمزارز"
    body += f"\n\nسپس عکس رسید را بفرستید.\n(پرداخت #{payment.id})"
    rows.append([InlineKeyboardButton(text=ui.get("btn_back") or "بازگشت", callback_data="wallet:home")])
    return format_message(title, body), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.in_({"wtop:card", "wtop:gateway", "wtop:crypto"}), WalletStates.choose_method)
async def wtop_choose_method(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext, db_user: BotUser
):
    ui = await get_all_settings(session)
    key = (callback.data or "").split(":")[-1]
    flag, method = _TOPUP_METHODS[key]
    if not on(ui.get(flag)):
        await callback.answer("غیرفعال است", show_alert=True)
        return
    data = await state.get_data()
    amount = int(data.get("topup_amount") or 0)
    if amount < 1000:
        await callback.answer("ابتدا مبلغ شارژ را وارد کنید", show_alert=True)
        return
    payment = await create_wallet_topup(session, db_user.id, amount, method=method)
    await callback.answer()
    text, markup = await _topup_instructions(session, payment, method, ui=ui)
    await state.set_state(WalletStates.waiting_receipt)
    await state.update_data(payment_id=payment.id, topup_amount=None)
    if callback.message:
        await safe_edit_text(callback.message, text, reply_markup=markup)
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
    file_id = message.photo[-1].file_id
    await attach_receipt(session, payment, file_id)
    await state.clear()
    text = await process_receipt(
        session,
        payment,
        bot=message.bot,
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
    await attach_receipt(session, payment, message.photo[-1].file_id)
    text = await process_receipt(
        session,
        payment,
        bot=message.bot,
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
