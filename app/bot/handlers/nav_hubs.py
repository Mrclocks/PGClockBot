"""Inline hub callbacks for nav_mode=inline (prefix ``nv:``).

Does not replace reply_nav label routing — only new inline buttons.
Business logic stays in existing open_* helpers / wallet / support handlers.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser
from app.services.users import get_all_settings

router = Router(name="nav_hubs")


async def _answer_gone(callback: CallbackQuery) -> None:
    try:
        await callback.answer("این پیام دیگر معتبر نیست. از منوی پایین دوباره وارد شوید.")
    except Exception:
        pass


@router.callback_query(F.data == "nv:w:topup")
async def nv_wallet_topup(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
):
    from app.bot.handlers.reply_nav import open_wallet_topup

    await callback.answer()
    if not callback.message:
        return
    await open_wallet_topup(callback.message, session, state)


@router.callback_query(F.data == "nv:w:tx")
async def nv_wallet_tx(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser
):
    from app.bot.handlers.reply_nav import open_wallet_tx

    await callback.answer()
    if not callback.message:
        return
    await open_wallet_tx(callback.message, session, db_user)


@router.callback_query(F.data == "nv:w:home")
async def nv_wallet_home(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
):
    from app.bot.handlers.reply_nav import open_wallet_home
    from app.bot.nav_inline import safe_edit_inline, wallet_hub_keyboard
    from app.bot.nav_mode import is_inline_nav
    from app.config import get_settings
    from app.services.formatting import format_message, format_toman, kv_line
    from app.services.wallet import wallet_balance_for_context

    await callback.answer()
    if not callback.message:
        return
    ui = await get_all_settings(session)
    if not is_inline_nav(ui):
        await open_wallet_home(callback.message, session, db_user, state, push=False)
        return
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
                "",
                "یک گزینه را از دکمه‌های زیر انتخاب کنید.",
            ]
        ),
    )
    ok = await safe_edit_inline(
        callback.message, text, reply_markup=wallet_hub_keyboard(ui)
    )
    if not ok:
        await open_wallet_home(callback.message, session, db_user, state, push=False)


@router.callback_query(F.data == "nv:s:new")
async def nv_support_new(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext
):
    from app.bot.handlers.reply_nav import open_support_new

    await callback.answer()
    if not callback.message:
        return
    await open_support_new(callback.message, session, state)


@router.callback_query(F.data == "nv:s:list")
async def nv_support_list(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser
):
    from app.bot.handlers.reply_nav import open_support_list

    await callback.answer()
    if not callback.message:
        return
    await open_support_list(callback.message, session, db_user)


@router.callback_query(F.data == "nv:s:home")
async def nv_support_home(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
):
    from app.bot.handlers.reply_nav import open_support_home

    await callback.answer()
    if not callback.message:
        return
    await open_support_home(callback.message, session, db_user, state, push=False)


@router.callback_query(F.data == "nv:resapply")
async def nv_reseller_apply(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
):
    from app.bot.handlers.reply_nav import open_reseller_apply

    await callback.answer()
    if not callback.message:
        return
    await open_reseller_apply(callback.message, session, db_user, state, push=False)


@router.callback_query(F.data == "nv:loy:home")
async def nv_loyalty_home(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
):
    from app.bot.handlers.loyalty import open_loyalty_home_message

    await callback.answer()
    if not callback.message:
        return
    await open_loyalty_home_message(
        callback.message, session, db_user, state, push=False
    )


@router.callback_query(F.data == "nv:loy:ref")
async def nv_loyalty_ref(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
):
    from app.bot.handlers.loyalty import open_loyalty_referral_message

    await callback.answer()
    if not callback.message:
        return
    await open_loyalty_referral_message(
        callback.message, session, db_user, state, push=False
    )


@router.callback_query(F.data == "nv:loy:pts")
async def nv_loyalty_pts(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser
):
    from app.bot.handlers.loyalty import open_loyalty_points_message

    await callback.answer()
    if not callback.message:
        return
    await open_loyalty_points_message(callback.message, session, db_user)


@router.callback_query(F.data == "nv:loy:rew")
async def nv_loyalty_rew(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser
):
    from app.bot.handlers.loyalty import open_loyalty_rewards_message

    await callback.answer()
    if not callback.message:
        return
    await open_loyalty_rewards_message(callback.message, session, db_user)


@router.callback_query(F.data == "nv:loy:wheel")
async def nv_loyalty_wheel(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser
):
    from app.bot.handlers.loyalty import open_loyalty_wheel_message

    await callback.answer()
    if not callback.message:
        return
    await open_loyalty_wheel_message(callback.message, session, db_user)


@router.callback_query(F.data == "nv:loy:hist")
async def nv_loyalty_hist(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser
):
    from app.bot.handlers.loyalty import open_loyalty_history_message

    await callback.answer()
    if not callback.message:
        return
    await open_loyalty_history_message(callback.message, session, db_user)


@router.callback_query(F.data.startswith("nv:svc:guide:"))
async def nv_svc_guide(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    """Service-card guide: show guide_text with back to the same service card."""
    from app.bot.nav_inline import safe_edit_inline
    from app.db.models import UserService
    from app.services.rich_text import outbound_setting_text, rich_plain_text

    try:
        svc_id = int((callback.data or "").split(":")[-1])
    except (TypeError, ValueError):
        await callback.answer("نامعتبر", show_alert=True)
        return
    svc = await session.get(UserService, svc_id)
    if not svc or svc.bot_user_id != db_user.id:
        await callback.answer("یافت نشد", show_alert=True)
        return
    await callback.answer()
    if not callback.message:
        return
    ui = await get_all_settings(session)
    raw = ui.get("guide_text")
    if not rich_plain_text(raw).strip():
        raw = (
            "متنی برای راهنما تنظیم نشده. از وب‌پنل → تنظیمات ربات → متن‌ها، "
            "فیلد «متن راهنما» را پر کنید."
        )
    text, send_kw = outbound_setting_text(raw, title="📘 آموزش اتصال")
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    back = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=ui.get("btn_back") or "⬅️ بازگشت",
                    callback_data=f"svc:view:{svc_id}",
                )
            ]
        ]
    )
    ok = await safe_edit_inline(
        callback.message, text, reply_markup=back, **send_kw
    )
    if not ok:
        await callback.message.answer(text, reply_markup=back, **send_kw)


@router.callback_query(F.data == "nv:trial")
async def nv_trial(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
):
    """Welcome CTA → shop trial kind (reuses shop flow)."""
    from app.bot.handlers.shop import present_shop_kind_picker
    from app.services.users import on

    await callback.answer()
    if not callback.message:
        return
    ui = await get_all_settings(session)
    if not on(ui.get("trial_enabled")):
        await callback.message.answer("پلن تست فعلاً فعال نیست.")
        return
    await present_shop_kind_picker(
        callback.message,
        ui=ui,
        body="پلن تست را از گزینه‌های زیر انتخاب کنید.",
        fixed_on=False,
        trial_on=True,
        custom_on=False,
        wholesale_on=False,
        mode="send",
    )
    _ = (db_user, state)


# Silence unused helper warning for static checkers when message missing
_ = _answer_gone
