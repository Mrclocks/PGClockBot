"""Ask subscription buyers to share their own Telegram contact when required."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.menu_nav import build_main_reply_keyboard, restore_main_reply
from app.db.models import BotUser
from app.services import bot_feedback_texts as feedback
from app.services.purchase_contact import purchase_contact_needed, purchase_shop_key, verify_purchase_contact

router = Router(name="purchase_contact")


class PurchaseContactStates(StatesGroup):
    contact = State()


async def _ask_contact(message: Message, state: FSMContext, *, resume: str) -> None:
    await state.set_state(PurchaseContactStates.contact)
    await state.update_data(purchase_contact_resume=resume, purchase_contact_shop=purchase_shop_key())
    await message.answer(
        "📱 برای خرید اشتراک، شماره تماس خودتان را با دکمه زیر ارسال کنید.\n"
        "تأیید شماره فقط برای همین فروشگاه است؛ شماره خام ذخیره نمی‌شود.",
        reply_markup=ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="📱 ارسال شماره تماس", request_contact=True)],
            [KeyboardButton(text="❌ انصراف")],
        ], resize_keyboard=True, one_time_keyboard=True),
    )


async def prompt_purchase_contact_if_needed(
    callback: CallbackQuery, session: AsyncSession, user: BotUser, state: FSMContext, ui: dict[str, str],
) -> bool:
    if not await purchase_contact_needed(session, user.id, settings=ui):
        return False
    await callback.answer()
    if callback.message:
        await _ask_contact(callback.message, state, resume=callback.data or "shop:list")
    return True


@router.message(Command("verify_phone"))
async def verify_phone_command(message: Message, session: AsyncSession, db_user: BotUser, state: FSMContext) -> None:
    if not await purchase_contact_needed(session, db_user.id):
        await message.answer("برای خرید در این فروشگاه نیازی به تأیید مجدد شماره تماس نیست.")
        return
    await _ask_contact(message, state, resume="shop:list")


@router.message(PurchaseContactStates.contact, F.contact)
async def receive_purchase_contact(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    data = await state.get_data()
    if data.get("purchase_contact_shop") != purchase_shop_key():
        await state.set_state(None)
        await message.answer("تأیید شماره را دوباره از همین فروشگاه شروع کنید.")
        return
    contact = message.contact
    if contact is None:
        return
    try:
        await verify_purchase_contact(session, db_user, contact_user_id=contact.user_id, phone_number=contact.phone_number)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.set_state(None)
    # The contact button leaves; the person's own main menu comes back, not a shop submenu.
    main_kb, _ui, _role = await build_main_reply_keyboard(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    await message.answer("✅ شماره تماس شما برای این فروشگاه تأیید شد.", reply_markup=main_kb)
    await message.answer("خرید را ادامه دهید:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🛒 ادامه خرید", callback_data=data.get("purchase_contact_resume") or "shop:list"),
    ]]))


@router.message(PurchaseContactStates.contact)
async def purchase_contact_hint(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    if kb.is_cancel_text(message.text):
        # The contact keyboard goes; the person's own menu comes back.
        await restore_main_reply(
            message, session, db_user, text=feedback.CANCELLED, state=state,
            is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id,
        )
        return
    await message.answer("از دکمه «ارسال شماره تماس» استفاده کنید یا انصراف بزنید.")
