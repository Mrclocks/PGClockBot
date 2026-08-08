from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.tg_utils import safe_edit_text
from app.config import get_settings
from app.db.models import BotUser, UserService
from app.services.formatting import service_card
from app.services.pasarguard import extract_sub_token, get_pg
from app.services.users import get_all_settings, on
from app.services.shortcodes import render_user_message

router = Router(name="start")


async def _has_services(session: AsyncSession, user_id: int) -> bool:
    result = await session.execute(
        select(UserService.id).where(UserService.bot_user_id == user_id).limit(1)
    )
    return result.scalar_one_or_none() is not None


async def render_home(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    *,
    edit: bool = False,
    seed_reply_kb: bool = False,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
    ui: dict | None = None,
    effective_role: str | None = None,
):
    from app.services.formatting import format_message
    from app.services.reseller_access import effective_menu_role, is_shop_owner_on_main_bot

    if ui is None:
        ui = await get_all_settings(session)
    # Dedicated reseller bot: owner + bot_admin_ids → reseller panel;
    # platform admins/other resellers → shop user menu.
    # Main bot: resellers see user menu + credentials (no full panel).
    if effective_role is None:
        effective_role = await effective_menu_role(
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )

    show_creds = is_shop_owner_on_main_bot(db_user, is_reseller_bot=is_reseller_bot)
    has = False if effective_role == "admin" else await _has_services(session, db_user.id)

    if effective_role == "admin":
        text = format_message(
            f"🛠 {ui.get('shop_title', 'کلاک')}",
            "پنل مدیریت فروشگاه\nاز کیبورد پایین گزینه را انتخاب کنید.",
        )
        reply_kb = kb.main_reply_keyboard(effective_role, has_services=False, ui=ui)
    elif effective_role == "reseller" and is_reseller_bot:
        from app.services.reseller_access import load_reseller_actor

        _, profile = await load_reseller_actor(
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        text = format_message(
            f"🛠 {ui.get('shop_title', 'فروشگاه')}",
            "پنل مدیریت فروشگاه شما\nاز کیبورد پایین گزینه را انتخاب کنید.\n"
            "برای دیدن منوی مشتری: «پیش‌نمایش منوی کاربر».",
        )
        reply_kb = kb.reseller_hub_main_keyboard(profile, ui)
    else:
        welcome = ui.get("welcome_text", "")
        title = ui.get("shop_title", "")
        body = render_user_message(
            welcome,
            welcome or "سلام!",
            name=db_user.full_name or "دوست عزیز",
        )
        text = format_message(f"✨ {title}", body)
        reply_kb = kb.main_reply_keyboard(
            effective_role,
            has_services=has,
            ui=ui,
            show_reseller_creds=show_creds,
        )

    mini = None
    if not is_reseller_bot:
        mini = kb.miniapp_inline_keyboard(ui)

    if edit:
        from aiogram.exceptions import TelegramBadRequest

        try:
            # Inline «بازگشت» — update the bubble text; reply kb is re-seeded below
            await message.edit_text(text, reply_markup=None)
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e).lower():
                if getattr(message, "photo", None):
                    try:
                        await message.delete()
                    except Exception:
                        pass
                    await message.answer(text, reply_markup=reply_kb)
                    if mini:
                        await message.answer("📱", reply_markup=mini)
                    return
        except Exception:
            pass
        await seed_main_reply_kb(
            message,
            reply_kb,
            tip=ui.get("btn_menu_home") or "⌨️ منوی اصلی",
        )
        if mini:
            await message.answer("📱", reply_markup=mini)
        return

    await message.answer(text, reply_markup=reply_kb)
    if mini:
        await message.answer("📱", reply_markup=mini)
    # seed_reply_kb kept for API compat — reply kb already attached to welcome
    _ = seed_reply_kb


async def seed_main_reply_kb(message: Message, reply_kb, *, tip: str = "⌨️") -> None:
    from app.bot.tg_utils import seed_reply_keyboard

    await seed_reply_keyboard(message, reply_kb, tip=tip)


@router.message(F.text.func(kb.is_restart_text))
async def cmd_restart(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    """Home / legacy «شروع مجدد» — always restore main reply keyboard."""
    await state.clear()
    await render_home(
        message,
        session,
        db_user,
        seed_reply_kb=True,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    await state.clear()
    args = (command.args or "").strip()
    if args.startswith("ref_"):
        await message.answer(
            "کد دعوت ثبت شد ✅" if db_user.referred_by_id else "به ربات خوش آمدید."
        )
    elif args.startswith("sub_"):
        token = args[4:].strip()
        await _link_subscription(
            message,
            session,
            db_user,
            token,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        return
    ui = await get_all_settings(session)
    channels = []
    try:
        from app.services.users import parse_force_join_channels

        channels = parse_force_join_channels(ui.get("force_join_channel"))
    except Exception:
        ch = (ui.get("force_join_channel") or "").strip()
        channels = [ch] if ch else []
    enabled = ui.get("force_join_enabled")
    from app.bot.middlewares import check_force_join_all
    from app.services.reseller_access import effective_menu_role

    role_for_force = await effective_menu_role(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if on(enabled) and channels and role_for_force == "user":
        missing, _ = await check_force_join_all(
            message.bot, int(db_user.telegram_id), channels
        )
        if missing:
            listed = "\n".join(f"• {c}" for c in missing)
            await message.answer(
                "برای استفاده، ابتدا در همه کانال‌های زیر عضو شوید سپس دوباره /start بزنید:\n"
                f"{listed}",
                reply_markup=kb.persistent_reply_keyboard(),
            )
            return
    await render_home(
        message,
        session,
        db_user,
        seed_reply_kb=True,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
        ui=ui,
        effective_role=role_for_force,
    )


@router.callback_query(F.data == "menu:home")
async def cb_home(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    await callback.answer()
    await state.clear()
    if callback.message:
        await render_home(
            callback.message,
            session,
            db_user,
            edit=True,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )


@router.callback_query(F.data == "menu:as_user")
async def cb_home_as_user(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    """Admin preview of the customer menu (reply keyboard)."""
    from app.services.formatting import format_message

    await callback.answer()
    ui = await get_all_settings(session)
    has = await _has_services(session, db_user.id)
    text = format_message(
        "👁 پیش‌نمایش منوی کاربر",
        "کیبورد پایین همان منویی است که مشتری می‌بیند.",
    )
    if callback.message:
        try:
            await callback.message.edit_text(text)
        except Exception:
            pass
        await callback.message.answer(
            text,
            reply_markup=kb.main_reply_keyboard(
                db_user.role, has_services=has, ui=ui, as_user=True
            ),
        )


@router.message(Command("menu"))
async def cmd_menu(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    await state.clear()
    await render_home(
        message,
        session,
        db_user,
        seed_reply_kb=True,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )


@router.message(Command("help"))
async def cmd_help(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    """Telegram /help — guide_text from settings (no longer a keyboard button)."""
    from app.bot.menu_nav import build_main_reply_keyboard
    from app.services.formatting import format_message

    ui = await get_all_settings(session)
    body = (ui.get("guide_text") or "").strip() or (
        "متنی برای راهنما تنظیم نشده. از وب‌پنل → تنظیمات ربات → متن‌ها، فیلد «متن راهنما» را پر کنید."
    )
    main_kb, _, _ = await build_main_reply_keyboard(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    await message.answer(format_message("📘 راهنما", body), reply_markup=main_kb)


@router.message(F.text.func(kb.is_cancel_text))
async def orphan_cancel(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    """انصراف outside FSM restores main KB; inside FSM let state handlers run first."""
    from aiogram.dispatcher.event.bases import SkipHandler

    cur = await state.get_state()
    if cur:
        raise SkipHandler()
    from app.bot.menu_nav import restore_main_reply

    await restore_main_reply(
        message,
        session,
        db_user,
        text="🏠 منوی اصلی",
        state=state,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )


@router.callback_query(F.data == "help:guide")
async def help_guide(callback: CallbackQuery, session: AsyncSession):
    from app.services.formatting import format_message

    await callback.answer()
    ui = await get_all_settings(session)
    body = (ui.get("guide_text") or "").strip() or "متنی برای راهنما تنظیم نشده. از وب‌پنل → تنظیمات ربات → متن‌ها، فیلد «متن راهنما» را پر کنید."
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message("📘 راهنما", body),
            reply_markup=kb.back_home(ui),
        )


@router.callback_query(F.data == "help:faq")
async def help_faq(callback: CallbackQuery, session: AsyncSession):
    from app.services.formatting import format_message

    await callback.answer()
    ui = await get_all_settings(session)
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message("❓ سوالات متداول", ui.get("faq_text") or ""),
            reply_markup=kb.back_home(ui),
        )


async def _link_subscription(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    token: str,
    *,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    from app.services.formatting import format_message

    ui = await get_all_settings(session)
    pg = get_pg()
    try:
        info = await pg.subscription_info(token)
    except Exception:
        await message.answer("لینک نامعتبر است یا سرویس پیدا نشد.")
        await render_home(
            message,
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        return

    existing = await session.execute(
        select(UserService).where(
            UserService.bot_user_id == db_user.id,
            UserService.subscription_token == token,
        )
    )
    svc = existing.scalar_one_or_none()
    if not svc:
        claimed = await session.execute(
            select(UserService).where(
                UserService.subscription_token == token,
                UserService.bot_user_id != db_user.id,
            ).limit(1)
        )
        if claimed.scalar_one_or_none() is not None:
            await message.answer("این اشتراک قبلاً به حساب دیگری وصل شده است.")
            await render_home(
                message,
                session,
                db_user,
                is_reseller_bot=is_reseller_bot,
                reseller_owner_id=reseller_owner_id,
            )
            return
        sub_url = f"{pg.base_url.rstrip('/')}/sub/{token}"
        svc = UserService(
            bot_user_id=db_user.id,
            pg_user_id=info.get("id"),
            pg_username=info.get("username", "unknown"),
            subscription_url=sub_url,
            subscription_token=token or extract_sub_token(sub_url),
            remark="linked",
        )
        session.add(svc)
        await session.commit()

    await message.answer(
        format_message("✅ اتصال سرویس", service_card(info)),
        reply_markup=kb.service_actions_reply_keyboard(ui),
    )
