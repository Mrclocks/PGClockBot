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
from app.db.models import BotUser, Order, PaymentMethod, UserService
from app.services.delivery import send_delivery_to_user
from app.services.formatting import format_message, format_toman, kv_line
from app.services.orders import (
    apply_discount_to_order,
    calc_custom_plan_price,
    calc_wholesale_price,
    create_custom_order,
    create_order,
    create_wholesale_order,
    get_catalog_plan,
    get_plan,
    list_active_plans,
    mark_order_free_paid,
    parse_wholesale_tiers,
    pay_with_wallet,
    stars_amount_for_toman,
    start_card_payment,
    start_method_payment,
    wholesale_bounds,
    wholesale_description,
    wholesale_tier_percent,
)
from app.services.users import get_all_settings, on
from app.services.safe_format import safe_format

router = Router(name="shop")

async def _show_order_pay(message, session, db_user, order_id, state, text: str):
    """Show order summary then payment methods on the reply keyboard."""
    from app.bot.menu_nav import present_order_pay
    if message is not None:
        try:
            from app.bot.tg_utils import safe_edit_text
            await safe_edit_text(message, text, reply_markup=None)
        except Exception:
            try:
                await message.answer(text)
            except Exception:
                pass
        await present_order_pay(message, session, db_user, order_id, state=state, text="💳 روش پرداخت را از کیبورد پایین انتخاب کنید:")



class ShopStates(StatesGroup):
    discount = State()
    custom_gb_input = State()
    custom_days_input = State()
    wholesale_qty_input = State()


def _custom_bounds(ui: dict) -> tuple[int, int, int, int, int, int]:
    min_gb = max(1, int(float(ui.get("custom_plan_min_gb") or 1)))
    max_gb = max(min_gb, int(float(ui.get("custom_plan_max_gb") or 500)))
    min_days = max(1, int(float(ui.get("custom_plan_min_days") or 1)))
    max_days = max(min_days, int(float(ui.get("custom_plan_max_days") or 365)))
    price_gb = int(float(ui.get("custom_plan_price_per_gb") or 1000))
    price_day = int(float(ui.get("custom_plan_price_per_day") or 500))
    return min_gb, max_gb, min_days, max_days, price_gb, price_day


def _custom_has_pg_link(ui: dict) -> bool:
    tpl = (ui.get("custom_plan_template_id") or "").strip()
    groups = (ui.get("custom_plan_group_ids") or "").strip()
    return bool(tpl or groups)


async def _custom_available_for_users(
    session: AsyncSession,
    ui: dict,
    *,
    plans: list | None = None,
) -> bool:
    """Custom plan only when enabled, linked, AND at least one catalog plan exists."""
    if not on(ui.get("custom_plan_enabled")):
        return False
    from app.services.users import current_shop_reseller_id

    shop_rid = current_shop_reseller_id()
    if shop_rid is not None:
        # Mirror create_custom_order: reseller shops need their own PG link, not platform inheritance
        from app.db.models import ResellerSetting

        own = await session.execute(
            select(ResellerSetting).where(
                ResellerSetting.reseller_user_id == int(shop_rid),
                ResellerSetting.key.in_(
                    ("custom_plan_template_id", "custom_plan_group_ids")
                ),
            )
        )
        own_map = {r.key: (r.value or "").strip() for r in own.scalars().all()}
        if not (own_map.get("custom_plan_template_id") or own_map.get("custom_plan_group_ids")):
            return False
    elif not _custom_has_pg_link(ui):
        return False
    if plans is None:
        plans = await list_active_plans(session, include_trial=True)
    catalog = [p for p in plans if not p.is_trial]
    return bool(catalog)


async def _custom_gate(
    session: AsyncSession,
    ui: dict,
    state: FSMContext | None = None,
    *,
    plans: list | None = None,
) -> bool:
    """Reuse FSM gate after first full check — steppers skip catalog re-query."""
    if state is not None:
        data = await state.get_data()
        if data.get("custom_gate_ok") and on(ui.get("custom_plan_enabled")):
            return True
    ok = await _custom_available_for_users(session, ui, plans=plans)
    if ok and state is not None:
        await state.update_data(custom_gate_ok=True)
    return ok


async def _shop_kind_flags(
    session: AsyncSession,
    db_user: BotUser,
) -> tuple[dict, bool, bool, bool, bool, list, list, list]:
    """UI + enabled kinds + plan lists for the shop kind picker."""
    ui = await get_all_settings(session)
    trial_setting = on(ui.get("trial_enabled"))
    plans = await list_active_plans(session, include_trial=True)
    if not trial_setting:
        plans = [p for p in plans if not p.is_trial]
    has_svc = (
        await session.execute(
            select(UserService.id).where(UserService.bot_user_id == db_user.id).limit(1)
        )
    ).scalar_one_or_none()
    if has_svc:
        plans = [p for p in plans if not p.is_trial]
    fixed_plans = [p for p in plans if not p.is_trial]
    trial_plans = [p for p in plans if p.is_trial]
    custom_on = await _custom_available_for_users(session, ui, plans=plans)
    wholesale_on = on(ui.get("wholesale_enabled")) and bool(fixed_plans)
    fixed_on = bool(fixed_plans)
    trial_on = bool(trial_plans) and trial_setting
    return (
        ui,
        fixed_on,
        trial_on,
        custom_on,
        wholesale_on,
        plans,
        fixed_plans,
        trial_plans,
    )


@router.callback_query(F.data == "shop:list")
async def shop_list(callback: CallbackQuery, session: AsyncSession, db_user: BotUser, state: FSMContext):
    await callback.answer()
    await state.clear()
    ui, fixed_on, trial_on, custom_on, wholesale_on, *_rest = await _shop_kind_flags(
        session, db_user
    )
    if not any((fixed_on, trial_on, custom_on, wholesale_on)):
        text = format_message(
            "🛒 فروشگاه",
            ui.get("shop_empty_text")
            or "در حال حاضر پلنی برای فروش فعال نیست.",
        )
        if callback.message:
            await safe_edit_text(callback.message, text, reply_markup=None)
            await callback.message.answer(text, reply_markup=kb.persistent_reply_keyboard(ui))
        return
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message(
                "🛒 فروشگاه",
                "ابتدا <b>نوع پلن</b> را انتخاب کنید (مثل وب‌پنل):",
            ),
            reply_markup=kb.shop_kind_keyboard(
                ui,
                fixed_on=fixed_on,
                trial_on=trial_on,
                custom_on=custom_on,
                wholesale_on=wholesale_on,
            ),
        )
        await state.update_data(_shop_custom=custom_on, _shop_wholesale=wholesale_on)
        await callback.message.answer(
            "فروشگاه:",
            reply_markup=kb.shop_reply_keyboard(ui),
        )


@router.callback_query(F.data == "shop:kind:fixed")
async def shop_kind_fixed(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser, state: FSMContext
):
    await callback.answer()
    ui, fixed_on, *_rest, fixed_plans, _trial = await _shop_kind_flags(session, db_user)
    if not fixed_on:
        await callback.answer("پلن ثابت فعال نیست.", show_alert=True)
        return
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message("💎 پلن ثابت", "یکی از پلن‌ها را انتخاب کنید:"),
            reply_markup=kb.plans_keyboard(
                fixed_plans,
                ui,
                back_callback="shop:list",
            ),
        )


@router.callback_query(F.data == "shop:kind:trial")
async def shop_kind_trial(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser, state: FSMContext
):
    ui, _fixed, trial_on, *_rest, _fixed_plans, trial_plans = await _shop_kind_flags(
        session, db_user
    )
    if not trial_on or not trial_plans:
        await callback.answer("پلن تست در دسترس نیست.", show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message("🎁 پلن تست", "پلن تست را انتخاب کنید:"),
            reply_markup=kb.plans_keyboard(
                trial_plans,
                ui,
                back_callback="shop:list",
            ),
        )


@router.callback_query(F.data == "shop:kind:custom")
async def shop_kind_custom(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext
):
    await custom_start(callback, session, state)


@router.callback_query(F.data == "shop:kind:wholesale")
async def shop_kind_wholesale(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext
):
    await wholesale_start(callback, session, state)


@router.callback_query(F.data == "shop:custom:noop")
async def custom_noop(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data == "shop:custom")
async def custom_start(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    ui = await get_all_settings(session)
    if not await _custom_gate(session, ui, state):
        await callback.answer(
            "پلن دلخواه در دسترس نیست (پلنی تعریف نشده یا غیرفعال است).",
            show_alert=True,
        )
        return
    await callback.answer()
    min_gb, max_gb, _, _, _, _ = _custom_bounds(ui)
    data = await state.get_data()
    gb = int(data.get("custom_gb") or min_gb)
    gb = max(min_gb, min(max_gb, gb))
    await state.update_data(custom_gb=gb, custom_days=data.get("custom_days"), custom_gate_ok=True)
    text = format_message(
        "✨ پلن دلخواه — حجم",
        f"حجم سرویس را انتخاب کنید ({min_gb} تا {max_gb} گیگ):\n"
        f"فعلی: <b>{gb}</b> گیگ",
    )
    if callback.message:
        await safe_edit_text(callback.message, text, reply_markup=kb.custom_gb_keyboard(gb, ui))


@router.callback_query(F.data.in_({"shop:custom:gb:+", "shop:custom:gb:-"}))
async def custom_gb_step(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    ui = await get_all_settings(session)
    if not await _custom_gate(session, ui, state):
        await callback.answer("پلن دلخواه در دسترس نیست", show_alert=True)
        return
    min_gb, max_gb, _, _, _, _ = _custom_bounds(ui)
    data = await state.get_data()
    gb = int(data.get("custom_gb") or min_gb)
    if callback.data.endswith("+"):
        gb = min(max_gb, gb + 1)
    else:
        gb = max(min_gb, gb - 1)
    await state.update_data(custom_gb=gb)
    await callback.answer()
    text = format_message(
        "✨ پلن دلخواه — حجم",
        f"حجم سرویس را انتخاب کنید ({min_gb} تا {max_gb} گیگ):\n"
        f"فعلی: <b>{gb}</b> گیگ",
    )
    if callback.message:
        await safe_edit_text(callback.message, text, reply_markup=kb.custom_gb_keyboard(gb, ui))


@router.callback_query(F.data == "shop:custom:gb:input")
async def custom_gb_ask(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    ui = await get_all_settings(session)
    if not await _custom_gate(session, ui, state):
        await callback.answer("پلن دلخواه در دسترس نیست", show_alert=True)
        return
    await callback.answer()
    min_gb, max_gb, _, _, _, _ = _custom_bounds(ui)
    await state.set_state(ShopStates.custom_gb_input)
    if callback.message:
        await callback.message.answer(
            f"حجم به گیگ را وارد کنید ({min_gb} تا {max_gb}):",
            reply_markup=kb.cancel_reply(),
        )


@router.message(ShopStates.custom_gb_input)
async def custom_gb_entered(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    ui = await get_all_settings(session)
    if kb.is_cancel_text(message.text):
        from app.bot.menu_nav import restore_main_reply

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
    min_gb, max_gb, _, _, _, _ = _custom_bounds(ui)
    try:
        gb = int(float((message.text or "").replace(",", "").replace("٬", "").strip()))
    except ValueError:
        await message.answer("عدد معتبر بفرستید")
        return
    if gb < min_gb or gb > max_gb:
        await message.answer(f"حجم باید بین {min_gb} تا {max_gb} باشد")
        return
    await state.set_state(None)
    await state.update_data(custom_gb=gb)
    await message.answer(
        format_message(
            "✨ پلن دلخواه — حجم",
            f"حجم انتخاب‌شده: <b>{gb}</b> گیگ",
        ),
        reply_markup=kb.custom_gb_keyboard(gb, ui),
    )


@router.callback_query(F.data == "shop:custom:gb:next")
async def custom_days_start(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    ui = await get_all_settings(session)
    if not await _custom_gate(session, ui, state):
        await callback.answer("پلن دلخواه در دسترس نیست", show_alert=True)
        return
    await callback.answer()
    _, _, min_days, max_days, _, _ = _custom_bounds(ui)
    data = await state.get_data()
    gb = int(data.get("custom_gb") or _custom_bounds(ui)[0])
    days = int(data.get("custom_days") or min_days)
    days = max(min_days, min(max_days, days))
    await state.update_data(custom_gb=gb, custom_days=days)
    text = format_message(
        "✨ پلن دلخواه — مدت",
        f"حجم: <b>{gb}</b> گیگ\n"
        f"مدت را انتخاب کنید ({min_days} تا {max_days} روز):\n"
        f"فعلی: <b>{days}</b> روز",
    )
    if callback.message:
        await safe_edit_text(callback.message, text, reply_markup=kb.custom_days_keyboard(days, ui))


@router.callback_query(F.data.in_({"shop:custom:days:+", "shop:custom:days:-"}))
async def custom_days_step(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    ui = await get_all_settings(session)
    if not await _custom_gate(session, ui, state):
        await callback.answer("پلن دلخواه در دسترس نیست", show_alert=True)
        return
    _, _, min_days, max_days, _, _ = _custom_bounds(ui)
    data = await state.get_data()
    gb = int(data.get("custom_gb") or _custom_bounds(ui)[0])
    days = int(data.get("custom_days") or min_days)
    if callback.data.endswith("+"):
        days = min(max_days, days + 1)
    else:
        days = max(min_days, days - 1)
    await state.update_data(custom_days=days)
    await callback.answer()
    text = format_message(
        "✨ پلن دلخواه — مدت",
        f"حجم: <b>{gb}</b> گیگ\n"
        f"مدت را انتخاب کنید ({min_days} تا {max_days} روز):\n"
        f"فعلی: <b>{days}</b> روز",
    )
    if callback.message:
        await safe_edit_text(callback.message, text, reply_markup=kb.custom_days_keyboard(days, ui))


@router.callback_query(F.data == "shop:custom:days:input")
async def custom_days_ask(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    ui = await get_all_settings(session)
    if not await _custom_gate(session, ui, state):
        await callback.answer("پلن دلخواه در دسترس نیست", show_alert=True)
        return
    await callback.answer()
    _, _, min_days, max_days, _, _ = _custom_bounds(ui)
    await state.set_state(ShopStates.custom_days_input)
    if callback.message:
        await callback.message.answer(
            f"مدت به روز را وارد کنید ({min_days} تا {max_days}):",
            reply_markup=kb.cancel_reply(),
        )


@router.message(ShopStates.custom_days_input)
async def custom_days_entered(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    ui = await get_all_settings(session)
    if kb.is_cancel_text(message.text):
        from app.bot.menu_nav import restore_main_reply

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
    _, _, min_days, max_days, _, _ = _custom_bounds(ui)
    try:
        days = int((message.text or "").replace(",", "").replace("٬", "").strip())
    except ValueError:
        await message.answer("عدد معتبر بفرستید")
        return
    if days < min_days or days > max_days:
        await message.answer(f"مدت باید بین {min_days} تا {max_days} باشد")
        return
    data = await state.get_data()
    gb = int(data.get("custom_gb") or _custom_bounds(ui)[0])
    await state.set_state(None)
    await state.update_data(custom_days=days)
    await message.answer(
        format_message(
            "✨ پلن دلخواه — مدت",
            f"حجم: <b>{gb}</b> گیگ\nمدت انتخاب‌شده: <b>{days}</b> روز",
        ),
        reply_markup=kb.custom_days_keyboard(days, ui),
    )


@router.callback_query(F.data == "shop:custom:confirm")
async def custom_confirm(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    ui = await get_all_settings(session)
    if not await _custom_gate(session, ui, state):
        await callback.answer("پلن دلخواه در دسترس نیست", show_alert=True)
        return
    data = await state.get_data()
    min_gb, _, min_days, _, price_gb, price_day = _custom_bounds(ui)
    gb = int(data.get("custom_gb") or min_gb)
    days = int(data.get("custom_days") or min_days)
    amount = calc_custom_plan_price(
        gb=gb, days=days, price_per_gb=price_gb, price_per_day=price_day
    )
    await callback.answer()
    from app.services.formatting import info_block, kv_line

    body = info_block(
        [
            kv_line("📦", "حجم", f"<b>{gb}</b> گیگ"),
            kv_line("⏱", "مدت", f"<b>{days}</b> روز"),
            kv_line("💰", "قیمت", f"<b>{format_toman(amount, get_settings().currency)}</b>"),
            f"<i>({price_gb:,} ت/گیگ + {price_day:,} ت/روز)</i>".replace(",", "٬"),
        ]
    )
    if callback.message:
        await safe_edit_text(callback.message, 
            format_message("✨ تأیید پلن دلخواه", body),
            reply_markup=kb.custom_confirm_keyboard(ui),
        )


async def _notify_new_order(bot, session, order, db_user, plan_name: str | None):
    try:
        from app.services.notifications import notify_new_order

        await notify_new_order(
            bot,
            session,
            order=order,
            user_tg_id=db_user.telegram_id,
            user_name=db_user.full_name or db_user.username,
            plan_name=plan_name,
        )
    except Exception:
        pass


@router.callback_query(F.data == "shop:custom:buy")
async def custom_buy(callback: CallbackQuery, session: AsyncSession, db_user: BotUser, state: FSMContext):
    ui = await get_all_settings(session)
    if not await _custom_available_for_users(session, ui):
        await callback.answer("پلن دلخواه در دسترس نیست", show_alert=True)
        return
    data = await state.get_data()
    min_gb, _, min_days, _, price_gb, price_day = _custom_bounds(ui)
    gb = float(data.get("custom_gb") or min_gb)
    days = int(data.get("custom_days") or min_days)
    expected = calc_custom_plan_price(
        gb=gb, days=days, price_per_gb=price_gb, price_per_day=price_day
    )
    if expected > 0 and not kb.any_checkout_method_enabled(ui):
        await callback.answer("هیچ روش پرداختی فعال نیست", show_alert=True)
        return
    try:
        order = await create_custom_order(
            session,
            user_id=db_user.id,
            data_limit_gb=gb,
            duration_days=days,
            reseller_id=db_user.reseller_id,
        )
    except ValueError as e:
        await callback.answer(str(e), show_alert=True)
        return
    await state.clear()
    await callback.answer()

    if order.amount <= 0:
        from app.services.orders import deliver_order, revert_failed_free_delivery

        await mark_order_free_paid(session, order, db_user.id)
        try:
            order = await deliver_order(session, order)
        except Exception as e:
            try:
                await revert_failed_free_delivery(session, order)
            except Exception:
                pass
            if callback.message:
                await safe_edit_text(callback.message, 
                    format_message("❌ خطا در تحویل", str(e)),
                    reply_markup=kb.back_home(ui),
                )
            return
        if callback.message:
            await safe_edit_text(callback.message, 
                format_message("✅ فعال شد", f"سفارش #{order.id} تحویل شد."),
                reply_markup=kb.back_home(ui),
            )
        try:
            await send_delivery_to_user(callback.bot, db_user.telegram_id, session, None, order)
        except Exception:
            pass
        return

    text = format_message(
        f"🧾 سفارش #{order.id}",
        f"پلن دلخواه — <b>{gb:g}</b> گیگ / <b>{days}</b> روز\n"
        f"{kv_line('💰', 'مبلغ قابل پرداخت', f'<b>{format_toman(order.amount, get_settings().currency)}</b>')}\n\n"
        "روش پرداخت را انتخاب کنید:",
    )
    if callback.message:
        await _show_order_pay(callback.message, session, db_user, order.id, state, text)
    await _notify_new_order(callback.bot, session, order, db_user, "پلن دلخواه")


@router.callback_query(F.data == "shop:wholesale:noop")
async def wholesale_noop(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data == "shop:wholesale")
async def wholesale_start(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    ui = await get_all_settings(session)
    if not on(ui.get("wholesale_enabled")):
        await callback.answer("فروش عمده فعال نیست", show_alert=True)
        return
    plans = await list_active_plans(session, include_trial=False)
    if not plans:
        await callback.answer("پلنی برای فروش عمده نیست", show_alert=True)
        return
    # Keep shop nav stack; only reset wholesale qty FSM fields
    await state.set_state(None)
    await state.update_data(wholesale_plan_id=None, wholesale_qty=None)
    await callback.answer()
    text = format_message(
        "📦 فروش عمده",
        wholesale_description(ui) + "\n\nابتدا نوع سرویس (پلن) را انتخاب کنید:",
    )
    if callback.message:
        await safe_edit_text(
            callback.message, text, reply_markup=kb.wholesale_plans_keyboard(plans, ui)
        )


@router.callback_query(F.data.startswith("shop:wholesale:plan:"))
async def wholesale_pick_plan(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    ui = await get_all_settings(session)
    if not on(ui.get("wholesale_enabled")):
        await callback.answer("فروش عمده فعال نیست", show_alert=True)
        return
    plan_id = int(callback.data.split(":")[-1])
    plan = await get_catalog_plan(session, plan_id)
    if not plan or not plan.is_active or plan.is_trial:
        await callback.answer("پلن پیدا نشد", show_alert=True)
        return
    mn, mx = wholesale_bounds(ui)
    await state.update_data(wholesale_plan_id=plan.id, wholesale_qty=mn)
    await callback.answer()
    text = format_message(
        "📦 فروش عمده — تعداد",
        f"پلن: <b>{plan.name}</b>\n"
        f"قیمت واحد: <b>{format_toman(plan.price, get_settings().currency)}</b>\n\n"
        f"{wholesale_description(ui)}\n\n"
        f"تعداد درخواستی را انتخاب کنید ({mn} تا {mx}):\n"
        f"فعلی: <b>{mn}</b> عدد",
    )
    if callback.message:
        await safe_edit_text(
            callback.message,
            text,
            reply_markup=kb.wholesale_qty_keyboard(mn, ui, plan_id=plan.id),
        )


@router.callback_query(F.data == "shop:wholesale:qty")
async def wholesale_qty_back(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    ui = await get_all_settings(session)
    data = await state.get_data()
    plan_id = int(data.get("wholesale_plan_id") or 0)
    plan = await get_catalog_plan(session, plan_id) if plan_id else None
    if not plan:
        await wholesale_start(callback, session, state)
        return
    mn, mx = wholesale_bounds(ui)
    qty = int(data.get("wholesale_qty") or mn)
    qty = max(mn, min(mx, qty))
    await state.update_data(wholesale_qty=qty)
    await callback.answer()
    text = format_message(
        "📦 فروش عمده — تعداد",
        f"پلن: <b>{plan.name}</b>\n"
        f"قیمت واحد: <b>{format_toman(plan.price, get_settings().currency)}</b>\n\n"
        f"{wholesale_description(ui)}\n\n"
        f"تعداد درخواستی را انتخاب کنید ({mn} تا {mx}):\n"
        f"فعلی: <b>{qty}</b> عدد",
    )
    if callback.message:
        await safe_edit_text(
            callback.message,
            text,
            reply_markup=kb.wholesale_qty_keyboard(qty, ui, plan_id=plan.id),
        )


@router.callback_query(F.data.in_({"shop:wholesale:qty:+", "shop:wholesale:qty:-"}))
async def wholesale_qty_step(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    ui = await get_all_settings(session)
    if not on(ui.get("wholesale_enabled")):
        await callback.answer("فروش عمده فعال نیست", show_alert=True)
        return
    data = await state.get_data()
    plan_id = int(data.get("wholesale_plan_id") or 0)
    plan = await get_catalog_plan(session, plan_id) if plan_id else None
    if not plan:
        await callback.answer("ابتدا پلن را انتخاب کنید", show_alert=True)
        return
    mn, mx = wholesale_bounds(ui)
    qty = int(data.get("wholesale_qty") or mn)
    if callback.data.endswith("+"):
        qty = min(mx, qty + 1)
    else:
        qty = max(mn, qty - 1)
    await state.update_data(wholesale_qty=qty)
    await callback.answer()
    tiers = parse_wholesale_tiers(ui.get("wholesale_tiers"))
    pct = wholesale_tier_percent(qty, tiers)
    payable, discount = calc_wholesale_price(
        unit_price=plan.price, quantity=qty, percent=pct
    )
    disc_line = f"\nتخفیف فعلی: <b>{pct}٪</b> (−{format_toman(discount, get_settings().currency)})" if pct else ""
    text = format_message(
        "📦 فروش عمده — تعداد",
        f"پلن: <b>{plan.name}</b>\n"
        f"قیمت واحد: <b>{format_toman(plan.price, get_settings().currency)}</b>\n\n"
        f"{wholesale_description(ui)}\n\n"
        f"فعلی: <b>{qty}</b> عدد{disc_line}\n"
        f"جمع: <b>{format_toman(payable, get_settings().currency)}</b>",
    )
    if callback.message:
        await safe_edit_text(
            callback.message,
            text,
            reply_markup=kb.wholesale_qty_keyboard(qty, ui, plan_id=plan.id),
        )


@router.callback_query(F.data == "shop:wholesale:qty:input")
async def wholesale_qty_ask(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    ui = await get_all_settings(session)
    if not on(ui.get("wholesale_enabled")):
        await callback.answer("فروش عمده فعال نیست", show_alert=True)
        return
    mn, mx = wholesale_bounds(ui)
    await state.set_state(ShopStates.wholesale_qty_input)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            f"تعداد را عددی بین {mn} تا {mx} بفرستید:",
            reply_markup=kb.cancel_reply(),
        )


@router.message(ShopStates.wholesale_qty_input)
async def wholesale_qty_entered(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    ui = await get_all_settings(session)
    if kb.is_cancel_text(message.text):
        from app.bot.menu_nav import restore_main_reply

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
    mn, mx = wholesale_bounds(ui)
    raw = (message.text or "").strip().replace(",", "").replace("٬", "")
    try:
        qty = int(raw)
    except ValueError:
        await message.answer(f"عدد معتبر بین {mn} تا {mx} بفرستید.")
        return
    if qty < mn or qty > mx:
        await message.answer(f"تعداد باید بین {mn} تا {mx} باشد.")
        return
    data = await state.get_data()
    plan_id = int(data.get("wholesale_plan_id") or 0)
    plan = await get_catalog_plan(session, plan_id) if plan_id else None
    if not plan:
        await state.clear()
        await message.answer("پلن نامعتبر است — دوباره از فروشگاه شروع کنید.")
        return
    await state.set_state(None)
    await state.update_data(wholesale_qty=qty)
    tiers = parse_wholesale_tiers(ui.get("wholesale_tiers"))
    pct = wholesale_tier_percent(qty, tiers)
    payable, discount = calc_wholesale_price(
        unit_price=plan.price, quantity=qty, percent=pct
    )
    disc_line = f"\nتخفیف: <b>{pct}٪</b> (−{format_toman(discount, get_settings().currency)})" if pct else ""
    text = format_message(
        "📦 فروش عمده — تعداد",
        f"پلن: <b>{plan.name}</b>\n"
        f"فعلی: <b>{qty}</b> عدد{disc_line}\n"
        f"جمع: <b>{format_toman(payable, get_settings().currency)}</b>",
    )
    # Leave cancel_reply; restore shop chrome so user is not stuck on «انصراف»
    from app.bot import menu_nav as nav

    custom_on = on(ui.get("custom_plan_enabled"))
    wholesale_on = True
    await state.update_data(_shop_custom=custom_on, _shop_wholesale=wholesale_on)
    await nav.show_nav_keyboard(
        message,
        session,
        db_user,
        nav.NAV_SHOP,
        text=text,
        state=state,
        push=False,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    await message.answer(
        "تعداد را با دکمه‌ها تنظیم کنید:",
        reply_markup=kb.wholesale_qty_keyboard(qty, ui, plan_id=plan.id),
    )


@router.callback_query(F.data == "shop:wholesale:confirm")
async def wholesale_confirm(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    ui = await get_all_settings(session)
    if not on(ui.get("wholesale_enabled")):
        await callback.answer("فروش عمده فعال نیست", show_alert=True)
        return
    data = await state.get_data()
    plan_id = int(data.get("wholesale_plan_id") or 0)
    plan = await get_catalog_plan(session, plan_id) if plan_id else None
    if not plan:
        await callback.answer("ابتدا پلن را انتخاب کنید", show_alert=True)
        return
    mn, mx = wholesale_bounds(ui)
    qty = int(data.get("wholesale_qty") or mn)
    qty = max(mn, min(mx, qty))
    await state.update_data(wholesale_qty=qty)
    tiers = parse_wholesale_tiers(ui.get("wholesale_tiers"))
    pct = wholesale_tier_percent(qty, tiers)
    payable, discount = calc_wholesale_price(
        unit_price=plan.price, quantity=qty, percent=pct
    )
    await callback.answer()
    limit = f"{plan.data_limit_gb:g} گیگ" if plan.data_limit_gb is not None else "نامحدود"
    disc_block = ""
    if pct:
        disc_block = (
            f"\n{kv_line('🏷', 'تخفیف عمده', f'<b>{pct}٪</b> (−{format_toman(discount, get_settings().currency)})')}"
        )
    text = format_message(
        "📦 تأیید فروش عمده",
        f"{wholesale_description(ui)}\n\n"
        f"{kv_line('💎', 'پلن', plan.name)}\n"
        f"{kv_line('📦', 'تعداد', f'<b>{qty}</b>')}\n"
        f"{kv_line('📅', 'مدت هر سرویس', f'{plan.duration_days} روز')}\n"
        f"{kv_line('📶', 'حجم هر سرویس', limit)}\n"
        f"{kv_line('💰', 'قیمت واحد', format_toman(plan.price, get_settings().currency))}"
        f"{disc_block}\n"
        f"{kv_line('💳', 'مبلغ قابل پرداخت', f'<b>{format_toman(payable, get_settings().currency)}</b>')}",
    )
    if callback.message:
        await safe_edit_text(
            callback.message, text, reply_markup=kb.wholesale_confirm_keyboard(ui)
        )


@router.callback_query(F.data == "shop:wholesale:buy")
async def wholesale_buy(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser, state: FSMContext
):
    ui = await get_all_settings(session)
    if not on(ui.get("wholesale_enabled")):
        await callback.answer("فروش عمده فعال نیست", show_alert=True)
        return
    data = await state.get_data()
    plan_id = int(data.get("wholesale_plan_id") or 0)
    mn, _mx = wholesale_bounds(ui)
    qty = int(data.get("wholesale_qty") or mn)
    if not plan_id:
        await callback.answer("پلن انتخاب نشده", show_alert=True)
        return
    if not kb.any_checkout_method_enabled(ui):
        await callback.answer("هیچ روش پرداختی فعال نیست", show_alert=True)
        return
    try:
        order = await create_wholesale_order(
            session,
            user_id=db_user.id,
            plan_id=plan_id,
            quantity=qty,
            reseller_id=db_user.reseller_id,
        )
    except ValueError as e:
        await callback.answer(str(e), show_alert=True)
        return
    # Drop wholesale qty FSM; present_order_pay sets NAV_PAY + order id
    await state.set_state(None)
    await state.update_data(wholesale_plan_id=None, wholesale_qty=None)
    await callback.answer()
    plan = await get_catalog_plan(session, plan_id)
    plan_name = plan.name if plan else "پلن"
    text = format_message(
        f"🧾 سفارش عمده #{order.id}",
        f"{plan_name} × <b>{order.quantity}</b>\n"
        f"{kv_line('💰', 'مبلغ قابل پرداخت', f'<b>{format_toman(order.amount, get_settings().currency)}</b>')}\n\n"
        "روش پرداخت را انتخاب کنید:",
    )
    if callback.message:
        await _show_order_pay(callback.message, session, db_user, order.id, state, text)
    await _notify_new_order(
        callback.bot, session, order, db_user, f"فروش عمده ×{order.quantity}"
    )


@router.callback_query(F.data.startswith("shop:plan:"))
async def shop_plan(callback: CallbackQuery, session: AsyncSession):
    ui = await get_all_settings(session)
    plan_id = int(callback.data.split(":")[-1])
    plan = await get_catalog_plan(session, plan_id)
    if not plan:
        await callback.answer("پلن پیدا نشد", show_alert=True)
        return
    await callback.answer()
    limit = f"{plan.data_limit_gb:g} گیگ" if plan.data_limit_gb is not None else "نامحدود"
    from app.services.formatting import info_block, kv_line

    body = info_block(
        [
            plan.description or "",
            kv_line("⏱", "مدت", f"<b>{plan.duration_days}</b> روز"),
            kv_line("📦", "حجم", f"<b>{limit}</b>"),
            kv_line("💰", "قیمت", f"<b>{format_toman(plan.price, get_settings().currency)}</b>"),
        ]
    )
    text = format_message(f"💎 {plan.name}", body)
    if callback.message:
        await safe_edit_text(callback.message, text, reply_markup=kb.plan_actions(plan.id, ui))


@router.callback_query(F.data.startswith("shop:buy:"))
async def shop_buy(callback: CallbackQuery, session: AsyncSession, db_user: BotUser, state: FSMContext):
    ui = await get_all_settings(session)
    plan_id = int(callback.data.split(":")[-1])
    plan = await get_catalog_plan(session, plan_id)
    if not plan:
        await callback.answer("پلن پیدا نشد", show_alert=True)
        return
    if plan.price > 0 and not kb.any_checkout_method_enabled(ui):
        await callback.answer("هیچ روش پرداختی فعال نیست", show_alert=True)
        return

    try:
        order = await create_order(
            session,
            user_id=db_user.id,
            plan_id=plan_id,
            reseller_id=db_user.reseller_id,
        )
    except ValueError as e:
        await callback.answer(str(e), show_alert=True)
        return

    await callback.answer()

    # Free / trial: deliver immediately
    if order.amount <= 0:
        from app.services.orders import deliver_order, revert_failed_free_delivery

        await mark_order_free_paid(session, order, db_user.id)
        try:
            order = await deliver_order(session, order)
        except Exception as e:
            try:
                await revert_failed_free_delivery(session, order)
            except Exception:
                pass
            if callback.message:
                await safe_edit_text(callback.message, 
                    format_message("❌ خطا در تحویل", str(e)),
                    reply_markup=kb.back_home(ui),
                )
            return
        if callback.message:
            await safe_edit_text(callback.message, 
                format_message("✅ فعال شد", f"سفارش #{order.id} تحویل شد."),
                reply_markup=kb.back_home(ui),
            )
        try:
            await send_delivery_to_user(callback.bot, db_user.telegram_id, session, None, order)
        except Exception:
            pass
        return

    text = format_message(
        f"🧾 سفارش #{order.id}",
        f"{kv_line('💰', 'مبلغ قابل پرداخت', f'<b>{format_toman(order.amount, get_settings().currency)}</b>')}\n\n"
        "روش پرداخت را انتخاب کنید:",
    )
    if callback.message:
        await _show_order_pay(callback.message, session, db_user, order.id, state, text)
    await _notify_new_order(callback.bot, session, order, db_user, plan.name if plan else None)


@router.callback_query(F.data.startswith("pay:discount:"))
async def ask_discount(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
):
    ui = await get_all_settings(session)
    if not on(ui.get("pay_discount_enabled")):
        await callback.answer("کد تخفیف غیرفعال است", show_alert=True)
        return
    await callback.answer()
    order_id = int(callback.data.split(":")[-1])
    await state.set_state(ShopStates.discount)
    await state.update_data(order_id=order_id)

    from app.services.loyalty import list_available_discounts

    ents = await list_available_discounts(session, db_user.id)
    rows: list[list[InlineKeyboardButton]] = []
    for e in ents[:5]:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🏷 {e.percent}٪ — {e.code}",
                    callback_data=f"pay:loydisc:{order_id}:{e.id}",
                )
            ]
        )
    hint = "کد تخفیف را ارسال کنید یا از دکمه زیر استفاده کنید:"
    if not ents:
        hint = "کد تخفیف را ارسال کنید یا «انصراف» بزنید:"
    if callback.message:
        await callback.message.answer(
            hint,
            reply_markup=kb.cancel_reply(),
        )
        if rows:
            await callback.message.answer(
                "تخفیف‌های باشگاه شما:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )


@router.callback_query(F.data.startswith("pay:loydisc:"))
async def apply_loyalty_discount_btn(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
):
    parts = (callback.data or "").split(":")
    try:
        order_id = int(parts[2])
        ent_id = int(parts[3])
    except (IndexError, TypeError, ValueError):
        await callback.answer("نامعتبر", show_alert=True)
        return
    from app.db.models import LoyaltyDiscountEntitlement
    from app.bot.menu_nav import present_order_pay

    ent = await session.get(LoyaltyDiscountEntitlement, ent_id)
    order = await session.get(Order, order_id)
    if not order or order.user_id != db_user.id:
        await callback.answer("سفارش نامعتبر", show_alert=True)
        return
    if not ent or int(ent.user_id) != int(db_user.id):
        await callback.answer("تخفیف نامعتبر", show_alert=True)
        return
    await state.clear()
    try:
        order = await apply_discount_to_order(session, order, ent.code)
    except ValueError as e:
        await callback.answer(str(e)[:180], show_alert=True)
        return
    await callback.answer("تخفیف اعمال شد ✅")
    if callback.message:
        await callback.message.answer(
            f"تخفیف اعمال شد ✅\nمبلغ جدید: {format_toman(order.amount, get_settings().currency)}",
        )
        await present_order_pay(callback.message, session, db_user, order.id, state=state)


@router.message(ShopStates.discount)
async def apply_discount_msg(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    from app.bot.menu_nav import restore_main_reply

    ui = await get_all_settings(session)
    if kb.is_cancel_text(message.text):
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
    code_raw = (message.text or "").strip()
    if not code_raw:
        await message.answer("کد تخفیف را به‌صورت متن بفرستید.")
        return
    data = await state.get_data()
    order = await session.get(Order, data.get("order_id"))
    await state.clear()
    if not order or order.user_id != db_user.id:
        await restore_main_reply(
            message,
            session,
            db_user,
            text="سفارش معتبر نیست.",
            state=state,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        return
    from app.bot.menu_nav import present_order_pay

    try:
        order = await apply_discount_to_order(session, order, code_raw)
    except ValueError as e:
        await message.answer(str(e))
        await present_order_pay(message, session, db_user, order.id, state=state)
        return
    await message.answer(
        f"تخفیف اعمال شد ✅\nمبلغ جدید: {format_toman(order.amount, get_settings().currency)}",
    )
    await present_order_pay(message, session, db_user, order.id, state=state)


@router.callback_query(F.data.startswith("pay:wallet:"))
async def pay_wallet_cb(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    ui = await get_all_settings(session)
    if not on(ui.get("pay_wallet_enabled")):
        await callback.answer("این روش پرداخت غیرفعال است", show_alert=True)
        return
    order_id = int(callback.data.split(":")[-1])
    order = await session.get(Order, order_id)
    if not order or order.user_id != db_user.id:
        await callback.answer("سفارش نامعتبر", show_alert=True)
        return
    try:
        order = await pay_with_wallet(session, order, db_user)
    except ValueError as e:
        await callback.answer(str(e), show_alert=True)
        return
    except Exception as e:
        await callback.answer(f"خطا در تحویل: {e}", show_alert=True)
        return

    await callback.answer()
    from app.bot.menu_nav import buyer_main_reply_keyboard, clear_checkout_nav

    await clear_checkout_nav(state)
    main_kb, _ = await buyer_main_reply_keyboard(
        session,
        db_user,
        order=order,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )

    if order.note and str(order.note).startswith("reseller_app:"):
        from app.db.models import ResellerApplicationStatus
        from app.services.resellers import format_credentials_message, get_application

        creds = order.__dict__.get("_reseller_app_creds")
        try:
            app_id = int(str(order.note).split(":", 1)[1])
        except Exception:
            app_id = 0
        app = await get_application(session, app_id) if app_id else None
        approved = bool(
            isinstance(creds, dict)
            or (app and app.status == ResellerApplicationStatus.APPROVED.value)
        )
        if callback.message:
            if isinstance(creds, dict):
                body = format_credentials_message(creds)
                await safe_edit_text(
                    callback.message,
                    body,
                    reply_markup=None,
                )
            else:
                await safe_edit_text(
                    callback.message,
                    format_message(
                        "✅ پرداخت ثبت شد",
                        (
                            "نمایندگی شما فعال شد."
                            if approved
                            else "هزینه نمایندگی پرداخت شد.\nدرخواست شما برای تأیید ادمین ارسال شد."
                        ),
                    ),
                    reply_markup=None,
                )
            try:
                await callback.message.answer(
                    "🏠 منوی اصلی",
                    reply_markup=main_kb,
                )
            except Exception:
                pass
        for aid in get_settings().admin_ids:
            try:
                if approved:
                    await callback.bot.send_message(
                        aid,
                        f"✅ نمایندگی فعال شد — سفارش #{order.id}\n"
                        f"کاربر: {db_user.full_name or db_user.telegram_id}\n"
                        f"(تأیید خودکار پس از پرداخت)",
                    )
                else:
                    await callback.bot.send_message(
                        aid,
                        f"🤝 درخواست نمایندگی پرداخت‌شده — سفارش #{order.id}\n"
                        f"کاربر: {db_user.full_name or db_user.telegram_id}",
                        reply_markup=kb.reseller_app_review(app_id) if app_id else None,
                    )
            except Exception:
                pass
        return

    if callback.message:
        try:
            await safe_edit_text(callback.message, 
                format_message("✅ خرید موفق", "سرویس در حال تحویل است…"),
                reply_markup=None,
            )
        except Exception:
            pass
    try:
        await send_delivery_to_user(
            callback.bot, db_user.telegram_id, session, None, order
        )
    except Exception:
        # Delivery failed to send — still put user back on main menu
        if callback.message:
            try:
                await callback.message.answer("🏠 منوی اصلی", reply_markup=main_kb)
            except Exception:
                pass
    try:
        from app.services.notifications import notify_new_subscription

        plan = await get_plan(session, order.plan_id) if order.plan_id else None
        await notify_new_subscription(
            callback.bot,
            session,
            order=order,
            user_tg_id=db_user.telegram_id,
            user_name=db_user.full_name or db_user.username,
            plan_name=plan.name if plan else None,
            needs_approval=False,
        )
    except Exception:
        pass


async def _await_order_receipt(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    *,
    title: str,
    body: str,
    reply_markup=None,
    state: FSMContext | None = None,
):
    """Show pay instructions and switch reply KB off payment methods (cancel while waiting)."""
    ui = await get_all_settings(session)
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message(title, body),
            reply_markup=reply_markup,
        )
        await callback.message.answer(
            "پس از واریز، عکس رسید را در همین گفتگو بفرستید.\n"
            "با ارسال رسید به منوی اصلی برمی‌گردید.",
            reply_markup=kb.cancel_reply(ui),
        )
    if state is not None:
        from app.bot import menu_nav as nav

        try:
            await state.update_data(**{nav.NAV_LEVEL: nav.NAV_MAIN})
        except Exception:
            pass


@router.callback_query(F.data.startswith("pay:card:"))
async def pay_card_cb(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
):
    ui = await get_all_settings(session)
    if not on(ui.get("pay_card_enabled")):
        await callback.answer("این روش پرداخت غیرفعال است", show_alert=True)
        return
    order_id = int(callback.data.split(":")[-1])
    order = await session.get(Order, order_id)
    if not order or order.user_id != db_user.id:
        await callback.answer("سفارش نامعتبر", show_alert=True)
        return
    try:
        payment = await start_card_payment(session, order, db_user.id)
    except ValueError as e:
        await callback.answer(str(e), show_alert=True)
        return
    await callback.answer()
    amount = format_toman(order.amount, get_settings().currency)
    from app.services.shortcodes import render_user_message

    body = render_user_message(
        ui.get("card_pay_text"),
        f"مبلغ {amount} را کارت به کارت کنید و رسید بفرستید.",
        amount=amount,
        card=ui.get("card_number") or "—",
        holder=ui.get("card_holder") or "—",
        order_id=order.id,
        payment_id=payment.id,
    )
    body += f"\n\n(پرداخت #{payment.id})"
    await _await_order_receipt(
        callback, session, db_user, title="💳 کارت به کارت", body=body, state=state
    )


@router.callback_query(F.data.startswith("pay:gateway:"))
async def pay_gateway_cb(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    ui = await get_all_settings(session)
    if not on(ui.get("pay_gateway_enabled")):
        await callback.answer("این روش پرداخت غیرفعال است", show_alert=True)
        return
    order_id = int(callback.data.split(":")[-1])
    order = await session.get(Order, order_id)
    if not order or order.user_id != db_user.id:
        await callback.answer("سفارش نامعتبر", show_alert=True)
        return
    try:
        payment = await start_method_payment(
            session, order, db_user.id, PaymentMethod.GATEWAY.value
        )
    except ValueError as e:
        await callback.answer(str(e), show_alert=True)
        return
    await callback.answer()
    amount = format_toman(order.amount, get_settings().currency)
    name = ui.get("gateway_name") or "درگاه پرداخت"
    link = (ui.get("gateway_link") or "").strip()
    if link:
        try:
            link = safe_format(link, amount=order.amount, order_id=order.id, payment_id=payment.id)
        except Exception:
            pass
    from app.services.shortcodes import render_user_message

    body = render_user_message(
        ui.get("gateway_pay_text") or "",
        f"مبلغ {amount} را از طریق {name} پرداخت کنید و رسید بفرستید.",
        amount=amount,
        order_id=order.id,
        payment_id=payment.id,
        name=name,
        gateway_name=name,
    )
    body += f"\n\n(پرداخت #{payment.id})"
    rows: list[list[InlineKeyboardButton]] = []
    if link.startswith("http://") or link.startswith("https://"):
        rows.append([InlineKeyboardButton(text=f"🌐 ورود به {name}", url=link)])
    await _await_order_receipt(
        callback,
        session,
        db_user,
        title=f"🌐 {name}",
        body=body,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None,
        state=state,
    )


@router.callback_query(F.data.startswith("pay:crypto:"))
async def pay_crypto_cb(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
):
    ui = await get_all_settings(session)
    if not on(ui.get("pay_crypto_enabled")):
        await callback.answer("این روش پرداخت غیرفعال است", show_alert=True)
        return
    order_id = int(callback.data.split(":")[-1])
    order = await session.get(Order, order_id)
    if not order or order.user_id != db_user.id:
        await callback.answer("سفارش نامعتبر", show_alert=True)
        return
    address = (ui.get("crypto_address") or "").strip()
    if not address:
        await callback.answer("آدرس ولت تنظیم نشده — به ادمین اطلاع دهید", show_alert=True)
        return
    try:
        payment = await start_method_payment(
            session, order, db_user.id, PaymentMethod.CRYPTO.value
        )
    except ValueError as e:
        await callback.answer(str(e), show_alert=True)
        return
    await callback.answer()
    amount = format_toman(order.amount, get_settings().currency)
    from app.services.shortcodes import render_user_message

    body = render_user_message(
        ui.get("crypto_pay_text") or "",
        (
            f"مبلغ {amount}\n"
            f"{ui.get('crypto_asset') or 'USDT'} ({ui.get('crypto_network') or '—'})\n"
            f"<code>{address}</code>\n\nرسید را بفرستید."
        ),
        amount=amount,
        asset=ui.get("crypto_asset") or "USDT",
        network=ui.get("crypto_network") or "—",
        address=address,
        order_id=order.id,
        payment_id=payment.id,
    )
    body += f"\n\n(پرداخت #{payment.id})"
    await _await_order_receipt(
        callback, session, db_user, title="💎 رمزارز", body=body, state=state
    )


@router.callback_query(F.data.startswith("pay:stars:"))
async def pay_stars_cb(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
):
    from aiogram.types import LabeledPrice

    ui = await get_all_settings(session)
    if not on(ui.get("pay_stars_enabled")):
        await callback.answer("این روش پرداخت غیرفعال است", show_alert=True)
        return
    order_id = int(callback.data.split(":")[-1])
    order = await session.get(Order, order_id)
    if not order or order.user_id != db_user.id:
        await callback.answer("سفارش نامعتبر", show_alert=True)
        return
    try:
        payment = await start_method_payment(
            session, order, db_user.id, PaymentMethod.STARS.value
        )
    except ValueError as e:
        await callback.answer(str(e), show_alert=True)
        return
    await callback.answer()
    try:
        rate = int(float(ui.get("stars_toman_per_star") or 500))
    except ValueError:
        rate = 500
    stars = stars_amount_for_toman(order.amount, rate)
    title = (ui.get("stars_title") or "خرید سرویس")[:32]
    desc = (ui.get("stars_description") or f"سفارش #{order.id}")[:255]
    try:
        await callback.bot.send_invoice(
            chat_id=db_user.telegram_id,
            title=title,
            description=desc,
            payload=f"stars:{payment.id}:{stars}",
            currency="XTR",
            prices=[LabeledPrice(label=title, amount=stars)],
            provider_token="",
        )
        if callback.message:
            await safe_edit_text(callback.message, 
                format_message(
                    "⭐ استارز تلگرام",
                    f"فاکتور {stars} استارز برای سفارش #{order.id} ارسال شد.\n"
                    f"(معادل تقریبی {format_toman(order.amount, get_settings().currency)})\n\n"
                    "پس از پرداخت موفق، به منوی اصلی برمی‌گردید.",
                ),
                reply_markup=None,
            )
            await callback.message.answer(
                "فاکتور استارز ارسال شد — پس از پرداخت منتظر بمانید:",
                reply_markup=kb.cancel_reply(ui),
            )
        if state is not None:
            from app.bot import menu_nav as nav

            try:
                await state.update_data(**{nav.NAV_LEVEL: nav.NAV_MAIN})
            except Exception:
                pass
    except Exception as e:
        if callback.message:
            await callback.message.answer(f"خطا در ساخت فاکتور استارز: {e}")
