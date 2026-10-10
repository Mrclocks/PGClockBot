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

@router.callback_query(F.data.startswith("nv:w:amt:"))
async def nv_wallet_amount(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    """Preset amount or custom free-text prompt (Wave C)."""
    from app.bot import keyboards as kb
    from app.bot.handlers.wallet import WalletStates, present_topup_methods
    from app.bot.nav_inline import present_inline_only, wallet_topup_presets_keyboard
    from app.services.formatting import format_message

    await callback.answer()
    if not callback.message:
        return
    part = (callback.data or "").split(":")[-1]
    ui = await get_all_settings(session)
    if part == "custom":
        await state.set_state(WalletStates.topup_amount)
        await callback.message.answer(
            format_message("➕ شارژ کیف پول", "مبلغ شارژ را به تومان وارد کنید:"),
            reply_markup=kb.cancel_reply(ui),
        )
        return
    try:
        amount = int(part)
    except ValueError:
        await present_inline_only(
            callback.message,
            text=format_message("⚠️ خطا", "مبلغ نامعتبر است."),
            inline=wallet_topup_presets_keyboard(ui),
        )
        return
    if amount < 1000:
        await present_inline_only(
            callback.message,
            text=format_message("⚠️ خطا", "حداقل مبلغ ۱٬۰۰۰ تومان است."),
            inline=wallet_topup_presets_keyboard(ui),
        )
        return
    await present_topup_methods(
        callback.message,
        session,
        db_user,
        state,
        amount,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )

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
    from app.bot.nav_inline import safe_edit_inline, wallet_hub_keyboard
    from app.config import get_settings
    from app.services.formatting import format_message, format_toman, kv_line
    from app.services.wallet import wallet_balance_for_context

    await callback.answer()
    if not callback.message:
        return
    ui = await get_all_settings(session)
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
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    from app.bot.handlers.reply_nav import open_support_home

    await callback.answer()
    if not callback.message:
        return
    await open_support_home(
        callback.message,
        session,
        db_user,
        state,
        push=False,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )

@router.callback_query(F.data == "nv:resapply")
async def nv_reseller_apply(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    from app.bot.handlers.reply_nav import open_reseller_apply

    await callback.answer()
    if not callback.message:
        return
    await open_reseller_apply(
        callback.message,
        session,
        db_user,
        state,
        push=False,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )

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
    """Legacy card callback — same catalog-or-fallback path as ``guide:svc``."""
    from app.bot.handlers.services import svc_guide

    raw = callback.data or ""
    try:
        svc_id = int(raw.rsplit(":", 1)[-1])
    except (TypeError, ValueError):
        await callback.answer("نامعتبر", show_alert=True)
        return
    callback.data = f"guide:svc:{svc_id}"
    await svc_guide(callback, session, db_user)

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

# ── Wave D: admin / PG / reseller manage hubs ──────────────────────────────

@router.callback_query(F.data == "nv:adm:home")
async def nv_adm_home(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
):
    from app.bot.handlers.reply_nav import open_admin_home

    await callback.answer()
    if not callback.message:
        return
    await open_admin_home(
        callback.message,
        session,
        db_user,
        state,
        push=False,
        is_reseller_bot=is_reseller_bot,
    )

@router.callback_query(F.data == "nv:adm:close")
async def nv_adm_close(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
):
    """Close the live admin panel — reopen via «پنل ادمین» on the main reply KB."""
    from app.bot.handlers.reply_nav import _deny_unless_owner
    from app.bot.nav_inline import clear_nav_panel, safe_edit_inline
    from app.version import __version__ as local_version

    await callback.answer()
    if not callback.message:
        return
    if not await _deny_unless_owner(
        callback.message, session, db_user, is_reseller_bot=is_reseller_bot
    ):
        return
    body = (
        f"🛠 <b>پنل ادمین</b>\n<code>v{local_version}</code>\n\n"
        "برای بازگشت، «پنل ادمین» را از کیبورد پایین بزنید."
    )
    await safe_edit_inline(callback.message, body, reply_markup=None)
    await clear_nav_panel(state)

@router.callback_query(F.data == "nv:res:close")
async def nv_res_close(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    """Close the live reseller panel — reopen via «پنل مدیریت» on reply KB."""
    from app.bot.nav_inline import clear_nav_panel, safe_edit_inline
    from app.services.reseller_access import load_reseller_actor

    await callback.answer()
    if not callback.message:
        return
    if not is_reseller_bot:
        await safe_edit_inline(
            callback.message, "از کیبورد پایین ادامه دهید.", reply_markup=None
        )
        await clear_nav_panel(state)
        return
    owner_id, profile = await load_reseller_actor(
        session,
        db_user,
        is_reseller_bot=True,
        reseller_owner_id=reseller_owner_id,
    )
    if not owner_id or not profile:
        await safe_edit_inline(
            callback.message, "دسترسی نماینده یافت نشد.", reply_markup=None
        )
        await clear_nav_panel(state)
        return
    await safe_edit_inline(
        callback.message,
        "🤝 <b>پنل نماینده</b>\nبرای بازگشت، «پنل مدیریت» را از کیبورد پایین بزنید.",
        reply_markup=None,
    )
    await clear_nav_panel(state)

@router.callback_query(F.data == "nv:adm:ops")
async def nv_adm_ops(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
):
    from app.bot.handlers.reply_nav import open_admin_ops_hub

    await callback.answer()
    if not callback.message:
        return
    await open_admin_ops_hub(
        callback.message,
        session,
        db_user,
        state,
        push=False,
        is_reseller_bot=is_reseller_bot,
    )

@router.callback_query(F.data == "nv:adm:people")
async def nv_adm_people(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
):
    from app.bot.handlers.reply_nav import open_admin_people_hub

    await callback.answer()
    if not callback.message:
        return
    await open_admin_people_hub(
        callback.message,
        session,
        db_user,
        state,
        push=False,
        is_reseller_bot=is_reseller_bot,
    )

@router.callback_query(F.data == "nv:adm:product")
async def nv_adm_product(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
):
    from app.bot.handlers.reply_nav import open_admin_product_hub

    await callback.answer()
    if not callback.message:
        return
    await open_admin_product_hub(
        callback.message,
        session,
        db_user,
        state,
        push=False,
        is_reseller_bot=is_reseller_bot,
    )

@router.callback_query(F.data == "nv:adm:system")
async def nv_adm_system(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
):
    from app.bot.handlers.reply_nav import open_admin_system_hub

    await callback.answer()
    if not callback.message:
        return
    await open_admin_system_hub(
        callback.message,
        session,
        db_user,
        state,
        push=False,
        is_reseller_bot=is_reseller_bot,
    )

@router.callback_query(F.data == "nv:adm:pg")
async def nv_adm_pg(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    from app.bot.handlers.reply_nav import open_pg_home

    await callback.answer()
    if not callback.message:
        return
    await open_pg_home(
        callback.message,
        session,
        db_user,
        state,
        push=False,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
    )

@router.callback_query(F.data == "nv:adm:plans")
async def nv_adm_plans(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
):
    from app.bot.handlers.reply_nav import open_admin_plans_hub

    await callback.answer()
    if not callback.message:
        return
    await open_admin_plans_hub(
        callback.message,
        session,
        db_user,
        state,
        push=True,
        is_reseller_bot=is_reseller_bot,
    )

@router.callback_query(F.data == "nv:adm:users")
async def nv_adm_users(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
):
    from app.bot.handlers.reply_nav import open_admin_users_hub

    await callback.answer()
    if not callback.message:
        return
    await open_admin_users_hub(
        callback.message,
        session,
        db_user,
        state,
        push=True,
        is_reseller_bot=is_reseller_bot,
    )

@router.callback_query(F.data == "nv:adm:resellers")
async def nv_adm_resellers(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
):
    from app.bot.handlers.reply_nav import open_admin_resellers_hub

    await callback.answer()
    if not callback.message:
        return
    await open_admin_resellers_hub(
        callback.message,
        session,
        db_user,
        state,
        push=True,
        is_reseller_bot=is_reseller_bot,
    )

@router.callback_query(F.data == "nv:adm:settings")
async def nv_adm_settings(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    from app.bot.handlers.reply_nav import open_admin_settings_hub

    await callback.answer()
    if not callback.message:
        return
    await open_admin_settings_hub(
        callback.message,
        session,
        db_user,
        state,
        push=True,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
    )

@router.callback_query(F.data == "nv:adm:broadcast")
async def nv_adm_broadcast(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
):
    from app.bot.handlers.reply_nav import open_admin_broadcast_hub

    await callback.answer()
    if not callback.message:
        return
    await open_admin_broadcast_hub(
        callback.message,
        session,
        db_user,
        state,
        push=True,
        is_reseller_bot=is_reseller_bot,
    )

@router.callback_query(F.data == "nv:adm:backup")
async def nv_adm_backup(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    from app.bot.handlers.reply_nav import open_admin_backup_hub

    await callback.answer()
    if not callback.message:
        return
    await open_admin_backup_hub(
        callback.message,
        session,
        db_user,
        state,
        push=True,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
    )

@router.callback_query(F.data == "nv:adm:preview")
async def nv_adm_preview(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
):
    from app.bot.handlers.reply_nav import open_user_preview

    await callback.answer()
    if not callback.message:
        return
    await open_user_preview(callback.message, session, db_user, state)

@router.callback_query(F.data == "nv:adm:loy")
async def nv_adm_loy(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    from app.bot.handlers.reply_nav import open_admin_loyalty_hub

    await callback.answer()
    if not callback.message:
        return
    await open_admin_loyalty_hub(
        callback.message,
        session,
        db_user,
        state,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
        push=False,
    )

@router.callback_query(F.data.startswith("nv:adm:loy:"))
async def nv_adm_loy_leaf(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    """Staff loyalty manage leaves — reuse open_admin_loyalty_* helpers."""
    from app.bot.handlers import loyalty as loy_h

    part = (callback.data or "").split(":")[-1]
    await callback.answer()
    if not callback.message:
        return
    kw = dict(
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if part == "overview":
        await loy_h.open_admin_loyalty_overview(
            callback.message, session, db_user, **kw
        )
    elif part == "rules":
        await loy_h.open_admin_loyalty_rules(
            callback.message, session, db_user, **kw
        )
    elif part == "rewards":
        await loy_h.open_admin_loyalty_rewards(
            callback.message, session, db_user, **kw
        )
    elif part == "tiers":
        await loy_h.open_admin_loyalty_tiers(
            callback.message, session, db_user, **kw
        )
    elif part == "settings":
        await loy_h.open_admin_loyalty_settings(
            callback.message, session, db_user, **kw
        )
    elif part == "reftext":
        await loy_h.open_admin_loyalty_ref_text(
            callback.message, session, db_user, state, **kw
        )

@router.callback_query(F.data == "nv:res:home")
async def nv_res_home(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    from app.bot.handlers.reply_nav import open_reseller_home

    await callback.answer()
    if not callback.message:
        return
    await open_reseller_home(
        callback.message,
        session,
        db_user,
        state,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
        push=False,
    )

@router.callback_query(F.data.startswith("nv:res:ra:"))
async def nv_res_ra(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    """Dispatch reseller manage hub taps via existing reply-action path."""
    from app.bot.handlers.reply_nav import _soft_reseller, open_user_preview
    from app.bot import keyboards as kb

    action = (callback.data or "")[len("nv:res:ra:") :]
    await callback.answer()
    if not callback.message or not action:
        return
    if action in {kb.REPLY_ACTION_RES_PREVIEW, "res_preview"}:
        if not is_reseller_bot:
            await callback.message.answer(
                "پیش‌نمایش فقط روی ربات فروشگاه شما فعال است."
            )
            return
        await open_user_preview(
            callback.message,
            session,
            db_user,
            state,
            is_reseller_bot=True,
            reseller_owner_id=reseller_owner_id,
        )
        return
    await _soft_reseller(
        callback.message,
        session,
        db_user,
        action,
        state,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )

@router.callback_query(F.data.startswith("nv:adm:plans:aud:"))
async def nv_adm_plans_aud(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
):
    from app.bot.handlers.reply_nav import (
        _deny_unless_owner,
        present_admin_plans_audience,
    )

    aud = (callback.data or "").split(":")[-1]
    if aud not in {"users", "resellers"}:
        await callback.answer("نامعتبر", show_alert=True)
        return
    await callback.answer()
    if not callback.message:
        return
    # Defense in depth: helper also owner-gates; block forged callbacks early.
    if not await _deny_unless_owner(
        callback.message, session, db_user, is_reseller_bot=is_reseller_bot
    ):
        return
    await present_admin_plans_audience(
        callback.message,
        session,
        db_user,
        state,
        audience=aud,
        is_reseller_bot=is_reseller_bot,
    )

@router.callback_query(F.data.startswith("nv:adm:ra:"))
async def nv_adm_ra(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
    reseller_profile_id: int | None = None,
):
    """Wave E: dispatch admin leaf reply-actions from inline hubs."""
    from app.bot.handlers.reply_nav import (
        _deny_unless_owner,
        dispatch_admin_inline_reply_action,
    )

    action = (callback.data or "")[len("nv:adm:ra:") :]
    await callback.answer()
    if not callback.message or not action:
        return
    # Defense in depth: dispatcher also owner-gates every action.
    if not await _deny_unless_owner(
        callback.message, session, db_user, is_reseller_bot=is_reseller_bot
    ):
        return
    await dispatch_admin_inline_reply_action(
        callback.message,
        session,
        db_user,
        state,
        action,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
        reseller_profile_id=reseller_profile_id,
    )

# Silence unused helper warning for static checkers when message missing
_ = _answer_gone
