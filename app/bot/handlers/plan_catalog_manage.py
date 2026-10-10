"""Bot manage parity for PlanCategory + ServiceAddonPack (web /plans modals).

Shop-scoped via ``plan_categories`` / ``service_addons`` owned helpers —
platform Owner writes ``owner_reseller_id IS NULL``; reseller shop bots write
only their catalog. Reply-keyboard entry points live under plans hubs.
"""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.auth import resolve_bot_principal_bridge
from app.bot.tg_utils import parse_bot_float, parse_bot_int, safe_edit_text
from app.db.models import BotUser
from app.services.redact import user_safe_error
from app.services.shop_scope import ShopScopeError, is_platform_admin as staff_is_platform_admin

router = Router(name="plan_catalog_manage")


class PlanCatalogStates(StatesGroup):
    cat_name = State()
    cat_audience = State()
    cat_edit_name = State()
    addon_name = State()
    addon_kind = State()
    addon_amount = State()
    addon_price = State()
    addon_edit_field = State()


async def resolve_catalog_staff(
    session: AsyncSession,
    db_user: BotUser,
    *,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> dict | None:
    """Staff dict for catalog writes — fail closed when scope/perm missing."""
    if is_reseller_bot:
        from app.services.reseller_access import load_reseller_actor
        from app.services.resellers import has_bot_perm

        owner_id, profile = await load_reseller_actor(
            session,
            db_user,
            is_reseller_bot=True,
            reseller_owner_id=reseller_owner_id,
        )
        if not owner_id or not profile or not has_bot_perm(profile, "plans"):
            return None
        bridge = await resolve_bot_principal_bridge(
            session,
            db_user,
            is_reseller_bot=True,
            reseller_owner_id=reseller_owner_id,
        )
        if bridge and bridge.staff:
            return bridge.staff
        # Fail closed fallback: shop-scoped staff without Principal bridge.
        return {
            "role": "reseller",
            "bot_user_id": int(owner_id),
        }

    bridge = await resolve_bot_principal_bridge(
        session, db_user, is_reseller_bot=False
    )
    if not bridge or not bridge.staff or not staff_is_platform_admin(bridge.staff):
        return None
    return bridge.staff


async def _hub_reply_kb(
    session: AsyncSession,
    state: FSMContext,
    *,
    is_reseller_bot: bool,
) -> object:
    ui = None
    from app.services.users import get_all_settings

    data = await state.get_data()
    rid = data.get("_pcm_reseller_id")
    ui = await get_all_settings(
        session, reseller_id=int(rid) if rid else None
    )
    if is_reseller_bot or data.get("_pcm_surface") == "reseller":
        return kb.reseller_plans_reply_keyboard(ui)
    aud = data.get("_adm_plans_aud")
    if aud in {"users", "resellers"}:
        return kb.admin_plans_kind_reply_keyboard(aud, ui)
    return kb.admin_plans_audience_reply_keyboard(ui)


async def _remember_surface(
    state: FSMContext,
    *,
    is_reseller_bot: bool,
    reseller_owner_id: int | None,
) -> None:
    await state.update_data(
        _pcm_surface="reseller" if is_reseller_bot else "admin",
        _pcm_reseller_id=int(reseller_owner_id) if reseller_owner_id else None,
    )


def _cat_list_markup(cats) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for c in cats[:25]:
        mark = "✅" if c.is_active else "⏸"
        aud = str(getattr(c, "audience", None) or "users").strip().lower()
        aud_mark = "🤝" if aud == "resellers" else "👤"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark}{aud_mark} {c.name}"[:48],
                    callback_data=f"pcm:cat:tog:{c.id}",
                ),
                InlineKeyboardButton(
                    text="✏️",
                    callback_data=f"pcm:cat:edit:{c.id}",
                ),
                InlineKeyboardButton(
                    text="🗑",
                    callback_data=f"pcm:cat:del:{c.id}",
                ),
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="➕ دسته جدید", callback_data="pcm:cat:add")]
    )
    rows.append(
        [InlineKeyboardButton(text="🔄 تازه‌سازی", callback_data="pcm:cat:list")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _addon_list_markup(packs) -> InlineKeyboardMarkup:
    from app.services.service_addons import amount_label, kind_label

    rows: list[list[InlineKeyboardButton]] = []
    for p in packs[:25]:
        mark = "✅" if p.is_active else "⏸"
        label = f"{mark} {p.name} · {kind_label(p.kind)} {amount_label(p)}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label[:48],
                    callback_data=f"pcm:addon:tog:{p.id}",
                ),
                InlineKeyboardButton(
                    text="✏️",
                    callback_data=f"pcm:addon:edit:{p.id}",
                ),
                InlineKeyboardButton(
                    text="🗑",
                    callback_data=f"pcm:addon:del:{p.id}",
                ),
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="➕ بسته جدید", callback_data="pcm:addon:add")]
    )
    rows.append(
        [InlineKeyboardButton(text="🔄 تازه‌سازی", callback_data="pcm:addon:list")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def open_categories_manage(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    *,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    from app.services.plan_categories import list_categories

    staff = await resolve_catalog_staff(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if not staff:
        await message.answer("دسترسی مدیریت دسته ندارید.")
        return
    await _remember_surface(
        state, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    await state.set_state(None)
    cats = await list_categories(session, staff, active_only=False)
    body = (
        "هنوز برچسب دسته‌ای نیست."
        if not cats
        else f"تعداد: {len(cats)} — روشن/خاموش · ویرایش نام · حذف"
    )
    markup = await _hub_reply_kb(
        session, state, is_reseller_bot=is_reseller_bot
    )
    await message.answer(
        "🏷 <b>برچسب دسته</b>\n"
        "روی پلن ثابت در فروشگاه نمایش داده می‌شود — نوع پلن جدا می‌ماند.\n\n"
        + body,
        reply_markup=markup,
    )
    await message.answer("دسته‌ها:", reply_markup=_cat_list_markup(cats))


async def open_addons_manage(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    *,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    from app.services.service_addons import amount_label, kind_label, list_packs

    staff = await resolve_catalog_staff(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if not staff:
        await message.answer("دسترسی مدیریت بسته ندارید.")
        return
    await _remember_surface(
        state, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    await state.set_state(None)
    packs = await list_packs(session, staff, active_only=False)
    if not packs:
        body = "هنوز بسته‌ای نیست."
    else:
        lines = [
            f"• {html.escape(p.name)} — {kind_label(p.kind)} {amount_label(p)}"
            f" — {int(p.price):,} ت"
            for p in packs[:20]
        ]
        body = "\n".join(lines)
    markup = await _hub_reply_kb(
        session, state, is_reseller_bot=is_reseller_bot
    )
    await message.answer(
        "⏱ <b>بسته حجم / زمان</b>\n"
        "مشتری بعد از خرید سرویس می‌تواند به همان سرویس اضافه کند.\n\n"
        + body,
        reply_markup=markup,
    )
    await message.answer("بسته‌ها:", reply_markup=_addon_list_markup(packs))


async def _ctx_from_callback(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    *,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> dict | None:
    await _remember_surface(
        state, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    return await resolve_catalog_staff(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )


@router.callback_query(F.data == "pcm:cat:list")
async def pcm_cat_list(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    from app.services.plan_categories import list_categories

    staff = await _ctx_from_callback(
        callback,
        session,
        db_user,
        state,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if not staff:
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    await state.set_state(None)
    cats = await list_categories(session, staff, active_only=False)
    await callback.answer()
    if callback.message:
        await safe_edit_text(
            callback.message,
            "🏷 <b>برچسب دسته</b>\nروشن/خاموش · ویرایش · حذف",
            reply_markup=_cat_list_markup(cats),
        )


@router.callback_query(F.data == "pcm:cat:add")
async def pcm_cat_add(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    staff = await _ctx_from_callback(
        callback,
        session,
        db_user,
        state,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if not staff:
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    await callback.answer()
    from app.bot.nav_input import ask_text

    await ask_text(
        callback,
        state,
        prompt="نام برچسب دسته را بفرستید:",
        cancel_code="pcm_hub",
        fsm_state=PlanCatalogStates.cat_name,
        edit=True,
    )


@router.message(PlanCatalogStates.cat_name)
async def pcm_cat_name_save(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    from app.services.plan_categories import create_category, list_categories

    if kb.is_cancel_text(message.text):
        from app.bot.nav_input import try_legacy_cancel

        if await try_legacy_cancel(
            message,
            state,
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        ):
            return
        await state.set_state(None)
        await message.answer(
            "لغو شد.",
            reply_markup=await _hub_reply_kb(
                session, state, is_reseller_bot=is_reseller_bot
            ),
        )
        return
    staff = await resolve_catalog_staff(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if not staff:
        await state.clear()
        await message.answer("دسترسی ندارید.")
        return
    name = (message.text or "").strip()
    if not name:
        await message.answer("نام خالی است.")
        return
    # Platform Owner can target users or resellers; shop bots always users.
    if staff_is_platform_admin(staff):
        await state.update_data(_pcm_cat_name=name[:128])
        await state.set_state(PlanCatalogStates.cat_audience)
        await message.answer(
            "مخاطب این برچسب؟",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="👤 کاربران", callback_data="pcm:cat:aud:users"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="🤝 نمایندگان",
                            callback_data="pcm:cat:aud:resellers",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="❌ انصراف", callback_data="pcm:cat:list"
                        )
                    ],
                ]
            ),
        )
        return
    try:
        cat = await create_category(
            session, staff, name=name, audience="users"
        )
    except (ShopScopeError, ValueError) as e:
        await message.answer(user_safe_error(e))
        return
    except Exception as e:
        await message.answer(f"خطا: {user_safe_error(e)}")
        return
    await state.set_state(None)
    cats = await list_categories(session, staff, active_only=False)
    await message.answer(
        f"✅ دسته «{html.escape(cat.name)}» ذخیره شد.",
        reply_markup=await _hub_reply_kb(
            session, state, is_reseller_bot=is_reseller_bot
        ),
    )
    await message.answer("دسته‌ها:", reply_markup=_cat_list_markup(cats))


@router.callback_query(
    F.data.startswith("pcm:cat:aud:"), PlanCatalogStates.cat_audience
)
async def pcm_cat_audience_save(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    from app.services.plan_categories import create_category, list_categories

    staff = await _ctx_from_callback(
        callback,
        session,
        db_user,
        state,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if not staff:
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    aud = parts[-1] if parts else "users"
    if aud not in {"users", "resellers"}:
        aud = "users"
    data = await state.get_data()
    name = str(data.get("_pcm_cat_name") or "").strip()
    if not name:
        await callback.answer("نام یافت نشد", show_alert=True)
        await state.set_state(None)
        return
    try:
        cat = await create_category(
            session, staff, name=name, audience=aud
        )
    except (ShopScopeError, ValueError) as e:
        await callback.answer(user_safe_error(e), show_alert=True)
        return
    except Exception as e:
        await callback.answer(user_safe_error(e), show_alert=True)
        return
    await state.set_state(None)
    cats = await list_categories(session, staff, active_only=False)
    await callback.answer()
    if callback.message:
        await safe_edit_text(
            callback.message,
            f"✅ دسته «{html.escape(cat.name)}» ذخیره شد.",
            reply_markup=_cat_list_markup(cats),
        )


@router.callback_query(F.data.startswith("pcm:cat:tog:"))
async def pcm_cat_toggle(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    from app.services.plan_categories import get_owned_category, list_categories

    staff = await _ctx_from_callback(
        callback,
        session,
        db_user,
        state,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if not staff:
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    try:
        cid = int(callback.data.rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer("نامعتبر", show_alert=True)
        return
    cat = await get_owned_category(session, cid, staff)
    if not cat:
        await callback.answer("یافت نشد", show_alert=True)
        return
    cat.is_active = not cat.is_active
    await session.commit()
    cats = await list_categories(session, staff, active_only=False)
    await callback.answer("ذخیره شد")
    if callback.message:
        await safe_edit_text(
            callback.message,
            "🏷 <b>برچسب دسته</b>\nروشن/خاموش · ویرایش · حذف",
            reply_markup=_cat_list_markup(cats),
        )


@router.callback_query(F.data.startswith("pcm:cat:del:"))
async def pcm_cat_delete(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    from app.services.plan_categories import delete_category, list_categories

    staff = await _ctx_from_callback(
        callback,
        session,
        db_user,
        state,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if not staff:
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    try:
        cid = int(callback.data.rsplit(":", 1)[-1])
        await delete_category(session, staff, cid)
    except (ShopScopeError, ValueError) as e:
        await callback.answer(user_safe_error(e, limit=160), show_alert=True)
        return
    except Exception as e:
        await callback.answer(user_safe_error(e, limit=160), show_alert=True)
        return
    cats = await list_categories(session, staff, active_only=False)
    await callback.answer("حذف شد")
    if callback.message:
        await safe_edit_text(
            callback.message,
            "🏷 <b>برچسب دسته</b>\nروشن/خاموش · ویرایش · حذف",
            reply_markup=_cat_list_markup(cats),
        )


@router.callback_query(F.data.startswith("pcm:cat:edit:"))
async def pcm_cat_edit_ask(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    from app.services.plan_categories import get_owned_category

    staff = await _ctx_from_callback(
        callback,
        session,
        db_user,
        state,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if not staff:
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    try:
        cid = int(callback.data.rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer("نامعتبر", show_alert=True)
        return
    cat = await get_owned_category(session, cid, staff)
    if not cat:
        await callback.answer("یافت نشد", show_alert=True)
        return
    await state.update_data(_pcm_edit_cat_id=cid)
    await callback.answer()
    from app.bot.nav_input import ask_text

    await ask_text(
        callback,
        state,
        prompt=f"نام جدید برای «{html.escape(cat.name)}»:",
        cancel_code="pcm_hub",
        fsm_state=PlanCatalogStates.cat_edit_name,
        edit=True,
    )


@router.message(PlanCatalogStates.cat_edit_name)
async def pcm_cat_edit_save(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    from app.services.plan_categories import list_categories, update_category

    if kb.is_cancel_text(message.text):
        from app.bot.nav_input import try_legacy_cancel

        if await try_legacy_cancel(
            message,
            state,
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        ):
            return
        await state.set_state(None)
        await message.answer(
            "لغو شد.",
            reply_markup=await _hub_reply_kb(
                session, state, is_reseller_bot=is_reseller_bot
            ),
        )
        return
    staff = await resolve_catalog_staff(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    data = await state.get_data()
    cid = int(data.get("_pcm_edit_cat_id") or 0)
    if not staff or not cid:
        await state.clear()
        await message.answer("دسترسی ندارید.")
        return
    try:
        await update_category(
            session, staff, cid, name=(message.text or "").strip()
        )
    except (ShopScopeError, ValueError) as e:
        await message.answer(user_safe_error(e))
        return
    except Exception as e:
        await message.answer(f"خطا: {user_safe_error(e)}")
        return
    await state.set_state(None)
    cats = await list_categories(session, staff, active_only=False)
    await message.answer(
        "ذخیره شد ✅",
        reply_markup=await _hub_reply_kb(
            session, state, is_reseller_bot=is_reseller_bot
        ),
    )
    await message.answer("دسته‌ها:", reply_markup=_cat_list_markup(cats))


@router.callback_query(F.data == "pcm:addon:list")
async def pcm_addon_list(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    from app.services.service_addons import list_packs

    staff = await _ctx_from_callback(
        callback,
        session,
        db_user,
        state,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if not staff:
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    await state.set_state(None)
    packs = await list_packs(session, staff, active_only=False)
    await callback.answer()
    if callback.message:
        await safe_edit_text(
            callback.message,
            "⏱ <b>بسته حجم / زمان</b>\nروشن/خاموش · ویرایش · حذف",
            reply_markup=_addon_list_markup(packs),
        )


@router.callback_query(F.data == "pcm:addon:add")
async def pcm_addon_add(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    staff = await _ctx_from_callback(
        callback,
        session,
        db_user,
        state,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if not staff:
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    await callback.answer()
    from app.bot.nav_input import ask_text

    await ask_text(
        callback,
        state,
        prompt="نام بسته را بفرستید:",
        cancel_code="pcm_hub",
        fsm_state=PlanCatalogStates.addon_name,
        edit=True,
    )


@router.message(PlanCatalogStates.addon_name)
async def pcm_addon_name(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    is_reseller_bot: bool = False,
):
    if kb.is_cancel_text(message.text):
        from app.bot.nav_input import try_legacy_cancel

        if await try_legacy_cancel(
            message,
            state,
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        ):
            return
        await state.set_state(None)
        await message.answer(
            "لغو شد.",
            reply_markup=await _hub_reply_kb(
                session, state, is_reseller_bot=is_reseller_bot
            ),
        )
        return
    name = (message.text or "").strip()
    if not name:
        await message.answer("نام خالی نیست.")
        return
    await state.update_data(_pcm_addon_name=name[:128])
    await state.set_state(PlanCatalogStates.addon_kind)
    await message.answer(
        "نوع بسته را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📦 حجم (گیگ)", callback_data="pcm:addon:kind:volume"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⏱ زمان (روز)", callback_data="pcm:addon:kind:duration"
                    )
                ],
                [InlineKeyboardButton(text="❌ انصراف", callback_data="pcm:addon:list")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("pcm:addon:kind:"), PlanCatalogStates.addon_kind)
async def pcm_addon_kind(
    callback: CallbackQuery,
    state: FSMContext,
):
    kind = callback.data.rsplit(":", 1)[-1]
    if kind not in {"volume", "duration"}:
        await callback.answer("نامعتبر", show_alert=True)
        return
    await state.update_data(_pcm_addon_kind=kind)
    await state.set_state(PlanCatalogStates.addon_amount)
    await callback.answer()
    ask = "مقدار به گیگ را بفرستید:" if kind == "volume" else "تعداد روز را بفرستید:"
    from app.bot.nav_input import ask_text

    await ask_text(
        callback,
        state,
        prompt=ask,
        cancel_code="pcm_hub",
        fsm_state=PlanCatalogStates.addon_amount,
        edit=True,
    )


@router.message(PlanCatalogStates.addon_amount)
async def pcm_addon_amount(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    is_reseller_bot: bool = False,
):
    if kb.is_cancel_text(message.text):
        from app.bot.nav_input import try_legacy_cancel

        if await try_legacy_cancel(
            message,
            state,
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        ):
            return
        await state.set_state(None)
        await message.answer(
            "لغو شد.",
            reply_markup=await _hub_reply_kb(
                session, state, is_reseller_bot=is_reseller_bot
            ),
        )
        return
    data = await state.get_data()
    kind = data.get("_pcm_addon_kind") or "volume"
    try:
        if kind == "duration":
            amount = float(max(1, parse_bot_int(message.text)))
        else:
            amount = parse_bot_float(message.text)
            if amount <= 0:
                raise ValueError("amount")
    except ValueError:
        await message.answer("عدد معتبر بفرستید.")
        return
    await state.update_data(_pcm_addon_amount=amount)
    from app.bot.nav_input import ask_text

    await ask_text(
        message,
        state,
        prompt="قیمت به تومان را بفرستید:",
        cancel_code="pcm_hub",
        fsm_state=PlanCatalogStates.addon_price,
        edit=False,
    )


@router.message(PlanCatalogStates.addon_price)
async def pcm_addon_price_save(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    from app.services.service_addons import create_pack, list_packs

    if kb.is_cancel_text(message.text):
        from app.bot.nav_input import try_legacy_cancel

        if await try_legacy_cancel(
            message,
            state,
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        ):
            return
        await state.set_state(None)
        await message.answer(
            "لغو شد.",
            reply_markup=await _hub_reply_kb(
                session, state, is_reseller_bot=is_reseller_bot
            ),
        )
        return
    try:
        price = max(0, parse_bot_int(message.text))
    except ValueError:
        await message.answer("عدد معتبر بفرستید.")
        return
    staff = await resolve_catalog_staff(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    data = await state.get_data()
    if not staff:
        await state.clear()
        await message.answer("دسترسی ندارید.")
        return
    try:
        pack = await create_pack(
            session,
            staff,
            name=str(data.get("_pcm_addon_name") or "بسته"),
            kind=str(data.get("_pcm_addon_kind") or "volume"),
            amount=float(data.get("_pcm_addon_amount") or 1),
            price=price,
        )
    except (ShopScopeError, ValueError) as e:
        await message.answer(user_safe_error(e))
        return
    except Exception as e:
        await message.answer(f"خطا: {user_safe_error(e)}")
        return
    await state.set_state(None)
    packs = await list_packs(session, staff, active_only=False)
    await message.answer(
        f"✅ بسته «{html.escape(pack.name)}» ذخیره شد.",
        reply_markup=await _hub_reply_kb(
            session, state, is_reseller_bot=is_reseller_bot
        ),
    )
    await message.answer("بسته‌ها:", reply_markup=_addon_list_markup(packs))


@router.callback_query(F.data.startswith("pcm:addon:tog:"))
async def pcm_addon_toggle(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    from app.services.service_addons import get_owned_pack, list_packs

    staff = await _ctx_from_callback(
        callback,
        session,
        db_user,
        state,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if not staff:
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    try:
        pid = int(callback.data.rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer("نامعتبر", show_alert=True)
        return
    pack = await get_owned_pack(session, pid, staff)
    if not pack:
        await callback.answer("یافت نشد", show_alert=True)
        return
    pack.is_active = not pack.is_active
    await session.commit()
    packs = await list_packs(session, staff, active_only=False)
    await callback.answer("ذخیره شد")
    if callback.message:
        await safe_edit_text(
            callback.message,
            "⏱ <b>بسته حجم / زمان</b>\nروشن/خاموش · ویرایش · حذف",
            reply_markup=_addon_list_markup(packs),
        )


@router.callback_query(F.data.startswith("pcm:addon:del:"))
async def pcm_addon_delete(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    from app.services.service_addons import delete_pack, list_packs

    staff = await _ctx_from_callback(
        callback,
        session,
        db_user,
        state,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if not staff:
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    try:
        pid = int(callback.data.rsplit(":", 1)[-1])
        await delete_pack(session, staff, pid)
    except (ShopScopeError, ValueError) as e:
        await callback.answer(user_safe_error(e, limit=160), show_alert=True)
        return
    except Exception as e:
        await callback.answer(user_safe_error(e, limit=160), show_alert=True)
        return
    packs = await list_packs(session, staff, active_only=False)
    await callback.answer("حذف شد")
    if callback.message:
        await safe_edit_text(
            callback.message,
            "⏱ <b>بسته حجم / زمان</b>\nروشن/خاموش · ویرایش · حذف",
            reply_markup=_addon_list_markup(packs),
        )


@router.callback_query(F.data.startswith("pcm:addon:edit:"))
async def pcm_addon_edit_ask(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    from app.services.service_addons import get_owned_pack

    staff = await _ctx_from_callback(
        callback,
        session,
        db_user,
        state,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if not staff:
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    try:
        pid = int(callback.data.rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer("نامعتبر", show_alert=True)
        return
    pack = await get_owned_pack(session, pid, staff)
    if not pack:
        await callback.answer("یافت نشد", show_alert=True)
        return
    await state.set_state(PlanCatalogStates.addon_edit_field)
    await state.update_data(_pcm_edit_addon_id=pid, _pcm_edit_addon_field="name")
    await callback.answer()
    from app.bot.nav_input import ask_text

    await ask_text(
        callback,
        state,
        prompt=(
            f"نام جدید برای «{html.escape(pack.name)}» "
            f"(یا همان نام را بفرستید؛ بعد مقدار و قیمت پرسیده می‌شود):"
        ),
        cancel_code="pcm_hub",
        fsm_state=PlanCatalogStates.addon_edit_field,
        edit=True,
    )


@router.message(PlanCatalogStates.addon_edit_field)
async def pcm_addon_edit_save(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    from app.services.service_addons import (
        get_owned_pack,
        list_packs,
        update_pack,
    )

    if kb.is_cancel_text(message.text):
        from app.bot.nav_input import try_legacy_cancel

        if await try_legacy_cancel(
            message,
            state,
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        ):
            return
        await state.set_state(None)
        await message.answer(
            "لغو شد.",
            reply_markup=await _hub_reply_kb(
                session, state, is_reseller_bot=is_reseller_bot
            ),
        )
        return
    staff = await resolve_catalog_staff(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    data = await state.get_data()
    pid = int(data.get("_pcm_edit_addon_id") or 0)
    field = data.get("_pcm_edit_addon_field") or "name"
    if not staff or not pid:
        await state.clear()
        await message.answer("دسترسی ندارید.")
        return
    pack = await get_owned_pack(session, pid, staff)
    if not pack:
        await state.clear()
        await message.answer("یافت نشد.")
        return
    text = (message.text or "").strip()
    try:
        if field == "name":
            if not text:
                await message.answer("نام خالی نیست.")
                return
            await state.update_data(
                _pcm_edit_addon_name=text[:128],
                _pcm_edit_addon_field="amount",
            )
            ask = (
                "مقدار به گیگ:"
                if pack.kind == "volume"
                else "تعداد روز:"
            )
            from app.bot.nav_input import ask_text

            await ask_text(
                message,
                state,
                prompt=f"{ask} (فعلی: {pack.amount:g})",
                cancel_code="pcm_hub",
                fsm_state=PlanCatalogStates.addon_edit_field,
                edit=False,
            )
            return
        if field == "amount":
            if pack.kind == "duration":
                amount = float(max(1, parse_bot_int(text)))
            else:
                amount = parse_bot_float(text)
                if amount <= 0:
                    raise ValueError("amount")
            await state.update_data(
                _pcm_edit_addon_amount=amount,
                _pcm_edit_addon_field="price",
            )
            from app.bot.nav_input import ask_text

            await ask_text(
                message,
                state,
                prompt=f"قیمت تومان (فعلی: {int(pack.price):,}):",
                cancel_code="pcm_hub",
                fsm_state=PlanCatalogStates.addon_edit_field,
                edit=False,
            )
            return
        # field == price → commit
        price = max(0, parse_bot_int(text))
        await update_pack(
            session,
            staff,
            pid,
            name=str(data.get("_pcm_edit_addon_name") or pack.name),
            amount=float(data.get("_pcm_edit_addon_amount") or pack.amount),
            price=price,
        )
    except (ShopScopeError, ValueError) as e:
        await message.answer(user_safe_error(e))
        return
    except Exception as e:
        await message.answer(f"خطا: {user_safe_error(e)}")
        return
    await state.set_state(None)
    packs = await list_packs(session, staff, active_only=False)
    await message.answer(
        "ذخیره شد ✅",
        reply_markup=await _hub_reply_kb(
            session, state, is_reseller_bot=is_reseller_bot
        ),
    )
    await message.answer("بسته‌ها:", reply_markup=_addon_list_markup(packs))
