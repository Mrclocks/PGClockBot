"""Reply-keyboard navigation: main menu, submenus, back stack, restore."""

from __future__ import annotations

from aiogram.fsm.context import FSMContext
from aiogram.types import Message, ReplyKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.db.models import BotUser, UserService
from app.services.users import get_all_settings

# FSM data keys (do not collide with shop/wallet payload keys)
NAV_LEVEL = "_kb_nav"
NAV_STACK = "_kb_stack"
PAY_ORDER_ID = "_pay_order_id"
# Within-section step (reply Back restores this before popping the keyboard level)
SHOP_STEP = "_shop_step"  # hub | plans | custom | wholesale
LOY_STEP = "_loy_step"  # hub | referral | points | rewards | wheel | history

NAV_MAIN = "main"
NAV_SHOP = "shop"
NAV_SERVICES = "services"
NAV_WALLET = "wallet"
NAV_SUPPORT = "support"
NAV_LOYALTY = "loyalty"
NAV_ADMIN_LOYALTY = "admin_loyalty"
NAV_ADMIN = "admin"
NAV_ADMIN_OPS = "admin_ops"
NAV_ADMIN_PEOPLE = "admin_people"
NAV_ADMIN_PRODUCT = "admin_product"
NAV_ADMIN_SYSTEM = "admin_system"
NAV_ADMIN_PG = "admin_pg"
NAV_ADMIN_USERS = "admin_users"
NAV_ADMIN_RESELLERS = "admin_resellers"
NAV_ADMIN_SETTINGS = "admin_settings"
NAV_ADMIN_BACKUP = "admin_backup"
NAV_ADMIN_BROADCAST = "admin_broadcast"
NAV_ADMIN_PLANS = "admin_plans"
NAV_ADMIN_PLANS_AUDIENCE = "admin_plans_audience"
NAV_ADMIN_PLANS_KIND = "admin_plans_kind"
NAV_ADMIN_PLANS_ADD_TYPE = "admin_plans_add_type"
NAV_RESELLER = "reseller"
NAV_RESELLER_SETTINGS = "reseller_settings"
NAV_RESELLER_PLANS = "reseller_plans"
NAV_RESELLER_APPLY = "reseller_apply"
NAV_PAY = "pay"
NAV_TOPUP_PAY = "topup_pay"
NAV_USER_PREVIEW = "user_preview"
NAV_SERVICE = "service"
NAV_REVIEW = "review"

# FSM payload keys for selected entities
SERVICE_ID = "_svc_id"
REVIEW_KIND = "_rev_kind"  # order | payment | resapp
REVIEW_ID = "_rev_id"


async def user_has_services(session: AsyncSession, user_id: int) -> bool:
    result = await session.execute(
        select(UserService.id).where(UserService.bot_user_id == user_id).limit(1)
    )
    return result.scalar_one_or_none() is not None


async def _platform_admin_menu_flags(
    session: AsyncSession,
    db_user: BotUser,
    *,
    is_reseller_bot: bool = False,
) -> tuple[frozenset[str], bool]:
    """Live PG menu keys + representative-create flag for platform admin keyboards."""
    from app.bot.auth import (
        bot_migrated_pg_features,
        is_bot_owner_principal,
        platform_can_manage_representatives,
    )

    feats: frozenset[str] = frozenset()
    can_reps = False
    try:
        if await is_bot_owner_principal(
            session, db_user, is_reseller_bot=is_reseller_bot
        ):
            feats = await bot_migrated_pg_features(
                session, db_user, is_reseller_bot=is_reseller_bot
            )
            can_reps = await platform_can_manage_representatives()
    except Exception:
        feats = frozenset()
        can_reps = False
    return feats, can_reps


async def admin_hub_reply_keyboard(
    session: AsyncSession,
    db_user: BotUser,
    *,
    is_reseller_bot: bool = False,
    ui: dict | None = None,
) -> ReplyKeyboardMarkup:
    """Live admin hub keyboard — same flags as ``build_main_reply_keyboard``."""
    if ui is None:
        ui = await get_all_settings(session)
    pg_feats, can_reps = await _platform_admin_menu_flags(
        session, db_user, is_reseller_bot=is_reseller_bot
    )
    return kb.admin_reply_keyboard(
        ui, pg_features=pg_feats, can_manage_representatives=can_reps
    )


async def build_main_reply_keyboard(
    session: AsyncSession,
    db_user: BotUser,
    *,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
    as_user: bool = False,
    ui: dict | None = None,
) -> tuple[ReplyKeyboardMarkup, dict, str]:
    from app.services.reseller_access import effective_menu_role, is_shop_owner_on_main_bot, load_reseller_actor

    if ui is None:
        ui = await get_all_settings(session)
    role = await effective_menu_role(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    # Shop owner/staff on their dedicated bot → management hub (like platform admin)
    if role == "reseller" and is_reseller_bot and not as_user:
        _, profile = await load_reseller_actor(
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        can_add = False
        if profile is not None:
            from app.services.representative_unification import (
                shop_bot_can_manage_representatives,
            )

            can_add = await shop_bot_can_manage_representatives(
                session,
                db_user,
                is_reseller_bot=True,
                reseller_owner_id=reseller_owner_id,
                reseller_profile_id=int(getattr(profile, "id", 0) or 0) or None,
            )
        markup = kb.reseller_hub_main_keyboard(
            profile, ui, can_add_representative=can_add
        )
        return markup, ui, role
    has = False if (role == "admin" and not as_user) else await user_has_services(session, db_user.id)
    show_creds = is_shop_owner_on_main_bot(db_user, is_reseller_bot=is_reseller_bot)
    owner_profile = None
    if show_creds and not as_user:
        from app.services.resellers import get_reseller_profile

        owner_profile = await get_reseller_profile(session, int(db_user.id))
        if owner_profile is not None and not owner_profile.is_active:
            owner_profile = None
    pg_feats: frozenset[str] | set[str] | None = None
    can_reps = True
    if role == "admin" and not as_user:
        pg_feats, can_reps = await _platform_admin_menu_flags(
            session, db_user, is_reseller_bot=is_reseller_bot
        )
    markup = kb.main_reply_keyboard(
        role,
        has_services=has,
        ui=ui,
        as_user=as_user,
        show_reseller_creds=show_creds,
        profile=owner_profile,
        pg_features=pg_feats,
        can_manage_representatives=can_reps,
    )
    return markup, ui, role


# Keys preserved across FSM clears so reply Back still has a stack.
NAV_PRESERVE_KEYS = frozenset(
    {
        NAV_LEVEL,
        NAV_STACK,
        PAY_ORDER_ID,
        SERVICE_ID,
        REVIEW_KIND,
        REVIEW_ID,
        SHOP_STEP,
        LOY_STEP,
        "_shop_custom",
        "_shop_wholesale",
        "_adm_plans_aud",
        "_adm_plans_kind",
        "_pay_order_id",
    }
)


async def get_nav_level(state: FSMContext | None) -> str:
    if state is None:
        return NAV_MAIN
    data = await state.get_data()
    return str(data.get(NAV_LEVEL) or NAV_MAIN)


async def set_nav_level(state: FSMContext | None, level: str, *, push: bool = True) -> None:
    """Set current keyboard level; optionally push previous onto the back stack."""
    if state is None:
        return
    data = await state.get_data()
    current = str(data.get(NAV_LEVEL) or NAV_MAIN)
    stack = list(data.get(NAV_STACK) or [])
    if push and current and current != level:
        stack.append(current)
        stack = stack[-8:]
    payload: dict = {NAV_LEVEL: level, NAV_STACK: stack}
    # Entering a fresh top-level shop/loyalty hub resets within-section steps.
    if level == NAV_SHOP:
        payload[SHOP_STEP] = "hub"
    elif level == NAV_LOYALTY:
        payload[LOY_STEP] = "hub"
    await state.update_data(**payload)


async def clear_nav(state: FSMContext | None) -> None:
    if state is None:
        return
    await state.update_data(
        **{
            NAV_LEVEL: NAV_MAIN,
            NAV_STACK: [],
            PAY_ORDER_ID: None,
            SHOP_STEP: None,
            LOY_STEP: None,
        }
    )


async def clear_fsm_keep_nav(state: FSMContext | None) -> None:
    """Drop FSM state / draft fields but keep reply-keyboard navigation stack."""
    if state is None:
        return
    data = await state.get_data()
    keep = {k: data[k] for k in NAV_PRESERVE_KEYS if k in data}
    try:
        await state.clear()
    except Exception:
        pass
    try:
        await state.set_state(None)
    except Exception:
        pass
    if keep:
        await state.update_data(**keep)


async def set_shop_step(state: FSMContext | None, step: str) -> None:
    if state is None:
        return
    await state.update_data(**{SHOP_STEP: step})


async def set_loy_step(state: FSMContext | None, step: str) -> None:
    if state is None:
        return
    await state.update_data(**{LOY_STEP: step})


async def pop_nav_level(state: FSMContext | None) -> str:
    """Pop back stack and return the level to restore (defaults to main)."""
    if state is None:
        return NAV_MAIN
    data = await state.get_data()
    stack = list(data.get(NAV_STACK) or [])
    level = stack.pop() if stack else NAV_MAIN
    payload: dict = {NAV_LEVEL: level, NAV_STACK: stack}
    if level != NAV_SHOP:
        payload[SHOP_STEP] = None
    if level != NAV_LOYALTY:
        payload[LOY_STEP] = None
    await state.update_data(**payload)
    return level


async def buyer_main_reply_keyboard(
    session: AsyncSession,
    db_user: BotUser,
    *,
    order=None,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> tuple[ReplyKeyboardMarkup, dict]:
    """Customer main menu after checkout/delivery (always shows «سرویس‌های من» when owned)."""
    rid = reseller_owner_id
    shop_bot = is_reseller_bot
    if order is not None:
        ord_rid = getattr(order, "reseller_id", None)
        if ord_rid:
            rid = int(ord_rid)
            shop_bot = True
    markup, ui, _role = await build_main_reply_keyboard(
        session,
        db_user,
        is_reseller_bot=shop_bot,
        reseller_owner_id=rid,
        as_user=True,
    )
    return markup, ui


async def clear_checkout_nav(state: FSMContext | None) -> None:
    """Drop pay/topup FSM nav so reply buttons leave the payment menu."""
    if state is None:
        return
    try:
        await state.clear()
    except Exception:
        pass
    try:
        await state.update_data(**{NAV_LEVEL: NAV_MAIN, NAV_STACK: []})
    except Exception:
        pass


async def restore_main_reply(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    *,
    text: str = "🏠 منوی اصلی",
    state: FSMContext | None = None,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
    as_user: bool = False,
) -> dict:
    if state is not None:
        try:
            await state.clear()
        except Exception:
            pass
        try:
            await state.update_data(**{NAV_LEVEL: NAV_MAIN, NAV_STACK: []})
        except Exception:
            pass
    markup, ui, _role = await build_main_reply_keyboard(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
        as_user=as_user,
    )
    await message.answer(text, reply_markup=markup)
    return ui


async def show_nav_keyboard(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    level: str,
    *,
    text: str,
    state: FSMContext | None = None,
    push: bool = True,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
    profile=None,
    order_id: int | None = None,
    as_user: bool = False,
    **send_kw,
) -> dict:
    """Show the reply keyboard for ``level`` and update nav stack."""
    ui = await get_all_settings(session)
    if state is not None:
        await set_nav_level(state, level, push=push)
        if order_id is not None:
            await state.update_data(**{PAY_ORDER_ID: int(order_id)})

    if level == NAV_SHOP:
        markup = kb.shop_reply_keyboard(ui)
    elif level == NAV_SERVICES:
        # Services list uses main reply chrome; Back from a service restores this level.
        markup, ui, _ = await build_main_reply_keyboard(
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
            as_user=as_user,
            ui=ui,
        )
    elif level == NAV_RESELLER_APPLY:
        markup = kb.reseller_apply_reply_keyboard(ui)
    elif level == NAV_WALLET:
        markup = kb.wallet_reply_keyboard(ui)
    elif level == NAV_SUPPORT:
        markup = kb.support_reply_keyboard(ui)
    elif level == NAV_LOYALTY:
        markup = kb.loyalty_reply_keyboard(ui)
    elif level == NAV_ADMIN_LOYALTY:
        # Platform admin sees tiers; reseller manage hub excludes global tiers
        include_tiers = not bool(is_reseller_bot)
        markup = kb.admin_loyalty_reply_keyboard(ui, include_tiers=include_tiers)
    elif level == NAV_ADMIN:
        pg_feats, can_reps = await _platform_admin_menu_flags(
            session, db_user, is_reseller_bot=is_reseller_bot
        )
        markup = kb.admin_reply_keyboard(
            ui, pg_features=pg_feats, can_manage_representatives=can_reps
        )
    elif level == NAV_ADMIN_OPS:
        markup = kb.admin_ops_reply_keyboard(ui)
    elif level == NAV_ADMIN_PEOPLE:
        _, can_reps = await _platform_admin_menu_flags(
            session, db_user, is_reseller_bot=is_reseller_bot
        )
        markup = kb.admin_people_reply_keyboard(
            ui, can_manage_representatives=can_reps
        )
    elif level == NAV_ADMIN_PRODUCT:
        pg_feats, _ = await _platform_admin_menu_flags(
            session, db_user, is_reseller_bot=is_reseller_bot
        )
        markup = kb.admin_product_reply_keyboard(ui, pg_features=pg_feats)
    elif level == NAV_ADMIN_SYSTEM:
        markup = kb.admin_system_reply_keyboard(ui)
    elif level == NAV_ADMIN_PG:
        feats: frozenset[str] = frozenset()
        can_create = False
        try:
            from app.bot.auth import bot_migrated_pg_features, bot_pg_can_create_user

            feats = await bot_migrated_pg_features(
                session, db_user, is_reseller_bot=is_reseller_bot
            )
            can_create = await bot_pg_can_create_user(
                session, db_user, is_reseller_bot=is_reseller_bot
            )
        except Exception:
            feats = frozenset()
            can_create = False
        if not feats:
            markup = kb.admin_product_reply_keyboard(ui, pg_features=feats)
        else:
            markup = kb.pg_reply_keyboard(ui, features=feats, can_create_user=can_create)
    elif level == NAV_ADMIN_USERS:
        markup = kb.admin_users_reply_keyboard(ui)
    elif level == NAV_ADMIN_RESELLERS:
        markup = kb.admin_resellers_reply_keyboard(ui)
    elif level == NAV_ADMIN_SETTINGS:
        markup = kb.admin_settings_reply_keyboard(ui)
    elif level == NAV_ADMIN_BACKUP:
        markup = kb.admin_backup_reply_keyboard(ui)
    elif level == NAV_ADMIN_BROADCAST:
        markup = kb.admin_broadcast_reply_keyboard(ui)
    elif level == NAV_ADMIN_PLANS_AUDIENCE:
        markup = kb.admin_plans_audience_reply_keyboard(ui)
    elif level == NAV_ADMIN_PLANS_KIND:
        data = await state.get_data() if state is not None else {}
        aud = str(data.get("_adm_plans_aud") or "users")
        markup = kb.admin_plans_kind_reply_keyboard(aud, ui)
    elif level == NAV_ADMIN_PLANS_ADD_TYPE:
        data = await state.get_data() if state is not None else {}
        aud = str(data.get("_adm_plans_aud") or "users")
        markup = kb.admin_plans_add_type_reply_keyboard(aud, ui)
    elif level == NAV_ADMIN_PLANS:
        markup = kb.admin_plans_audience_reply_keyboard(ui)
    elif level == NAV_RESELLER:
        markup = kb.reseller_reply_keyboard(profile, ui)
    elif level == NAV_RESELLER_SETTINGS:
        markup = kb.reseller_settings_reply_keyboard(ui)
    elif level == NAV_RESELLER_PLANS:
        markup = kb.reseller_plans_reply_keyboard(ui)
    elif level == NAV_SERVICE:
        markup = kb.service_actions_reply_keyboard(ui)
    elif level == NAV_REVIEW:
        markup = kb.review_reply_keyboard(ui)
    elif level == NAV_PAY:
        oid = order_id
        if oid is None and state is not None:
            data = await state.get_data()
            oid = data.get(PAY_ORDER_ID)
        markup = kb.pay_reply_keyboard(int(oid or 0), ui)
    elif level == NAV_TOPUP_PAY:
        markup = kb.topup_pay_reply_keyboard(ui)
    elif level == NAV_USER_PREVIEW:
        # Always customer keyboard — never leak reseller/admin hub buttons
        from app.db.models import Role as _Role

        has = await user_has_services(session, db_user.id)
        markup = kb.main_reply_keyboard(
            _Role.USER.value,
            has_services=has,
            ui=ui,
            as_user=True,
        )
    else:
        markup, ui, _ = await build_main_reply_keyboard(
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
            as_user=as_user,
            ui=ui,
        )
    await message.answer(text, reply_markup=markup, **send_kw)
    return ui


async def present_order_pay(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    order_id: int,
    *,
    state: FSMContext | None = None,
    text: str = "روش پرداخت را از کیبورد پایین انتخاب کنید:",
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    """Show order payment methods on the reply keyboard (not inline)."""
    await show_nav_keyboard(
        message,
        session,
        db_user,
        NAV_PAY,
        text=text,
        state=state,
        push=True,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
        order_id=order_id,
    )
