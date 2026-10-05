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
from app.services.message_variables import DOMAIN_USER

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

    from app.services.rich_text import outbound_setting_text, rich_plain_text

    home_send_kw: dict = {}
    if effective_role == "admin":
        from app.bot.menu_nav import build_main_reply_keyboard

        text, home_send_kw = outbound_setting_text(
            "پنل مدیریت فروشگاه\nاز کیبورد پایین گزینه را انتخاب کنید.",
            title_raw=ui.get("shop_title") or "کلاک",
        )
        reply_kb, ui, _ = await build_main_reply_keyboard(
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
            ui=ui,
        )
    elif effective_role == "reseller" and is_reseller_bot:
        from app.services.reseller_access import load_reseller_actor

        _, profile = await load_reseller_actor(
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        text, home_send_kw = outbound_setting_text(
            "پنل مدیریت فروشگاه شما\nاز کیبورد پایین گزینه را انتخاب کنید.\n"
            "برای دیدن منوی مشتری: «پیش‌نمایش منوی کاربر».",
            title_raw=ui.get("shop_title") or "فروشگاه",
        )
        reply_kb = kb.reseller_hub_main_keyboard(profile, ui)
    else:
        show_creds = is_shop_owner_on_main_bot(db_user, is_reseller_bot=is_reseller_bot)
        has = await _has_services(session, db_user.id)
        welcome = ui.get("welcome_text", "")
        shop_title_raw = ui.get("shop_title", "")
        title_plain = rich_plain_text(shop_title_raw)
        try:
            text, home_send_kw = outbound_setting_text(
                welcome,
                title_raw=shop_title_raw or "",
                domain=DOMAIN_USER,
                user_name=db_user.full_name or "دوست عزیز",
                user_id=getattr(db_user, "telegram_id", "") or "",
                username=(f"@{db_user.username}" if getattr(db_user, "username", None) else ""),
                shop_title=title_plain or "",
            )
        except Exception:
            text = format_message(title_plain or "", rich_plain_text(welcome))
            home_send_kw = {}
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
            await message.edit_text(text, reply_markup=None, **home_send_kw)
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e).lower():
                if getattr(message, "photo", None):
                    try:
                        await message.delete()
                    except Exception:
                        pass
                    await message.answer(text, reply_markup=reply_kb, **home_send_kw)
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
            try:
                await message.answer("📱", reply_markup=mini)
            except Exception:
                pass
        return

    await message.answer(text, reply_markup=reply_kb, **home_send_kw)
    if mini:
        # Never let Mini App keyboard failure break /start (HTTPS-only WebApp).
        try:
            await message.answer("📱", reply_markup=mini)
        except Exception:
            pass
    # seed_reply_kb kept for API compat — reply kb already attached to welcome
    _ = seed_reply_kb


async def seed_main_reply_kb(message: Message, reply_kb, *, tip: str = "⌨️") -> None:
    from app.bot.tg_utils import seed_reply_keyboard

    # Welcome already carries the reply KB; ephemeral tip is polish only.
    await seed_reply_keyboard(message, reply_kb, tip=tip, ephemeral=True)


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
    ui = await get_all_settings(session)
    channels = []
    force_entries: list = []
    try:
        from app.services.users import parse_force_join_channels, parse_force_join_entries

        raw_fj = ui.get("force_join_channel")
        force_entries = parse_force_join_entries(raw_fj)
        channels = parse_force_join_channels(raw_fj)
    except Exception:
        ch = (ui.get("force_join_channel") or "").strip()
        channels = [ch] if ch else []
        force_entries = []
    enabled = ui.get("force_join_enabled")
    from app.bot.middlewares import (
        check_force_join_all,
        clear_force_join_member_cache,
        force_join_block_outbound,
    )
    from app.services.reseller_access import effective_menu_role

    role_for_force = await effective_menu_role(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if on(enabled) and channels and role_for_force == "user":
        # Always re-check on /start (drop stale negative cache)
        clear_force_join_member_cache(int(db_user.telegram_id))
        missing, unverified = await check_force_join_all(
            message.bot, int(db_user.telegram_id), channels, entries=force_entries
        )
        if missing or unverified:
            fj_text, fj_kw = force_join_block_outbound(
                missing, unverified, custom=ui.get("force_join_msg")
            )
            await message.answer(
                fj_text,
                reply_markup=kb.force_join_inline_keyboard(
                    ui.get("force_join_channel"), ui=ui, channels=channels
                ),
                **fj_kw,
            )
            return
    # Entry terms gate (after force-join, before welcome/menu)
    from app.services.terms import needs_entry_gate
    from app.bot.handlers.terms import show_terms_prompt

    entry_prompt = await needs_entry_gate(
        session,
        db_user,
        ui,
        menu_role=role_for_force,
        reseller_owner_id=reseller_owner_id,
    )
    if entry_prompt is not None:
        await show_terms_prompt(message, entry_prompt, ui)
        return
    if args.startswith("sub_"):
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
    if await _handle_start_deeplink(
        message,
        session,
        db_user,
        state,
        args,
        ui=ui,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
        effective_role=role_for_force,
    ):
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


async def _handle_start_deeplink(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    args: str,
    *,
    ui: dict,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
    effective_role: str | None = None,
) -> bool:
    """Handle UX20 deep-links. Returns True if payload was consumed."""
    raw = (args or "").strip()
    if not raw or raw.startswith("ref_") or raw.startswith("sub_"):
        return False
    key = raw.split("_", 1)[0].lower()
    rest = raw[len(key) + 1 :] if "_" in raw else ""

    if key == "wallet":
        from app.bot.handlers.reply_nav import open_wallet_home

        await render_home(
            message,
            session,
            db_user,
            seed_reply_kb=True,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
            ui=ui,
            effective_role=effective_role,
        )
        await open_wallet_home(message, session, db_user, state, push=True)
        return True

    if key == "support":
        from app.bot.handlers.reply_nav import open_support_home

        await render_home(
            message,
            session,
            db_user,
            seed_reply_kb=True,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
            ui=ui,
            effective_role=effective_role,
        )
        await open_support_home(message, session, db_user, state, push=True)
        return True

    if key == "gift":
        from app.bot.handlers.wallet import prompt_gift_code, redeem_gift_for_user

        code = rest.strip() or ""
        # Allow /start gift_CODE as well as bare gift
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
        else:
            await prompt_gift_code(message, state, session)
        return True

    if key == "renew":
        await _deeplink_renew(
            message,
            session,
            db_user,
            state,
            svc_id=int(rest) if rest.isdigit() else None,
            ui=ui,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
            effective_role=effective_role,
        )
        return True

    if key == "config":
        await _deeplink_config(
            message,
            session,
            db_user,
            state,
            svc_id=int(rest) if rest.isdigit() else None,
            ui=ui,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
            effective_role=effective_role,
        )
        return True

    return False


async def _deeplink_renew(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    *,
    svc_id: int | None,
    ui: dict,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
    effective_role: str | None = None,
) -> None:
    from app.bot.handlers.reply_nav import open_services_list
    from app.bot.handlers.services import svc_renew
    from app.bot.handlers.reply_nav import _SoftCallback

    await render_home(
        message,
        session,
        db_user,
        seed_reply_kb=True,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
        ui=ui,
        effective_role=effective_role,
    )
    if svc_id is None:
        # Pick latest service when payload is bare "renew"
        row = (
            await session.execute(
                select(UserService)
                .where(UserService.bot_user_id == db_user.id)
                .order_by(UserService.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if not row:
            await open_services_list(message, session, db_user)
            await message.answer("سرویسی برای تمدید ندارید — از فروشگاه خرید کنید.")
            return
        svc_id = int(row.id)
    svc = await session.get(UserService, int(svc_id))
    if not svc or svc.bot_user_id != db_user.id:
        await open_services_list(message, session, db_user)
        await message.answer("سرویس پیدا نشد — از لیست یکی را انتخاب کنید.")
        return
    bubble = await message.answer("🔄 تمدید سرویس…")
    cb = _SoftCallback(bubble, f"svc:renew:{svc_id}")
    try:
        await svc_renew(cb, session, db_user)
    except Exception:
        await message.answer(
            "برای تمدید از «سرویس‌های من» استفاده کنید.",
            reply_markup=kb.main_reply_keyboard(
                effective_role or db_user.role,
                has_services=True,
                ui=ui,
            ),
        )


async def _deeplink_config(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    *,
    svc_id: int | None,
    ui: dict,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
    effective_role: str | None = None,
) -> None:
    from app.bot.handlers.reply_nav import open_services_list, _SoftCallback
    from app.bot.handlers.services import svc_link

    await render_home(
        message,
        session,
        db_user,
        seed_reply_kb=True,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
        ui=ui,
        effective_role=effective_role,
    )
    if svc_id is None:
        row = (
            await session.execute(
                select(UserService)
                .where(UserService.bot_user_id == db_user.id)
                .order_by(UserService.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if not row:
            await open_services_list(message, session, db_user)
            await message.answer("سرویسی ندارید — پس از خرید، کانفیگ اینجا ارسال می‌شود.")
            return
        svc_id = int(row.id)
    svc = await session.get(UserService, int(svc_id))
    if not svc or svc.bot_user_id != db_user.id:
        await open_services_list(message, session, db_user)
        await message.answer("سرویس پیدا نشد — از لیست یکی را انتخاب کنید.")
        return
    bubble = await message.answer("📱 ارسال کانفیگ…")
    cb = _SoftCallback(bubble, f"svc:link:{svc_id}")
    try:
        await svc_link(cb, session, db_user)
    except Exception:
        await message.answer(
            "برای دریافت لینک/QR از «سرویس‌های من» استفاده کنید.",
            reply_markup=kb.service_actions_reply_keyboard(ui),
        )


@router.callback_query(F.data == "forcejoin:nolink")
async def cb_force_join_nolink(callback: CallbackQuery):
    """Private channel saved without an invite URL — cannot open a join button."""
    await callback.answer(
        "لینک دعوت کانال در تنظیمات ذخیره نشده. ادمین باید لینک t.me/+… را وارد کند.",
        show_alert=True,
    )


@router.callback_query(F.data == "forcejoin:check")
async def cb_force_join_check(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    """Re-verify required channel membership after the user taps «عضو شدم»."""
    from app.bot.middlewares import (
        check_force_join_all,
        clear_force_join_member_cache,
        force_join_block_outbound,
    )
    from app.services.reseller_access import effective_menu_role
    from app.services.users import parse_force_join_channels, parse_force_join_entries

    ui = await get_all_settings(session)
    raw_fj = ui.get("force_join_channel")
    force_entries = parse_force_join_entries(raw_fj)
    channels = parse_force_join_channels(raw_fj)
    enabled = ui.get("force_join_enabled")
    role_for_force = await effective_menu_role(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if not on(enabled) or not channels or role_for_force != "user":
        await callback.answer("ادامه دهید ✅")
        await state.clear()
        if callback.message:
            await render_home(
                callback.message,
                session,
                db_user,
                seed_reply_kb=True,
                is_reseller_bot=is_reseller_bot,
                reseller_owner_id=reseller_owner_id,
                ui=ui,
                effective_role=role_for_force,
            )
        return

    clear_force_join_member_cache(int(db_user.telegram_id))
    missing, unverified = await check_force_join_all(
        callback.bot, int(db_user.telegram_id), channels, entries=force_entries
    )
    if missing or unverified:
        text, fj_kw = force_join_block_outbound(
            missing, unverified, custom=ui.get("force_join_msg")
        )
        markup = kb.force_join_inline_keyboard(
            raw_fj, ui=ui, channels=channels
        )
        alert = (
            "عضویت تأیید نشد — ربات باید ادمین کانال باشد"
            if unverified and not missing
            else "هنوز عضو کانال‌ها نشده‌اید — چند ثانیه بعد دوباره بزنید"
        )
        await callback.answer(alert, show_alert=True)
        if callback.message:
            try:
                await callback.message.edit_text(text, reply_markup=markup, **fj_kw)
            except Exception:
                try:
                    await callback.message.answer(text, reply_markup=markup, **fj_kw)
                except Exception:
                    pass
        return

    await callback.answer("عضویت تأیید شد ✅")
    await state.clear()
    from app.services.terms import needs_entry_gate
    from app.bot.handlers.terms import show_terms_prompt

    entry_prompt = await needs_entry_gate(
        session,
        db_user,
        ui,
        menu_role=role_for_force,
        reseller_owner_id=reseller_owner_id,
    )
    if entry_prompt is not None:
        if callback.message:
            await show_terms_prompt(callback.message, entry_prompt, ui)
        return
    if callback.message:
        await render_home(
            callback.message,
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
    from app.services.rich_text import outbound_setting_text, rich_plain_text

    ui = await get_all_settings(session)
    raw = ui.get("guide_text")
    if not rich_plain_text(raw).strip():
        raw = (
            "متنی برای راهنما تنظیم نشده. از وب‌پنل → تنظیمات ربات → متن‌ها، "
            "فیلد «متن راهنما» را پر کنید."
        )
    text, send_kw = outbound_setting_text(raw, title="📘 راهنما")
    main_kb, _, _ = await build_main_reply_keyboard(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    await message.answer(text, reply_markup=main_kb, **send_kw)


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
    from app.services.rich_text import outbound_setting_text, rich_plain_text

    await callback.answer()
    ui = await get_all_settings(session)
    raw = ui.get("guide_text")
    if not rich_plain_text(raw).strip():
        raw = (
            "متنی برای راهنما تنظیم نشده. از وب‌پنل → تنظیمات ربات → متن‌ها، "
            "فیلد «متن راهنما» را پر کنید."
        )
    text, send_kw = outbound_setting_text(raw, title="📘 راهنما")
    if callback.message:
        await safe_edit_text(
            callback.message,
            text,
            reply_markup=kb.back_home(ui),
            **send_kw,
        )


@router.callback_query(F.data == "help:faq")
async def help_faq(callback: CallbackQuery, session: AsyncSession):
    from app.services.rich_text import outbound_setting_text

    await callback.answer()
    ui = await get_all_settings(session)
    text, send_kw = outbound_setting_text(
        ui.get("faq_text") or "", title="❓ سوالات متداول"
    )
    if callback.message:
        await safe_edit_text(
            callback.message,
            text,
            reply_markup=kb.back_home(ui),
            **send_kw,
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
    from app.services.pasarguard import get_pg_for_reseller

    ui = await get_all_settings(session)
    # Shop bots must resolve subscription via the shop's PG credentials —
    # never the platform owner token (avoids cross-tenant link/read).
    if is_reseller_bot and reseller_owner_id:
        try:
            pg = await get_pg_for_reseller(session, int(reseller_owner_id))
        except Exception:
            await message.answer("اتصال به پنل فروشگاه ممکن نیست.")
            await render_home(
                message,
                session,
                db_user,
                is_reseller_bot=is_reseller_bot,
                reseller_owner_id=reseller_owner_id,
            )
            return
    else:
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
