"""Platform admin — plans hub (parity with web /plans modal)."""

from __future__ import annotations

import html
import json

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.auth import is_platform_admin as _is_admin
from app.bot.auth import require_bot_owner_handler
from app.bot.handlers.admin_settings import CUSTOM_PRICE
from app.config import get_settings
from app.db.models import BotUser, Plan, ResellerPlan
from app.services.orders import parse_wholesale_tiers, wholesale_description
from app.services.pasarguard import get_pg
from app.services.resellers import (
    DEFAULT_FEATURE_PERMS,
    FEATURE_PERMS,
    format_reseller_plan_apply_detail,
    list_reseller_plans,
    parse_perms,
    reseller_plan_mode_of,
)
from app.bot import menu_nav as nav
from app.bot.tg_utils import parse_bot_float, parse_bot_int, safe_edit_text
from app.services.users import get_all_settings, get_setting, on, set_setting

router = Router(name="admin_plans")

# Kind hub (reply keyboard step) — not the same-kind screen
BACK_USERS_KIND = "adm:plans:aud:users"
BACK_RESELLERS_KIND = "adm:plans:aud:resellers"
# Re-open a specific kind list/detail screen
BACK_USERS_FIXED_LIST = "adm:plans:kind:users:fixed"

class AdminPlansStates(StatesGroup):
    edit_value = State()
    res_plan_name = State()
    res_plan_price = State()
    res_plan_rate_gb = State()
    res_plan_addon_amount = State()
    res_plan_link = State()
    res_plan_role = State()
    res_plan_color = State()
    res_plan_edit_field = State()
    trial_name = State()
    trial_days = State()
    trial_gb = State()
    wholesale_tier_min = State()
    wholesale_tier_pct = State()

async def _plans_reply_markup(
    session: AsyncSession,
    state: FSMContext,
) -> ReplyKeyboardMarkup:
    data = await state.get_data()
    aud = data.get("_adm_plans_aud")
    ui = await get_all_settings(session)
    if aud in {"users", "resellers"}:
        return kb.admin_plans_kind_reply_keyboard(aud, ui)
    return kb.admin_plans_audience_reply_keyboard(ui)

async def sync_plans_reply_keyboard(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    *,
    audience: str | None = None,
    add_type: bool = False,
) -> None:
    """Keep nav aligned after inline «back» on plans screens.

    Wave F: under ``nav_mode=inline`` re-present the hub (no ``⬇️`` chrome).
    """
    from app.bot.nav_inline import (
        admin_plans_add_type_hub_keyboard,
        admin_plans_audience_hub_keyboard,
        admin_plans_kind_hub_keyboard,
        present_inline_only,
    )
    from app.bot.menu_nav import build_main_reply_keyboard

    if add_type and audience in {"users", "resellers"}:
        await state.update_data(_adm_plans_aud=audience, _adm_plans_kind=None)
        await nav.set_nav_level(state, nav.NAV_ADMIN_PLANS_ADD_TYPE, push=False)
        level = nav.NAV_ADMIN_PLANS_ADD_TYPE
    elif audience in {"users", "resellers"}:
        await state.update_data(_adm_plans_aud=audience, _adm_plans_kind=None)
        await nav.set_nav_level(state, nav.NAV_ADMIN_PLANS_KIND, push=False)
        level = nav.NAV_ADMIN_PLANS_KIND
    else:
        await state.update_data(_adm_plans_aud=None, _adm_plans_kind=None)
        await nav.set_nav_level(state, nav.NAV_ADMIN_PLANS_AUDIENCE, push=False)
        level = nav.NAV_ADMIN_PLANS_AUDIENCE
    ui = await get_all_settings(session)
    aud = str((await state.get_data()).get("_adm_plans_aud") or "users")
    if level == nav.NAV_ADMIN_PLANS_ADD_TYPE:
        inline = admin_plans_add_type_hub_keyboard(aud, ui)
        body = "➕ <b>افزودن پلن</b>\nنوع پلن را انتخاب کنید:"
    elif level == nav.NAV_ADMIN_PLANS_KIND:
        inline = admin_plans_kind_hub_keyboard(aud, ui)
        body = (
            "👥 <b>پلن‌های کاربران</b>\n"
            if aud == "users"
            else "🤝 <b>پلن‌های نمایندگان</b>\n"
        ) + "افزودن/کاتالوگ از دکمه‌های زیر."
    else:
        inline = admin_plans_audience_hub_keyboard(ui)
        body = "💎 <b>پلن‌ها</b>\nمخاطب یا ابزار کاتالوگ را انتخاب کنید."
    await present_inline_only(message, text=body, inline=inline)
    main_kb, _, _ = await build_main_reply_keyboard(session, db_user, ui=ui)
    await message.answer("از منوی پایین یا دکمه‌های بالا ادامه دهید.", reply_markup=main_kb)
    return

async def _answer_plans_cancel(message: Message, state: FSMContext, session: AsyncSession) -> None:
    from app.bot.menu_nav import build_main_reply_keyboard

    await state.set_state(None)
    ui = await get_all_settings(session)
    markup = await _plans_reply_markup(session, state)
    # Prefer stable main when we can resolve user from message.from_user later;
    # keep kind/audience reply only for classic. For inline, heal via show path below.
    from app.db.models import BotUser
    from sqlalchemy import select

    tg_id = getattr(getattr(message, "from_user", None), "id", None)
    db_user = None
    if tg_id is not None:
        db_user = (
            await session.execute(
                select(BotUser).where(BotUser.telegram_id == int(tg_id))
            )
        ).scalar_one_or_none()
    if db_user is not None:
        main_kb, _, _ = await build_main_reply_keyboard(session, db_user, ui=ui)
        await message.answer("لغو شد.", reply_markup=main_kb)
        return
    await message.answer("لغو شد.", reply_markup=markup)
    return

async def _answer_plans_saved(message: Message, state: FSMContext, session: AsyncSession, text: str) -> None:
    from app.bot.menu_nav import build_main_reply_keyboard
    from app.db.models import BotUser
    from sqlalchemy import select

    ui = await get_all_settings(session)
    tg_id = getattr(getattr(message, "from_user", None), "id", None)
    db_user = None
    if tg_id is not None:
        db_user = (
            await session.execute(
                select(BotUser).where(BotUser.telegram_id == int(tg_id))
            )
        ).scalar_one_or_none()
    if db_user is not None:
        main_kb, _, _ = await build_main_reply_keyboard(session, db_user, ui=ui)
        await message.answer(text, reply_markup=main_kb)
        return
    markup = await _plans_reply_markup(session, state)
    await message.answer(text, reply_markup=markup)

def _kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _kind_btn_style(ui: dict | None, kind: str) -> str | None:
    from app.services.button_styles import plan_kind_style_id

    sid = plan_kind_style_id(kind)
    if not sid:
        return None
    return kb._style(ui, sid, fallback="primary")

def _ib(text: str, cb: str, style: str | None = None) -> InlineKeyboardButton:
    return kb._ikb(text, callback_data=cb, style=style)

def _back_row(label: str, cb: str, style: str | None = None) -> list[InlineKeyboardButton]:
    return [_ib(label, cb, style=style)]

async def _persist(session: AsyncSession) -> None:
    await session.commit()

async def _ensure_trial(session: AsyncSession) -> Plan:
    trial = (
        await session.execute(select(Plan).where(Plan.is_trial.is_(True)))
    ).scalar_one_or_none()
    if trial:
        return trial
    trial = Plan(
        name="تست رایگان",
        price=0,
        duration_days=1,
        is_trial=True,
        is_active=False,
        description="پلن تست رایگان",
    )
    session.add(trial)
    await session.flush()
    await session.refresh(trial)
    await _persist(session)
    return trial

async def send_audience_hub(
    message: Message,
    session: AsyncSession,
    *,
    edit: bool = False,
) -> None:
    text = "💎 <b>پلن‌ها</b>\nمخاطب را از کیبورد پایین انتخاب کنید:"
    if edit and message.text:
        try:
            await message.edit_text(text, reply_markup=None)
            return
        except Exception:
            pass
    await message.answer(text)

async def send_kind_hub(
    message: Message,
    session: AsyncSession,
    audience: str,
    *,
    edit: bool = False,
) -> None:
    title = "کاربران" if audience == "users" else "نمایندگان"
    text = f"💎 <b>پلن‌های {title}</b>\nنوع پلن را از کیبورد پایین انتخاب کنید:"
    if edit:
        try:
            await message.edit_text(text, reply_markup=None)
            return
        except Exception:
            pass
    await message.answer(text)

async def send_users_plans_overview(message: Message, session: AsyncSession) -> None:
    """User plans list — mirrors web /plans user table; plans inline, add on reply keyboard."""
    ui = await get_all_settings(session)
    result = await session.execute(select(Plan).order_by(Plan.sort_order, Plan.id))
    plans = list(result.scalars().all())
    fixed = [p for p in plans if not p.is_trial]
    text = (
        "👥 <b>پلن‌های کاربران</b>\n"
        "━━━━━━━━━━━━\n"
        f"پلن‌های ثابت: <b>{len(fixed)}</b>\n"
        "روی هر پلن بزنید تا ویرایش/حذف — «افزودن پلن» از کیبورد پایین."
    )
    await message.answer(
        text,
        reply_markup=kb.admin_users_plans_overview_keyboard(plans, ui),
    )

async def send_resellers_plans_overview(message: Message, session: AsyncSession) -> None:
    """Reseller subscription plans — all rows inline like web /plans."""
    from app.services.pg_admin_subscription import is_addon_plan, is_subscription_plan

    all_plans = await list_reseller_plans(session)
    fixed = [
        p
        for p in all_plans
        if reseller_plan_mode_of(p) == "fixed" and is_subscription_plan(p)
    ]
    payg = [
        p
        for p in all_plans
        if reseller_plan_mode_of(p) == "payg" and is_subscription_plan(p)
    ]
    addons = [p for p in all_plans if is_addon_plan(p)]
    text = (
        "🤝 <b>پلن‌های نمایندگان</b>\n"
        "━━━━━━━━━━━━\n"
        f"اشتراک ثابت: <b>{len(fixed)}</b> · PAYG: <b>{len(payg)}</b> · بسته: <b>{len(addons)}</b>\n"
        "روی هر پلن بزنید — «افزودن پلن» از کیبورد پایین."
    )
    await message.answer(
        text,
        reply_markup=kb.admin_resellers_plans_overview_keyboard(
            fixed, payg, ui=await get_all_settings(session), addon_plans=addons
        ),
    )

async def send_add_plan_type_picker(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    audience: str,
) -> None:
    """Step after «افزودن پلن» — type picker like web modal kind step."""
    title = "کاربران" if audience == "users" else "نمایندگان"
    ui = await get_all_settings(session)
    await state.update_data(_adm_plans_aud=audience, _adm_plans_kind=None)
    await nav.set_nav_level(state, nav.NAV_ADMIN_PLANS_ADD_TYPE, push=True)
    markup = kb.admin_plans_add_type_reply_keyboard(audience, ui)
    await message.answer(
        f"➕ <b>افزودن پلن — {title}</b>\n"
        "نوع پلن را از کیبورد پایین یا دکمه‌های زیر انتخاب کنید:",
        reply_markup=markup,
    )
    await message.answer(
        "نوع پلن:",
        reply_markup=kb.admin_plans_add_type_keyboard(audience, ui),
    )

async def send_users_fixed_list(message: Message, session: AsyncSession) -> None:
    from app.bot.handlers.admin import _plan_line

    ui = await get_all_settings(session)
    result = await session.execute(select(Plan).order_by(Plan.sort_order, Plan.id))
    plans = list(result.scalars().all())
    fixed = [p for p in plans if not p.is_trial]
    if not fixed:
        body = "هنوز پلن ثابتی ثبت نشده است."
    else:
        body = "\n\n".join(_plan_line(p) for p in fixed[:20])
    await message.answer(
        f"📦 <b>پلن‌های ثابت</b>\n\n{body}",
        reply_markup=kb.admin_plans_list_keyboard(
            plans,
            ui,
            back_callback=BACK_USERS_KIND,
            kind="fixed",
        ),
    )

async def _build_custom_screen(session: AsyncSession) -> tuple[str, InlineKeyboardMarkup]:
    ui = await get_all_settings(session)
    st = _kind_btn_style(ui, "custom")
    rows: list[list[InlineKeyboardButton]] = [
        [
            _ib(
                f"{'✅' if on(ui.get('custom_plan_enabled')) else '⬜️'} فعال در فروشگاه",
                "adm:plans:tog:custom_plan_enabled",
                st,
            )
        ],
    ]
    for key, label, kind in CUSTOM_PRICE:
        if kind == "toggle":
            mark = "✅" if on(ui.get(key)) else "⬜️"
            rows.append([_ib(f"{mark} {label}", f"adm:plans:tog:{key}", st)])
        else:
            rows.append([_ib(label, f"adm:plans:edit:{key}", st)])
    rows.append([_ib("🔗 اتصال پاسارگارد", "adm:plans:custom:pg", st)])
    rows.append(_back_row("⬅️ پلن‌های کاربران", BACK_USERS_KIND, kb._style(ui, "back")))
    tpl = (ui.get("custom_plan_template_id") or "").strip()
    groups = (ui.get("custom_plan_group_ids") or "").strip()
    link = f"تمپلیت #{tpl}" if tpl else (f"گروه {groups}" if groups else "بدون اتصال")
    text = (
        "✨ <b>پلن دلخواه</b>\n"
        f"فروش: {'فعال' if on(ui.get('custom_plan_enabled')) else 'خاموش'}\n"
        f"پاسارگارد: {link}"
    )
    return text, _kb(rows)

async def send_users_custom(message: Message, session: AsyncSession) -> None:
    text, markup = await _build_custom_screen(session)
    await message.answer(text, reply_markup=markup)

async def _build_trial_screen(session: AsyncSession) -> tuple[str, InlineKeyboardMarkup]:
    trial = (
        await session.execute(select(Plan).where(Plan.is_trial.is_(True)))
    ).scalar_one_or_none()
    ui = await get_all_settings(session)
    if trial:
        gb = (
            f"{trial.data_limit_gb:g} گیگ"
            if trial.data_limit_gb is not None
            else "نامحدود"
        )
        if trial.pg_template_id:
            link = f"تمپلیت #{trial.pg_template_id}"
        elif trial.pg_group_ids:
            link = f"گروه {trial.pg_group_ids}"
        else:
            link = "بدون اتصال"
        body = (
            f"نام: <b>{trial.name}</b>\n"
            f"مدت: {trial.duration_days} روز · حجم: {gb}\n"
            f"اتصال: {link}"
        )
    else:
        body = "هنوز ساخته نشده."
    st = _kind_btn_style(ui, "trial")
    rows = [
        [
            _ib(
                f"{'✅' if on(ui.get('trial_enabled')) else '⬜️'} نمایش در فروشگاه",
                "adm:plans:tog:trial_enabled",
                st,
            )
        ],
        [_ib("نام", "adm:plans:trial:name", st)],
        [_ib("مدت (روز)", "adm:plans:trial:days", st)],
        [_ib("حجم (گیگ)", "adm:plans:trial:gb", st)],
        [_ib("تمپلیت پاسارگارد", "adm:plans:trial:tpl", st)],
        [_ib("گروه پاسارگارد", "adm:plans:trial:grp", st)],
        _back_row("⬅️ پلن‌های کاربران", BACK_USERS_KIND, kb._style(ui, "back")),
    ]
    return f"🧪 <b>پلن تست</b>\n\n{body}", _kb(rows)

async def send_users_trial(message: Message, session: AsyncSession) -> None:
    text, markup = await _build_trial_screen(session)
    await message.answer(text, reply_markup=markup)

async def _build_wholesale_screen(session: AsyncSession) -> tuple[str, InlineKeyboardMarkup]:
    ui = await get_all_settings(session)
    st = _kind_btn_style(ui, "wholesale")
    tiers = parse_wholesale_tiers(ui.get("wholesale_tiers"))
    tier_lines = (
        "\n".join(f"از {t['min']} عدد → {t['percent']}٪" for t in tiers)
        if tiers
        else "پله‌ای تعریف نشده"
    )
    rows: list[list[InlineKeyboardButton]] = [
        [
            _ib(
                f"{'✅' if on(ui.get('wholesale_enabled')) else '⬜️'} فعال در فروشگاه",
                "adm:plans:tog:wholesale_enabled",
                st,
            )
        ],
        [_ib("حداقل تعداد", "adm:plans:edit:wholesale_min_qty", st)],
        [_ib("حداکثر تعداد", "adm:plans:edit:wholesale_max_qty", st)],
        [_ib("متن دکمه ربات", "adm:plans:edit:btn_wholesale", st)],
        [_ib("➕ پله تخفیف", "adm:plans:wholesale:add_tier", st)],
    ]
    for i, t in enumerate(tiers[:6]):
        rows.append(
            [_ib(f"🗑 پله {t['min']}→{t['percent']}٪", f"adm:plans:wholesale:del:{i}", st)]
        )
    rows.append(_back_row("⬅️ پلن‌های کاربران", BACK_USERS_KIND, kb._style(ui, "back")))
    text = (
        "📦 <b>فروش عمده</b>\n"
        f"{wholesale_description(ui)}\n\n"
        f"<b>پله‌ها:</b>\n{tier_lines}"
    )
    return text, _kb(rows)

async def send_users_wholesale(message: Message, session: AsyncSession) -> None:
    text, markup = await _build_wholesale_screen(session)
    await message.answer(text, reply_markup=markup)

async def send_reseller_plans_list(
    message: Message,
    session: AsyncSession,
    kind: str,
) -> None:
    from app.services.pg_admin_subscription import is_subscription_plan

    all_plans = await list_reseller_plans(session)
    if kind in {"addon_volume", "addon_users"}:
        plans = [p for p in all_plans if str(getattr(p, "plan_kind", "")) == kind]
        label = "بسته حجم" if kind == "addon_volume" else "بسته کاربر"
        if not plans:
            body = "هنوز بسته‌ای در این دسته نیست."
        else:
            cards = [
                format_reseller_plan_apply_detail(p, currency=get_settings().currency)
                for p in plans[:10]
            ]
            body = "\n\n".join(cards)
        await message.answer(
            f"🎁 <b>پلن‌های نماینده — {label}</b>\n"
            "بدون گروه/نقش/نام‌گذاری سرویس.\n\n"
            f"{body}",
            reply_markup=kb.admin_reseller_plans_list_keyboard(
                plans,
                await get_all_settings(session),
                mode=kind,
                back_callback=BACK_RESELLERS_KIND,
                add_callback=f"adm:plans:add:resellers:{kind}",
            ),
        )
        return

    plans = [
        p
        for p in all_plans
        if reseller_plan_mode_of(p) == kind and is_subscription_plan(p)
    ]
    label = "PAYG" if kind == "payg" else "ثابت"
    if not plans:
        body = "هنوز پلنی در این دسته نیست."
    else:
        cards = [
            format_reseller_plan_apply_detail(p, currency=get_settings().currency)
            for p in plans[:10]
        ]
        body = "\n\n".join(cards)
    await message.answer(
        f"🤝 <b>پلن‌های نماینده — {label}</b>\n\n{body}",
        reply_markup=kb.admin_reseller_plans_list_keyboard(
            plans,
            await get_all_settings(session),
            mode=kind,
            back_callback=BACK_RESELLERS_KIND,
            add_callback=f"adm:resplan:add:{kind}",
        ),
    )

async def open_kind_screen(
    message: Message,
    session: AsyncSession,
    audience: str,
    kind: str,
) -> None:
    if audience == "users" and kind == "fixed":
        await send_users_fixed_list(message, session)
    elif audience == "users" and kind == "custom":
        await send_users_custom(message, session)
    elif audience == "users" and kind == "trial":
        await send_users_trial(message, session)
    elif audience == "users" and kind == "wholesale":
        await send_users_wholesale(message, session)
    elif audience == "resellers" and kind in {"fixed", "payg", "addon_volume", "addon_users"}:
        await send_reseller_plans_list(message, session, kind)
    else:
        await message.answer("نوع پلن نامعتبر است.")

async def open_add_kind_action(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    audience: str,
    kind: str,
) -> None:
    """Route add-type selection — mirrors web modal submit for each kind."""
    from app.bot.handlers.admin import AdminStates

    await state.update_data(_adm_plans_aud=audience, _adm_plans_kind=kind)
    await nav.set_nav_level(state, nav.NAV_ADMIN_PLANS_KIND, push=False)
    if audience == "users" and kind == "fixed":
        await state.set_state(AdminStates.add_plan_name)
        # Keep cancel reply KB for the whole FSM — do NOT restore list KB mid-wizard
        await message.answer(
            "➕ <b>پلن ثابت جدید</b>\nنام پلن را بفرستید:",
            reply_markup=kb.cancel_reply(),
        )
        return
    if audience == "resellers" and kind in {"fixed", "payg"}:
        label = "اشتراک PAYG" if kind == "payg" else "اشتراک ثابت"
        await state.set_state(AdminPlansStates.res_plan_name)
        await state.update_data(res_plan_mode=kind, res_plan_kind="subscription")
        await message.answer(
            f"➕ <b>پلن {label}</b>\nنام پلن نمایندگی:",
            reply_markup=kb.cancel_reply(),
        )
        return
    if audience == "resellers" and kind in {"addon_volume", "addon_users"}:
        label = "بسته حجم" if kind == "addon_volume" else "بسته کاربر"
        await state.set_state(AdminPlansStates.res_plan_name)
        await state.update_data(res_plan_mode="fixed", res_plan_kind=kind)
        await message.answer(
            f"➕ <b>{label}</b>\n"
            "بدون گروه/نقش/نام‌گذاری سرویس — فقط برای نمایندگان با اشتراک فعال.\n"
            "نام بسته:",
            reply_markup=kb.cancel_reply(),
        )
        return
    # Settings-based kinds — open configure screen (same as web «تنظیم»)
    from app.bot.nav_inline import admin_plans_kind_hub_keyboard, present_inline_only
    from app.bot.menu_nav import build_main_reply_keyboard

    ui = await get_all_settings(session)
    await present_inline_only(
        message,
        text=(
            "👥 <b>پلن‌های کاربران</b>\n"
            if audience == "users"
            else "🤝 <b>پلن‌های نمایندگان</b>\n"
        )
        + "افزودن/کاتالوگ از دکمه‌های زیر.",
        inline=admin_plans_kind_hub_keyboard(audience, ui),
    )
    main_kb, _, _ = await build_main_reply_keyboard(session, db_user, ui=ui)
    await message.answer(
        "از منوی پایین یا دکمه‌های بالا ادامه دهید.",
        reply_markup=main_kb,
    )
    await open_kind_screen(message, session, audience, kind)

async def _rerender_plans_screen(
    callback: CallbackQuery,
    session: AsyncSession,
    aud: str,
    kind: str,
) -> None:
    if not callback.message:
        return
    if aud == "users" and kind == "custom":
        text, markup = await _build_custom_screen(session)
        await callback.message.edit_text(text, reply_markup=markup)
    elif aud == "users" and kind == "trial":
        text, markup = await _build_trial_screen(session)
        await callback.message.edit_text(text, reply_markup=markup)
    elif aud == "users" and kind == "wholesale":
        text, markup = await _build_wholesale_screen(session)
        await callback.message.edit_text(text, reply_markup=markup)
    elif aud == "users" and kind == "fixed":
        from app.bot.handlers.admin import _render_plans_list

        await _render_plans_list(callback, session)
    elif aud == "resellers" and kind in {"fixed", "payg", "addon_volume", "addon_users"}:
        from app.services.pg_admin_subscription import is_subscription_plan

        all_plans = await list_reseller_plans(session)
        if kind in {"addon_volume", "addon_users"}:
            plans = [p for p in all_plans if str(getattr(p, "plan_kind", "")) == kind]
            label = "بسته حجم" if kind == "addon_volume" else "بسته کاربر"
            add_cb = f"adm:plans:add:resellers:{kind}"
        else:
            plans = [
                p
                for p in all_plans
                if reseller_plan_mode_of(p) == kind and is_subscription_plan(p)
            ]
            label = "PAYG" if kind == "payg" else "ثابت"
            add_cb = f"adm:resplan:add:{kind}"
        body = (
            "هنوز پلنی در این دسته نیست."
            if not plans
            else "\n\n".join(
                format_reseller_plan_apply_detail(p, currency=get_settings().currency)
                for p in plans[:10]
            )
        )
        title_prefix = "🎁" if kind in {"addon_volume", "addon_users"} else "🤝"
        await callback.message.edit_text(
            f"{title_prefix} <b>پلن‌های نماینده — {label}</b>\n\n{body}",
            reply_markup=kb.admin_reseller_plans_list_keyboard(
                plans,
                await get_all_settings(session),
                mode=kind,
                back_callback=BACK_RESELLERS_KIND,
                add_callback=add_cb,
            ),
        )

@router.callback_query(F.data == "adm:plans:noop")
@require_bot_owner_handler
async def plans_noop(callback: CallbackQuery, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await callback.answer("از کیبورد «افزودن پلن» استفاده کنید", show_alert=True)

@router.callback_query(F.data.startswith("adm:plans:add:"))
@require_bot_owner_handler
async def plans_add_kind_cb(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) < 5:
        await callback.answer("نامعتبر", show_alert=True)
        return
    aud, kind = parts[3], parts[4]
    if aud not in {"users", "resellers"}:
        await callback.answer("نامعتبر", show_alert=True)
        return
    await callback.answer()
    if callback.message:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await open_add_kind_action(callback.message, session, db_user, state, aud, kind)

@router.callback_query(F.data == "adm:plans")
@require_bot_owner_handler
async def plans_hub(callback: CallbackQuery, session: AsyncSession, db_user: BotUser, state: FSMContext):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await state.clear()
    await callback.answer()
    if callback.message:
        try:
            await callback.message.edit_text(
                "💎 <b>پلن‌ها</b>\n"
                "«پلن‌های کاربران» یا «پلن‌های نمایندگان» را از کیبورد پایین بزنید.",
                reply_markup=None,
            )
        except Exception:
            pass
        await sync_plans_reply_keyboard(callback.message, session, db_user, state)

@router.callback_query(F.data.startswith("adm:plans:aud:"))
@require_bot_owner_handler
async def plans_aud_inline_back(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    aud = callback.data.rsplit(":", 1)[-1]
    if aud not in {"users", "resellers"}:
        await callback.answer("نامعتبر", show_alert=True)
        return
    await state.update_data(_adm_plans_aud=aud, _adm_plans_kind=None)
    await callback.answer()
    title = "کاربران" if aud == "users" else "نمایندگان"
    if callback.message:
        try:
            await callback.message.edit_text(f"💎 <b>پلن‌های {title}</b>", reply_markup=None)
        except Exception:
            pass
        await sync_plans_reply_keyboard(callback.message, session, db_user, state, audience=aud)
        if aud == "users":
            await send_users_plans_overview(callback.message, session)
        else:
            await send_resellers_plans_overview(callback.message, session)

@router.callback_query(F.data.startswith("adm:plans:kind:"))
@require_bot_owner_handler
async def plans_kind_cb(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) < 5:
        await callback.answer("نامعتبر", show_alert=True)
        return
    aud, kind = parts[3], parts[4]
    await state.update_data(_adm_plans_aud=aud, _adm_plans_kind=kind)
    await callback.answer()
    if callback.message:
        await callback.message.edit_text("⏳")
        await open_kind_screen(callback.message, session, aud, kind)
        try:
            await callback.message.delete()
        except Exception:
            pass

@router.callback_query(F.data.startswith("adm:plans:tog:"))
@require_bot_owner_handler
async def plans_toggle(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    key = callback.data.split("adm:plans:tog:", 1)[-1]
    cur = await get_setting(session, key)
    new_val = "0" if on(cur) else "1"
    await set_setting(session, key, new_val)
    if key == "trial_enabled":
        trial = await _ensure_trial(session)
        trial.is_active = new_val == "1"
        await _persist(session)
    await callback.answer("ذخیره شد")
    data = await state.get_data()
    aud = data.get("_adm_plans_aud") or "users"
    kind = data.get("_adm_plans_kind") or (
        "custom" if key.startswith("custom_plan") else "trial" if key == "trial_enabled" else "wholesale"
    )
    if key.startswith("custom_plan"):
        kind = "custom"
    if key.startswith("wholesale"):
        kind = "wholesale"
    await _rerender_plans_screen(callback, session, aud, kind)

@router.callback_query(F.data.startswith("adm:plans:edit:"))
@require_bot_owner_handler
async def plans_edit_ask(
    callback: CallbackQuery,
    session: AsyncSession,
    state: FSMContext,
    db_user: BotUser,
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    key = callback.data.split("adm:plans:edit:", 1)[-1]
    cur = await get_setting(session, key)
    await callback.answer()
    await state.set_state(AdminPlansStates.edit_value)
    await state.update_data(plans_edit_key=key, _adm_plans_aud="users")
    kind = "wholesale" if key.startswith("wholesale") or key == "btn_wholesale" else "custom"
    await state.update_data(_adm_plans_kind=kind)
    if callback.message:
        await callback.message.answer(
            f"مقدار جدید برای <b>{key}</b>\nفعلی: <code>{html.escape((cur or '')[:200])}</code>",
            reply_markup=kb.cancel_reply(),
        )

@router.message(AdminPlansStates.edit_value)
@require_bot_owner_handler
async def plans_edit_save(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
):
    if not _is_admin(db_user):
        await state.clear()
        return
    if kb.is_cancel_text(message.text):
        await state.set_state(None)
        await _answer_plans_cancel(message, state, session)
        return
    data = await state.get_data()
    key = data.get("plans_edit_key")
    text = (message.text or "").strip()
    if not key:
        await state.clear()
        return
    if key in {
        "wholesale_min_qty",
        "wholesale_max_qty",
        "custom_plan_price_per_gb",
        "custom_plan_price_per_day",
        "custom_plan_min_gb",
        "custom_plan_max_gb",
        "custom_plan_min_days",
        "custom_plan_max_days",
    }:
        try:
            float(text.replace(",", "").replace("٬", ""))
        except ValueError:
            await message.answer("عدد معتبر بفرستید.", reply_markup=kb.cancel_reply())
            return
    await set_setting(session, key, text.replace(",", "").replace("٬", ""))
    await state.set_state(None)
    aud = data.get("_adm_plans_aud") or "users"
    kind = data.get("_adm_plans_kind") or "custom"
    await _answer_plans_saved(message, state, session, "ذخیره شد ✅")
    bubble = await message.answer("⏳")
    await open_kind_screen(bubble, session, aud, kind)

@router.callback_query(F.data == "adm:plans:wholesale:add_tier")
@require_bot_owner_handler
async def wholesale_add_tier_ask(callback: CallbackQuery, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await callback.answer()
    await state.set_state(AdminPlansStates.wholesale_tier_min)
    await state.update_data(_adm_plans_aud="users", _adm_plans_kind="wholesale")
    if callback.message:
        await callback.message.answer(
            "حداقل تعداد برای پله جدید:",
            reply_markup=kb.cancel_reply(),
        )

@router.message(AdminPlansStates.wholesale_tier_min)
@require_bot_owner_handler
async def wholesale_tier_min_entered(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await state.clear()
        return
    if kb.is_cancel_text(message.text):
        await state.set_state(None)
        await _answer_plans_cancel(message, state, session)
        return
    try:
        mn = parse_bot_int(message.text)
    except ValueError:
        await message.answer("عدد معتبر بفرستید.")
        return
    await state.update_data(tier_min=mn)
    await state.set_state(AdminPlansStates.wholesale_tier_pct)
    await message.answer("درصد تخفیف (۰–۱۰۰):", reply_markup=kb.cancel_reply())

@router.message(AdminPlansStates.wholesale_tier_pct)
@require_bot_owner_handler
async def wholesale_tier_pct_entered(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await state.clear()
        return
    if kb.is_cancel_text(message.text):
        await state.set_state(None)
        await _answer_plans_cancel(message, state, session)
        return
    try:
        pct = int((message.text or "").strip())
        pct = max(0, min(100, pct))
    except ValueError:
        await message.answer("عدد معتبر بفرستید.")
        return
    data = await state.get_data()
    mn = int(data.get("tier_min") or 1)
    ui = await get_all_settings(session)
    tiers = parse_wholesale_tiers(ui.get("wholesale_tiers"))
    tiers.append({"min": mn, "percent": pct})
    tiers.sort(key=lambda t: t["min"])
    await set_setting(session, "wholesale_tiers", json.dumps(tiers, ensure_ascii=False))
    await state.set_state(None)
    await _answer_plans_saved(message, state, session, "پله ذخیره شد ✅")
    bubble = await message.answer("⏳")
    await send_users_wholesale(bubble, session)

@router.callback_query(F.data.startswith("adm:plans:wholesale:del:"))
@require_bot_owner_handler
async def wholesale_del_tier(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser, state: FSMContext
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    idx = int(callback.data.rsplit(":", 1)[-1])
    ui = await get_all_settings(session)
    tiers = parse_wholesale_tiers(ui.get("wholesale_tiers"))
    if 0 <= idx < len(tiers):
        tiers.pop(idx)
    await set_setting(session, "wholesale_tiers", json.dumps(tiers, ensure_ascii=False))
    await state.update_data(_adm_plans_aud="users", _adm_plans_kind="wholesale")
    await callback.answer("حذف شد")
    await _rerender_plans_screen(callback, session, "users", "wholesale")

# —— Trial (plans context) ——

@router.callback_query(F.data == "adm:plans:trial:name")
@require_bot_owner_handler
async def trial_ask_name(callback: CallbackQuery, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await callback.answer()
    await state.set_state(AdminPlansStates.trial_name)
    await state.update_data(_adm_plans_aud="users", _adm_plans_kind="trial")
    if callback.message:
        await callback.message.answer("نام پلن تست:", reply_markup=kb.cancel_reply())

@router.message(AdminPlansStates.trial_name)
@require_bot_owner_handler
async def trial_save_name(message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user) or kb.is_cancel_text(message.text):
        await state.set_state(None)
        await _answer_plans_cancel(message, state, session)
        return
    trial = await _ensure_trial(session)
    trial.name = (message.text or "").strip()[:128]
    await _persist(session)
    await state.set_state(None)
    await _answer_plans_saved(message, state, session, "ذخیره شد ✅")
    bubble = await message.answer("⏳")
    await send_users_trial(bubble, session)

@router.callback_query(F.data == "adm:plans:trial:days")
@require_bot_owner_handler
async def trial_ask_days(callback: CallbackQuery, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await callback.answer()
    await state.set_state(AdminPlansStates.trial_days)
    await state.update_data(_adm_plans_aud="users", _adm_plans_kind="trial")
    if callback.message:
        await callback.message.answer("مدت به روز:", reply_markup=kb.cancel_reply())

@router.message(AdminPlansStates.trial_days)
@require_bot_owner_handler
async def trial_save_days(message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user) or kb.is_cancel_text(message.text):
        await state.set_state(None)
        await _answer_plans_cancel(message, state, session)
        return
    try:
        days = max(1, parse_bot_int(message.text))
    except ValueError:
        await message.answer("عدد معتبر بفرستید.")
        return
    trial = await _ensure_trial(session)
    trial.duration_days = days
    await _persist(session)
    await state.set_state(None)
    await _answer_plans_saved(message, state, session, "ذخیره شد ✅")
    bubble = await message.answer("⏳")
    await send_users_trial(bubble, session)

@router.callback_query(F.data == "adm:plans:trial:gb")
@require_bot_owner_handler
async def trial_ask_gb(callback: CallbackQuery, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await callback.answer()
    await state.set_state(AdminPlansStates.trial_gb)
    await state.update_data(_adm_plans_aud="users", _adm_plans_kind="trial")
    if callback.message:
        await callback.message.answer("حجم گیگ (۰ = نامحدود):", reply_markup=kb.cancel_reply())

@router.message(AdminPlansStates.trial_gb)
@require_bot_owner_handler
async def trial_save_gb(message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user) or kb.is_cancel_text(message.text):
        await state.set_state(None)
        await _answer_plans_cancel(message, state, session)
        return
    try:
        gb = parse_bot_float(message.text)
    except ValueError:
        await message.answer("عدد معتبر بفرستید.")
        return
    trial = await _ensure_trial(session)
    trial.data_limit_gb = None if gb <= 0 else gb
    await _persist(session)
    await state.set_state(None)
    await _answer_plans_saved(message, state, session, "ذخیره شد ✅")
    bubble = await message.answer("⏳")
    await send_users_trial(bubble, session)

@router.callback_query(F.data == "adm:plans:trial:tpl")
@require_bot_owner_handler
async def trial_pick_tpl(callback: CallbackQuery, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    try:
        templates = await get_pg().get_user_templates_simple()
    except Exception:
        templates = []
    rows: list[list[InlineKeyboardButton]] = []
    for t in templates[:20]:
        tid = t.get("id")
        if tid is None:
            continue
        rows.append(
            [
                InlineKeyboardButton(
                    text=str(t.get("name") or tid)[:40],
                    callback_data=f"adm:plans:trial:settpl:{tid}",
                )
            ]
        )
    if not rows:
        rows.append(
            [InlineKeyboardButton(text="تمپلیتی نیست", callback_data=BACK_USERS_KIND)]
        )
    rows.append(_back_row("⬅️ بازگشت", BACK_USERS_KIND))
    await callback.answer()
    if callback.message:
        await callback.message.edit_text("تمپلیت را انتخاب کنید:", reply_markup=_kb(rows))

@router.callback_query(F.data.startswith("adm:plans:trial:settpl:"))
@require_bot_owner_handler
async def trial_set_tpl(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    tid = int(callback.data.rsplit(":", 1)[-1])
    trial = await _ensure_trial(session)
    trial.pg_template_id = tid
    trial.pg_group_ids = None
    await _persist(session)
    await callback.answer("ذخیره شد")
    bubble = await callback.message.answer("⏳") if callback.message else None
    if bubble:
        await send_users_trial(bubble, session)

@router.callback_query(F.data == "adm:plans:trial:grp")
@require_bot_owner_handler
async def trial_pick_grp(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    trial = await _ensure_trial(session)
    selected: list[int] = []
    if trial.pg_group_ids:
        for part in str(trial.pg_group_ids).split(","):
            if part.strip().isdigit():
                selected.append(int(part.strip()))
    await state.update_data(trial_groups=selected, _adm_plans_kind="trial")
    await callback.answer()
    await _show_trial_groups_plans(callback, state)

async def _show_trial_groups_plans(callback: CallbackQuery, state: FSMContext) -> None:
    selected = [int(x) for x in ((await state.get_data()).get("trial_groups") or [])]
    try:
        groups = await get_pg().get_groups_simple()
    except Exception:
        groups = []
    rows: list[list[InlineKeyboardButton]] = []
    for g in groups[:20]:
        gid = g.get("id")
        if gid is None:
            continue
        gid = int(gid)
        mark = "✅ " if gid in selected else ""
        name = g.get("name") or f"گروه {gid}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark}{name}"[:40],
                    callback_data=f"adm:plans:trial:toggrp:{gid}",
                )
            ]
        )
    if rows:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"تأیید ({len(selected)})",
                    callback_data="adm:plans:trial:grpdone",
                )
            ]
        )
    else:
        rows.append([InlineKeyboardButton(text="گروهی نیست", callback_data=BACK_USERS_KIND)])
    rows.append(_back_row("⬅️ بازگشت", BACK_USERS_KIND))
    if callback.message:
        await callback.message.edit_text("گروه(ها) را انتخاب کنید:", reply_markup=_kb(rows))

@router.callback_query(F.data.startswith("adm:plans:trial:toggrp:"))
@require_bot_owner_handler
async def trial_tog_grp_plans(callback: CallbackQuery, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    gid = int(callback.data.split(":")[-1])
    selected = [int(x) for x in ((await state.get_data()).get("trial_groups") or [])]
    if gid in selected:
        selected = [x for x in selected if x != gid]
    else:
        selected.append(gid)
    await state.update_data(trial_groups=selected)
    await callback.answer()
    await _show_trial_groups_plans(callback, state)

@router.callback_query(F.data == "adm:plans:trial:grpdone")
@require_bot_owner_handler
async def trial_grp_done_plans(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    selected = [int(x) for x in ((await state.get_data()).get("trial_groups") or [])]
    if not selected:
        await callback.answer("حداقل یک گروه", show_alert=True)
        return
    trial = await _ensure_trial(session)
    trial.pg_group_ids = ",".join(str(x) for x in selected)
    trial.pg_template_id = None
    await _persist(session)
    await state.update_data(trial_groups=[])
    await callback.answer("ذخیره شد")
    if callback.message:
        text, markup = await _build_trial_screen(session)
        await callback.message.edit_text(text, reply_markup=markup)

# —— Custom PG (plans context) ——

async def _custom_pg_summary(session: AsyncSession) -> tuple[str, InlineKeyboardMarkup]:
    ui = await get_all_settings(session)
    enabled = on(ui.get("custom_plan_enabled"))
    tpl = (ui.get("custom_plan_template_id") or "").strip()
    groups = (ui.get("custom_plan_group_ids") or "").strip()
    if tpl:
        link = f"تمپلیت #{tpl}"
    elif groups:
        link = f"گروه‌ها: {groups}"
    else:
        link = "⚠️ بدون اتصال"
    text = (
        "🔗 <b>اتصال پلن دلخواه</b>\n\n"
        f"فروش: {'فعال' if enabled else 'خاموش'}\n"
        f"پاسارگارد: {link}"
    )
    rows = [
        [
            InlineKeyboardButton(
                text="خاموش کردن فروش" if enabled else "روشن کردن فروش",
                callback_data="adm:plans:custom:toggle",
            )
        ],
        [InlineKeyboardButton(text="تمپلیت", callback_data="adm:plans:custom:picktpl")],
        [InlineKeyboardButton(text="گروه", callback_data="adm:plans:custom:pickgrp")],
    ]
    if tpl or groups:
        rows.append(
            [InlineKeyboardButton(text="حذف اتصال", callback_data="adm:plans:custom:clearlink")]
        )
    rows.append(_back_row("⬅️ پلن دلخواه", BACK_USERS_KIND))
    return text, _kb(rows)

@router.callback_query(F.data == "adm:plans:custom:pg")
@require_bot_owner_handler
async def custom_pg_hub(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await callback.answer()
    text, markup = await _custom_pg_summary(session)
    if callback.message:
        await callback.message.edit_text(text, reply_markup=markup)

@router.callback_query(F.data == "adm:plans:custom:toggle")
@require_bot_owner_handler
async def custom_pg_toggle(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    cur = await get_setting(session, "custom_plan_enabled")
    await set_setting(session, "custom_plan_enabled", "0" if on(cur) else "1")
    await callback.answer("بروز شد")
    text, markup = await _custom_pg_summary(session)
    if callback.message:
        await callback.message.edit_text(text, reply_markup=markup)

@router.callback_query(F.data == "adm:plans:custom:clearlink")
@require_bot_owner_handler
async def custom_pg_clear(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await set_setting(session, "custom_plan_template_id", "")
    await set_setting(session, "custom_plan_group_ids", "")
    await callback.answer("حذف شد")
    text, markup = await _custom_pg_summary(session)
    if callback.message:
        await callback.message.edit_text(text, reply_markup=markup)

@router.callback_query(F.data == "adm:plans:custom:picktpl")
@require_bot_owner_handler
async def custom_pick_tpl(callback: CallbackQuery, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    try:
        templates = await get_pg().get_user_templates_simple()
    except Exception:
        templates = []
    rows: list[list[InlineKeyboardButton]] = []
    for t in templates[:20]:
        tid = t.get("id")
        if tid is None:
            continue
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"#{tid} — {(t.get('name') or tid)}"[:60],
                    callback_data=f"adm:plans:custom:settpl:{tid}",
                )
            ]
        )
    if not rows:
        rows.append([InlineKeyboardButton(text="تمپلیتی نیست", callback_data="adm:plans:custom:pg")])
    rows.append(_back_row("⬅️ بازگشت", "adm:plans:custom:pg"))
    await callback.answer()
    if callback.message:
        await callback.message.edit_text("تمپلیت:", reply_markup=_kb(rows))

@router.callback_query(F.data.startswith("adm:plans:custom:settpl:"))
@require_bot_owner_handler
async def custom_set_tpl(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    tpl_id = callback.data.rsplit(":", 1)[-1]
    await set_setting(session, "custom_plan_template_id", tpl_id)
    await set_setting(session, "custom_plan_group_ids", "")
    await callback.answer("ذخیره شد")
    text, markup = await _custom_pg_summary(session)
    if callback.message:
        await callback.message.edit_text(text, reply_markup=markup)

@router.callback_query(F.data == "adm:plans:custom:pickgrp")
@require_bot_owner_handler
async def custom_pick_grp(callback: CallbackQuery, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await state.update_data(custom_selected_groups=[])
    await callback.answer()
    await _show_custom_groups_plans(callback, state)

async def _show_custom_groups_plans(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    selected = [int(x) for x in (data.get("custom_selected_groups") or [])]
    try:
        groups = await get_pg().get_groups_simple()
    except Exception:
        groups = []
    rows: list[list[InlineKeyboardButton]] = []
    for g in groups[:25]:
        gid = g.get("id")
        if gid is None:
            continue
        gid = int(gid)
        mark = "✅ " if gid in selected else ""
        name = g.get("name") or f"گروه {gid}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark}#{gid} — {name}"[:60],
                    callback_data=f"adm:plans:custom:toggrp:{gid}",
                )
            ]
        )
    if rows:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"✅ تأیید ({len(selected)})",
                    callback_data="adm:plans:custom:grpdone",
                )
            ]
        )
    else:
        rows.append([InlineKeyboardButton(text="گروهی نیست", callback_data="adm:plans:custom:pg")])
    rows.append(_back_row("⬅️ بازگشت", "adm:plans:custom:pg"))
    if callback.message:
        await callback.message.edit_text(
            f"گروه‌ها:\nانتخاب: {', '.join(str(x) for x in selected) or '—'}",
            reply_markup=_kb(rows),
        )

@router.callback_query(F.data.startswith("adm:plans:custom:toggrp:"))
@require_bot_owner_handler
async def custom_tog_grp(callback: CallbackQuery, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    gid = int(callback.data.rsplit(":", 1)[-1])
    data = await state.get_data()
    selected = [int(x) for x in (data.get("custom_selected_groups") or [])]
    if gid in selected:
        selected = [x for x in selected if x != gid]
    else:
        selected.append(gid)
    await state.update_data(custom_selected_groups=selected)
    await callback.answer()
    await _show_custom_groups_plans(callback, state)

@router.callback_query(F.data == "adm:plans:custom:grpdone")
@require_bot_owner_handler
async def custom_grp_done(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    selected = [int(x) for x in ((await state.get_data()).get("custom_selected_groups") or [])]
    if not selected:
        await callback.answer("حداقل یک گروه", show_alert=True)
        return
    await set_setting(session, "custom_plan_group_ids", ",".join(str(x) for x in selected))
    await set_setting(session, "custom_plan_template_id", "")
    await state.update_data(custom_selected_groups=[])
    await callback.answer("ذخیره شد")
    text, markup = await _custom_pg_summary(session)
    if callback.message:
        await callback.message.edit_text(text, reply_markup=markup)

# —— Reseller subscription plans ——

def _resplan_detail_text(plan: ResellerPlan) -> str:
    return format_reseller_plan_apply_detail(plan, currency=get_settings().currency)

async def _show_resplan_color_picker(
    target: CallbackQuery | Message,
    *,
    session: AsyncSession,
    callback_prefix: str,
    back_callback: str,
) -> None:
    ui = await get_all_settings(session)
    markup = kb.plan_button_style_picker_keyboard(
        callback_prefix=callback_prefix,
        back_callback=back_callback,
        ui=ui,
    )
    text = "🎨 <b>رنگ دکمه این پلن در ربات</b>\n\n«ارث از نوع پلن» همان رنگ بخش «نوع پلن» در تنظیمات رنگبندی است."
    if isinstance(target, CallbackQuery):
        if target.message:
            await safe_edit_text(target.message, text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)

def _resplan_detail_keyboard(plan: ResellerPlan) -> InlineKeyboardMarkup:
    from app.services.pg_admin_subscription import is_addon_plan, is_subscription_plan
    from app.services.reseller_capacity import plan_allows_buy_extra

    mode = reseller_plan_mode_of(plan)
    pid = plan.id
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="✏️ نام", callback_data=f"adm:resplan:edit:name:{pid}")],
        [InlineKeyboardButton(text="✏️ قیمت", callback_data=f"adm:resplan:edit:price:{pid}")],
    ]
    if is_addon_plan(plan):
        kind = str(getattr(plan, "plan_kind", "") or "")
        label = "✏️ مقدار حجم" if kind == "addon_volume" else "✏️ تعداد کاربر"
        field = "addon_gb" if kind == "addon_volume" else "addon_users"
        rows.append(
            [InlineKeyboardButton(text=label, callback_data=f"adm:resplan:edit:{field}:{pid}")]
        )
        rows.extend(
            [
                [InlineKeyboardButton(text="✏️ توضیح", callback_data=f"adm:resplan:edit:desc:{pid}")],
                [
                    InlineKeyboardButton(
                        text="🎨 رنگ دکمه",
                        callback_data=f"adm:resplan:colorpick:{pid}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⏸ خاموش" if plan.is_active else "▶️ روشن",
                        callback_data=f"adm:resplan:toggle:{pid}",
                    )
                ],
                [InlineKeyboardButton(text="🗑 حذف", callback_data=f"adm:resplan:delask:{pid}")],
                _back_row("⬅️ پلن‌های نمایندگان", "adm:plans:aud:resellers"),
            ]
        )
        return _kb(rows)

    if mode == "payg":
        rows.append(
            [InlineKeyboardButton(text="✏️ نرخ / گیگ", callback_data=f"adm:resplan:edit:rate:{pid}")]
        )
    rows.append(
        [InlineKeyboardButton(text="📁 گروه PG", callback_data=f"adm:resplan:edit:grp:{pid}")]
    )
    rows.append(
        [InlineKeyboardButton(text="🎭 نقش پاسارگارد", callback_data=f"adm:resplan:edit:role:{pid}")]
    )
    rows.extend(
        [
            [InlineKeyboardButton(text="✏️ توضیح", callback_data=f"adm:resplan:edit:desc:{pid}")],
            [InlineKeyboardButton(text="🔐 دسترسی‌ها", callback_data=f"adm:resplan:perms:{pid}")],
            [
                InlineKeyboardButton(
                    text="🎨 رنگ دکمه",
                    callback_data=f"adm:resplan:colorpick:{pid}",
                )
            ],
            [
                InlineKeyboardButton(
                    text=("✅ " if plan.create_pg_admin else "⬜️ ") + "ساخت ادمین PG",
                    callback_data=f"adm:resplan:flag:pgadmin:{pid}",
                )
            ],
            [
                InlineKeyboardButton(
                    text=("✅ " if plan.share_pg_panel_url else "⬜️ ") + "ارسال لینک پنل PG",
                    callback_data=f"adm:resplan:flag:sharepg:{pid}",
                )
            ],
        ]
    )
    if is_subscription_plan(plan) and mode != "payg":
        rows.append(
            [
                InlineKeyboardButton(
                    text=("✅ " if plan.allow_buy_extra else "⬜️ ") + "خرید حجم/کاربر اضافه",
                    callback_data=f"adm:resplan:flag:buyextra:{pid}",
                )
            ]
        )
        if plan.allow_buy_extra or plan_allows_buy_extra(plan):
            rows.append(
                [
                    InlineKeyboardButton(
                        text="✏️ قیمت گیگ اضافه",
                        callback_data=f"adm:resplan:edit:extra_gb:{pid}",
                    )
                ]
            )
            rows.append(
                [
                    InlineKeyboardButton(
                        text="✏️ قیمت کاربر اضافه",
                        callback_data=f"adm:resplan:edit:extra_user:{pid}",
                    )
                ]
            )
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text="⏸ خاموش" if plan.is_active else "▶️ روشن",
                    callback_data=f"adm:resplan:toggle:{pid}",
                )
            ],
            [InlineKeyboardButton(text="🗑 حذف", callback_data=f"adm:resplan:delask:{pid}")],
            _back_row("⬅️ پلن‌های نمایندگان", "adm:plans:aud:resellers"),
        ]
    )
    return _kb(rows)

@router.callback_query(F.data.startswith("adm:resplan:view:"))
@require_bot_owner_handler
async def resplan_view(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    plan = await session.get(ResellerPlan, int(callback.data.rsplit(":", 1)[-1]))
    if not plan:
        await callback.answer("یافت نشد", show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            _resplan_detail_text(plan),
            reply_markup=_resplan_detail_keyboard(plan),
        )

@router.callback_query(F.data.startswith("adm:resplan:toggle:"))
@require_bot_owner_handler
async def resplan_toggle(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    plan = await session.get(ResellerPlan, int(callback.data.rsplit(":", 1)[-1]))
    if not plan:
        await callback.answer("یافت نشد", show_alert=True)
        return
    plan.is_active = not plan.is_active
    await _persist(session)
    await callback.answer("بروز شد")
    if callback.message:
        await callback.message.edit_text(
            _resplan_detail_text(plan),
            reply_markup=_resplan_detail_keyboard(plan),
        )

@router.callback_query(F.data.regexp(r"^adm:resplan:flag:(pgadmin|sharepg|buyextra):\d+$"))
@require_bot_owner_handler
async def resplan_flag_toggle(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    from app.services.pg_admin_subscription import is_addon_plan, is_subscription_plan

    parts = callback.data.split(":")
    flag, pid = parts[3], int(parts[4])
    plan = await session.get(ResellerPlan, pid)
    if not plan:
        await callback.answer("یافت نشد", show_alert=True)
        return
    if is_addon_plan(plan):
        await callback.answer("برای بسته افزونه این گزینه در دسترس نیست", show_alert=True)
        return
    if flag == "buyextra":
        if not is_subscription_plan(plan) or reseller_plan_mode_of(plan) == "payg":
            await callback.answer("خرید حجم/کاربر اضافه فقط برای اشتراک ثابت است", show_alert=True)
            return
        plan.allow_buy_extra = not bool(plan.allow_buy_extra)
        if plan.allow_buy_extra:
            plan.renew_pricing_mode = "from_capacity"
        else:
            plan.extra_gb_price = 0
            plan.extra_user_price = 0
    elif flag == "pgadmin":
        plan.create_pg_admin = not bool(plan.create_pg_admin)
        if not plan.create_pg_admin:
            plan.share_pg_panel_url = False
    else:
        if not bool(plan.create_pg_admin):
            await callback.answer("اول ساخت ادمین پنل را فعال کنید", show_alert=True)
            return
        plan.share_pg_panel_url = not bool(plan.share_pg_panel_url)
    await _persist(session)
    await callback.answer("بروز شد")
    if callback.message:
        await safe_edit_text(
            callback.message,
            _resplan_detail_text(plan),
            reply_markup=_resplan_detail_keyboard(plan),
        )

@router.callback_query(F.data.startswith("adm:resplan:delask:"))
@require_bot_owner_handler
async def resplan_del_ask(callback: CallbackQuery, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    pid = int(callback.data.rsplit(":", 1)[-1])
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            "⚠️ این پلن نمایندگی حذف شود؟",
            reply_markup=_kb(
                [
                    [
                        InlineKeyboardButton(
                            text="🗑 تأیید حذف",
                            callback_data=f"adm:resplan:del:{pid}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="انصراف",
                            callback_data=f"adm:resplan:view:{pid}",
                        )
                    ],
                ]
            ),
        )

@router.callback_query(F.data.startswith("adm:resplan:del:"))
@require_bot_owner_handler
async def resplan_del(callback: CallbackQuery, session: AsyncSession, db_user: BotUser, state: FSMContext):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    from app.services.plans_catalog import PlanDeleteBlocked, delete_reseller_plan

    pid = int(callback.data.rsplit(":", 1)[-1])
    plan = await session.get(ResellerPlan, pid)
    if not plan:
        await callback.answer("یافت نشد", show_alert=True)
        return
    mode = reseller_plan_mode_of(plan)
    try:
        await delete_reseller_plan(session, plan)
        await _persist(session)
    except PlanDeleteBlocked as e:
        try:
            await session.rollback()
        except Exception:
            pass
        msg = e.message if len(e.message) <= 180 else e.message[:177] + "…"
        await callback.answer(msg, show_alert=True)
        return
    await callback.answer("حذف شد")
    await state.update_data(_adm_plans_aud="resellers", _adm_plans_kind=mode)
    if callback.message:
        await _rerender_plans_screen(callback, session, "resellers", mode)

@router.callback_query(
    F.data.regexp(
        r"^adm:resplan:edit:(name|price|comm|rate|desc|grp|role|addon_gb|addon_users|extra_gb|extra_user):\d+$"
    )
)
@require_bot_owner_handler
async def resplan_edit_ask(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    from app.services.pg_admin_subscription import is_addon_plan, is_subscription_plan

    parts = callback.data.split(":")
    if len(parts) < 5:
        await callback.answer("نامعتبر", show_alert=True)
        return
    field, pid_raw = parts[3], parts[4]
    plan = await session.get(ResellerPlan, int(pid_raw))
    if not plan:
        await callback.answer("یافت نشد", show_alert=True)
        return
    mode = reseller_plan_mode_of(plan)
    kind = str(getattr(plan, "plan_kind", "") or "subscription")
    list_kind = kind if is_addon_plan(plan) else mode
    await state.update_data(
        resplan_edit_id=plan.id,
        resplan_edit_field=field,
        _adm_plans_aud="resellers",
        _adm_plans_kind=list_kind,
    )
    if field in {"grp", "role"} and is_addon_plan(plan):
        await callback.answer("برای بسته افزونه گروه/نقش لازم نیست", show_alert=True)
        return
    if field in {"extra_gb", "extra_user"}:
        if is_addon_plan(plan) or mode == "payg" or not is_subscription_plan(plan):
            await callback.answer("فقط برای اشتراک ثابت با خرید اضافه", show_alert=True)
            return
    if field == "addon_gb" and kind != "addon_volume":
        await callback.answer("فقط برای بسته حجم", show_alert=True)
        return
    if field == "addon_users" and kind != "addon_users":
        await callback.answer("فقط برای بسته کاربر", show_alert=True)
        return
    if field == "grp":
        selected: list[int] = []
        if plan.pg_group_ids:
            for part in str(plan.pg_group_ids).split(","):
                if part.strip().isdigit():
                    selected.append(int(part.strip()))
        await state.update_data(resplan_edit_groups=selected)
        await callback.answer()
        await _show_resplan_groups(callback, state, plan.id)
        return
    if field == "role":
        await callback.answer()
        await state.set_state(AdminPlansStates.res_plan_role)
        await state.update_data(res_plan_edit_role_id=plan.id)
        if callback.message:
            await _show_resplan_role_picker(callback.message, state, edit_plan_id=plan.id)
        return
    prompts = {
        "name": "نام جدید:",
        "price": "قیمت بسته (تومان):" if is_addon_plan(plan) else "قیمت ورود (تومان):",
        "rate": "نرخ هر گیگ (تومان):",
        "desc": "توضیح (خالی = حذف):",
        "addon_gb": "حجم بسته (گیگابایت):",
        "addon_users": "تعداد کاربر بسته:",
        "extra_gb": "قیمت هر گیگ اضافه (تومان):",
        "extra_user": "قیمت هر کاربر اضافه (تومان):",
    }
    if field not in prompts:
        await callback.answer("نامعتبر", show_alert=True)
        return
    await callback.answer()
    await state.set_state(AdminPlansStates.res_plan_edit_field)
    if callback.message:
        await callback.message.answer(prompts[field], reply_markup=kb.cancel_reply())

async def _show_resplan_groups(callback: CallbackQuery, state: FSMContext, plan_id: int) -> None:
    selected = [int(x) for x in ((await state.get_data()).get("resplan_edit_groups") or [])]
    try:
        groups = await get_pg().get_groups_simple()
    except Exception:
        groups = []
    rows: list[list[InlineKeyboardButton]] = []
    for g in groups[:25]:
        gid = g.get("id")
        if gid is None:
            continue
        gid = int(gid)
        mark = "✅ " if gid in selected else ""
        name = g.get("name") or f"گروه {gid}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark}{name}"[:40],
                    callback_data=f"adm:resplan:edit:toggrp:{plan_id}:{gid}",
                )
            ]
        )
    if rows:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"تأیید ({len(selected)})",
                    callback_data=f"adm:resplan:edit:grpdone:{plan_id}",
                )
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    text="گروهی از پاسارگارد لود نشد",
                    callback_data=f"adm:resplan:view:{plan_id}",
                )
            ]
        )
    rows.append(_back_row("⬅️ پلن", f"adm:resplan:view:{plan_id}"))
    if callback.message:
        await callback.message.edit_text(
            "گروه(ها) را انتخاب کنید (حداقل یک گروه الزامی):",
            reply_markup=_kb(rows),
        )

@router.callback_query(F.data.startswith("adm:resplan:edit:toggrp:"))
@require_bot_owner_handler
async def resplan_edit_tog_grp(callback: CallbackQuery, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    parts = callback.data.split(":")
    plan_id = int(parts[4])
    gid = int(parts[5])
    selected = [int(x) for x in ((await state.get_data()).get("resplan_edit_groups") or [])]
    if gid in selected:
        selected = [x for x in selected if x != gid]
    else:
        selected.append(gid)
    await state.update_data(resplan_edit_groups=selected)
    await callback.answer()
    await _show_resplan_groups(callback, state, plan_id)

@router.callback_query(F.data.startswith("adm:resplan:edit:grpdone:"))
@require_bot_owner_handler
async def resplan_edit_grp_done(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    from app.services.pg_admin_subscription import is_addon_plan

    plan_id = int(callback.data.rsplit(":", 1)[-1])
    plan = await session.get(ResellerPlan, plan_id)
    if not plan:
        await callback.answer("یافت نشد", show_alert=True)
        return
    if is_addon_plan(plan):
        await callback.answer("برای بسته افزونه گروه لازم نیست", show_alert=True)
        return
    selected = [int(x) for x in ((await state.get_data()).get("resplan_edit_groups") or [])]
    if not selected:
        await callback.answer("حداقل یک گروه الزامی است", show_alert=True)
        return
    plan.pg_group_ids = ",".join(str(x) for x in selected)
    from app.services.billing import sync_plan_billing_rate

    await sync_plan_billing_rate(session, plan)
    await _persist(session)
    await state.update_data(resplan_edit_groups=[])
    await callback.answer("ذخیره شد")
    if callback.message:
        await callback.message.edit_text(
            _resplan_detail_text(plan),
            reply_markup=_resplan_detail_keyboard(plan),
        )

@router.callback_query(F.data.startswith("adm:resplan:perms:"))
@require_bot_owner_handler
async def resplan_perms_screen(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    from app.services.pg_admin_subscription import is_addon_plan

    plan_id = int(callback.data.rsplit(":", 1)[-1])
    plan = await session.get(ResellerPlan, plan_id)
    if not plan:
        await callback.answer("یافت نشد", show_alert=True)
        return
    if is_addon_plan(plan):
        await callback.answer("بسته افزونه دسترسی وب/ربات ندارد", show_alert=True)
        return
    active = set(parse_perms(plan.web_permissions or plan.bot_permissions))
    rows: list[list[InlineKeyboardButton]] = []
    for key, label in FEATURE_PERMS:
        mark = "✅" if key in active else "⬜️"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark} {label}",
                    callback_data=f"adm:resplan:togperm:{plan_id}:{key}",
                )
            ]
        )
    rows.append(_back_row("⬅️ پلن", f"adm:resplan:view:{plan_id}"))
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            "🔐 <b>دسترسی‌های پلن</b>\nوب و ربات نماینده یکسان است.",
            reply_markup=_kb(rows),
        )

@router.callback_query(F.data.startswith("adm:resplan:togperm:"))
@require_bot_owner_handler
async def resplan_tog_perm(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    from app.services.pg_admin_subscription import is_addon_plan

    parts = callback.data.split(":")
    plan_id = int(parts[3])
    key = parts[4]
    plan = await session.get(ResellerPlan, plan_id)
    if not plan:
        await callback.answer("یافت نشد", show_alert=True)
        return
    if is_addon_plan(plan):
        await callback.answer("بسته افزونه دسترسی وب/ربات ندارد", show_alert=True)
        return
    perms = set(parse_perms(plan.web_permissions or plan.bot_permissions))
    if key in perms:
        perms.discard(key)
    else:
        perms.add(key)
    csv = ",".join(sorted(perms))
    plan.web_permissions = csv
    plan.bot_permissions = csv
    plan.can_approve_receipts = "payments" in perms
    await _persist(session)
    await callback.answer("بروز شد")
    active = perms
    rows: list[list[InlineKeyboardButton]] = []
    for perm_key, label in FEATURE_PERMS:
        mark = "✅" if perm_key in active else "⬜️"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark} {label}",
                    callback_data=f"adm:resplan:togperm:{plan_id}:{perm_key}",
                )
            ]
        )
    rows.append(_back_row("⬅️ پلن", f"adm:resplan:view:{plan_id}"))
    if callback.message:
        await callback.message.edit_text(
            "🔐 <b>دسترسی‌های پلن</b>\nوب و ربات نماینده یکسان است.",
            reply_markup=_kb(rows),
        )

@router.message(AdminPlansStates.res_plan_edit_field)
@require_bot_owner_handler
async def resplan_edit_save(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await state.clear()
        return
    if kb.is_cancel_text(message.text):
        await _answer_plans_cancel(message, state, session)
        return
    data = await state.get_data()
    plan_id = int(data.get("resplan_edit_id") or 0)
    field = data.get("resplan_edit_field")
    plan = await session.get(ResellerPlan, plan_id)
    if not plan or not field:
        await state.clear()
        return
    text = (message.text or "").strip()
    from app.services.billing import sync_plan_billing_rate
    from app.services.pg_admin_subscription import is_addon_plan

    is_addon = is_addon_plan(plan)
    is_payg = reseller_plan_mode_of(plan) == "payg"
    try:
        if field == "name":
            if not text:
                await message.answer("نام خالی نیست.", reply_markup=kb.cancel_reply())
                return
            plan.name = text[:128]
        elif field == "price":
            plan.price = max(0, parse_bot_int(text))
        elif field == "rate":
            plan.price_per_gb = max(0, parse_bot_int(text))
        elif field == "desc":
            plan.description = text or None
        elif field == "addon_gb":
            if not is_addon or str(getattr(plan, "plan_kind", "")) != "addon_volume":
                await message.answer("این فیلد فقط برای بسته حجم است.")
                return
            plan.addon_gb = max(1, parse_bot_int(text))
        elif field == "addon_users":
            if not is_addon or str(getattr(plan, "plan_kind", "")) != "addon_users":
                await message.answer("این فیلد فقط برای بسته کاربر است.")
                return
            plan.addon_users = max(1, parse_bot_int(text))
        elif field == "extra_gb":
            if is_addon or is_payg:
                await message.answer("این فیلد برای این پلن در دسترس نیست.")
                return
            plan.extra_gb_price = max(0, parse_bot_int(text))
        elif field == "extra_user":
            if is_addon or is_payg:
                await message.answer("این فیلد برای این پلن در دسترس نیست.")
                return
            plan.extra_user_price = max(0, parse_bot_int(text))
        else:
            await state.clear()
            return
    except ValueError:
        await message.answer("عدد معتبر بفرستید.", reply_markup=kb.cancel_reply())
        return
    await sync_plan_billing_rate(session, plan)
    await _persist(session)
    await state.set_state(None)
    await _answer_plans_saved(message, state, session, "ذخیره شد ✅")
    bubble = await message.answer(
        _resplan_detail_text(plan),
        reply_markup=_resplan_detail_keyboard(plan),
    )
    _ = bubble

@router.callback_query(F.data.in_({"adm:resplan:add:fixed", "adm:resplan:add:payg"}))
@require_bot_owner_handler
async def resplan_add_start(callback: CallbackQuery, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    mode = callback.data.rsplit(":", 1)[-1]
    if mode not in {"fixed", "payg"}:
        await callback.answer("نامعتبر", show_alert=True)
        return
    await callback.answer()
    await state.set_state(AdminPlansStates.res_plan_name)
    await state.update_data(res_plan_mode=mode, _adm_plans_aud="resellers", _adm_plans_kind=mode)
    if callback.message:
        await callback.message.answer("نام پلن نمایندگی:", reply_markup=kb.cancel_reply())

@router.message(AdminPlansStates.res_plan_name)
@require_bot_owner_handler
async def resplan_name(message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user) or kb.is_cancel_text(message.text):
        await state.set_state(None)
        await _answer_plans_cancel(message, state, session)
        return
    await state.update_data(res_plan_name=(message.text or "").strip()[:128])
    await state.set_state(AdminPlansStates.res_plan_price)
    await message.answer("قیمت ورود (تومان، ۰ = رایگان):", reply_markup=kb.cancel_reply())

@router.message(AdminPlansStates.res_plan_price)
@require_bot_owner_handler
async def resplan_price(message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user) or kb.is_cancel_text(message.text):
        await state.set_state(None)
        await _answer_plans_cancel(message, state, session)
        return
    try:
        price = max(0, parse_bot_int(message.text))
    except ValueError:
        await message.answer("عدد معتبر بفرستید.")
        return
    await state.update_data(res_plan_price=price)
    data = await state.get_data()
    plan_kind = str(data.get("res_plan_kind") or "subscription")
    if plan_kind in {"addon_volume", "addon_users"}:
        await state.set_state(AdminPlansStates.res_plan_addon_amount)
        ask = "حجم بسته (گیگ):" if plan_kind == "addon_volume" else "تعداد کاربر بسته:"
        await message.answer(ask, reply_markup=kb.cancel_reply())
        return
    if data.get("res_plan_mode") == "payg":
        await state.set_state(AdminPlansStates.res_plan_rate_gb)
        await message.answer("نرخ هر گیگ (تومان):", reply_markup=kb.cancel_reply())
    else:
        await state.update_data(res_plan_groups=[])
        await state.set_state(AdminPlansStates.res_plan_link)
        await message.answer(
            "📁 گروه پاسارگارد را انتخاب کنید (حداقل یک گروه الزامی):",
            reply_markup=kb.cancel_reply(),
        )
        bubble = await message.answer("⏳")
        await _show_resplan_add_groups(bubble, state)

@router.message(AdminPlansStates.res_plan_addon_amount)
@require_bot_owner_handler
async def resplan_addon_amount(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user) or kb.is_cancel_text(message.text):
        await state.set_state(None)
        await _answer_plans_cancel(message, state, session)
        return
    try:
        amount = max(0, parse_bot_int(message.text))
    except ValueError:
        await message.answer("عدد معتبر بفرستید.")
        return
    if amount <= 0:
        await message.answer("مقدار باید بیشتر از صفر باشد.")
        return
    data = await state.get_data()
    plan_kind = str(data.get("res_plan_kind") or "")
    price = int(data.get("res_plan_price") or 0)
    if price <= 0:
        await message.answer("قیمت بسته باید بیشتر از صفر باشد — از اول نام را بفرستید.")
        await state.set_state(AdminPlansStates.res_plan_name)
        return
    addon_gb = amount if plan_kind == "addon_volume" else 0
    addon_users = amount if plan_kind == "addon_users" else 0
    await state.update_data(
        res_addon_gb=addon_gb,
        res_addon_users=addon_users,
        res_plan_create_kind="addon",
    )
    await state.set_state(AdminPlansStates.res_plan_color)
    await _show_resplan_color_picker(
        message,
        session=session,
        callback_prefix="adm:resplan:addcolor",
        back_callback="adm:plans:aud:resellers",
    )

@router.message(AdminPlansStates.res_plan_rate_gb)
@require_bot_owner_handler
async def resplan_rate_gb(
    message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user) or kb.is_cancel_text(message.text):
        await state.set_state(None)
        await _answer_plans_cancel(message, state, session)
        return
    try:
        rate = max(0, parse_bot_int(message.text))
    except ValueError:
        await message.answer("عدد معتبر بفرستید.")
        return
    await state.update_data(res_plan_rate_gb=rate, res_plan_groups=[])
    await state.set_state(AdminPlansStates.res_plan_link)
    await message.answer(
        "📁 گروه پاسارگارد را انتخاب کنید (حداقل یک گروه الزامی):",
        reply_markup=kb.cancel_reply(),
    )
    bubble = await message.answer("⏳")
    await _show_resplan_add_groups(bubble, state)

@router.message(AdminPlansStates.res_plan_link)
@require_bot_owner_handler
async def resplan_link_cancel(message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user) or kb.is_cancel_text(message.text):
        await state.set_state(None)
        await _answer_plans_cancel(message, state, session)
        return
    await message.answer("گروه را از دکمه‌های زیر پیام انتخاب کنید (حداقل یک گروه).")

@router.message(AdminPlansStates.res_plan_role)
@require_bot_owner_handler
async def resplan_role_cancel(message: Message, state: FSMContext, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user) or kb.is_cancel_text(message.text):
        await state.set_state(None)
        await _answer_plans_cancel(message, state, session)
        return
    await message.answer("نقش پاسارگارد را از دکمه‌های زیر پیام انتخاب کنید.")

async def _show_resplan_add_groups(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    selected = [int(x) for x in (data.get("res_plan_groups") or [])]
    mode = data.get("res_plan_mode") or "fixed"
    label = "PAYG" if mode == "payg" else "ثابت"
    try:
        groups = await get_pg().get_groups_simple()
    except Exception:
        groups = []
    rows: list[list[InlineKeyboardButton]] = []
    for g in groups[:25]:
        gid = g.get("id")
        if gid is None:
            continue
        try:
            gid = int(gid)
        except (TypeError, ValueError):
            continue
        mark = "✅ " if gid in selected else ""
        name = g.get("name") or f"گروه {gid}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark}#{gid} — {name}"[:60],
                    callback_data=f"adm:resplan:add:toggrp:{gid}",
                )
            ]
        )
    if rows:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"✅ تأیید گروه ({len(selected)})",
                    callback_data="adm:resplan:add:grpdone",
                )
            ]
        )
    else:
        rows.append(
            [InlineKeyboardButton(text="گروهی از پاسارگارد لود نشد", callback_data="adm:plans:aud:resellers")]
        )
    rows.append([InlineKeyboardButton(text="❌ انصراف", callback_data="adm:plans:aud:resellers")])
    text = (
        f"📁 گروه‌های اینباند — پلن {label}\n"
        "<b>حداقل یک گروه الزامی است.</b>\n"
        f"انتخاب‌شده: {', '.join(str(x) for x in selected) or '—'}"
    )
    await safe_edit_text(message, text, reply_markup=_kb(rows))

async def _show_resplan_role_picker(
    message: Message,
    state: FSMContext,
    *,
    edit_plan_id: int | None = None,
) -> None:
    try:
        roles = await get_pg().get_admin_roles()
    except Exception:
        roles = []
    rows: list[list[InlineKeyboardButton]] = []
    prefix = f"adm:resplan:edit:setrole:{edit_plan_id}" if edit_plan_id else "adm:resplan:add:setrole"
    for r in roles[:25]:
        rid = r.get("id")
        if rid is None:
            continue
        name = r.get("name") or f"نقش {rid}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"#{rid} — {name}"[:60],
                    callback_data=f"{prefix}:{rid}",
                )
            ]
        )
    if not rows:
        rows.append(
            [InlineKeyboardButton(text="نقشی لود نشد — از پاسارگارد نقش بسازید", callback_data="adm:plans:aud:resellers")]
        )
    back = (
        f"adm:resplan:view:{edit_plan_id}"
        if edit_plan_id
        else "adm:plans:aud:resellers"
    )
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=back)])
    await safe_edit_text(
        message,
        "🎭 <b>نقش پاسارگارد الزامی است</b>\n"
        "نقشی که هنگام تأیید نماینده به ادمین PG او داده می‌شود:",
        reply_markup=_kb(rows),
    )

@router.callback_query(F.data.startswith("adm:resplan:add:toggrp:"))
@require_bot_owner_handler
async def resplan_add_tog_grp(callback: CallbackQuery, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    if await state.get_state() != AdminPlansStates.res_plan_link.state:
        await callback.answer("ابتدا ساخت پلن را شروع کنید", show_alert=True)
        return
    try:
        gid = int(callback.data.rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer("نامعتبر", show_alert=True)
        return
    selected = [int(x) for x in ((await state.get_data()).get("res_plan_groups") or [])]
    if gid in selected:
        selected = [x for x in selected if x != gid]
    else:
        selected.append(gid)
    await state.update_data(res_plan_groups=selected)
    await callback.answer()
    if callback.message:
        await _show_resplan_add_groups(callback.message, state)

@router.callback_query(F.data == "adm:resplan:add:grpdone")
@require_bot_owner_handler
async def resplan_add_grp_done(callback: CallbackQuery, state: FSMContext, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    if await state.get_state() != AdminPlansStates.res_plan_link.state:
        await callback.answer("ابتدا ساخت پلن را شروع کنید", show_alert=True)
        return
    selected = [int(x) for x in ((await state.get_data()).get("res_plan_groups") or [])]
    if not selected:
        await callback.answer("حداقل یک گروه الزامی است", show_alert=True)
        return
    await state.set_state(AdminPlansStates.res_plan_role)
    await callback.answer()
    if callback.message:
        await _show_resplan_role_picker(callback.message, state)

@router.callback_query(F.data.startswith("adm:resplan:add:setrole:"))
@require_bot_owner_handler
async def resplan_add_set_role(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    if await state.get_state() != AdminPlansStates.res_plan_role.state:
        await callback.answer("ابتدا نقش را از ویزارد انتخاب کنید", show_alert=True)
        return
    try:
        role_id = int(callback.data.rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer("نامعتبر", show_alert=True)
        return
    data = await state.get_data()
    groups = [int(x) for x in (data.get("res_plan_groups") or [])]
    if not groups:
        await callback.answer("گروه انتخاب نشده", show_alert=True)
        return
    group_csv = ",".join(str(x) for x in groups)
    mode = data.get("res_plan_mode") or "fixed"
    await state.update_data(
        res_plan_role_id=role_id,
        res_plan_create_kind="subscription",
    )
    await state.set_state(AdminPlansStates.res_plan_color)
    await callback.answer()
    await _show_resplan_color_picker(
        callback,
        session=session,
        callback_prefix="adm:resplan:addcolor",
        back_callback="adm:plans:aud:resellers",
    )

@router.callback_query(F.data.startswith("adm:resplan:addcolor:"))
@require_bot_owner_handler
async def resplan_add_color(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    if await state.get_state() != AdminPlansStates.res_plan_color.state:
        await callback.answer("ابتدا ساخت پلن را کامل کنید", show_alert=True)
        return
    from app.services.billing import sync_plan_billing_rate
    from app.services.button_styles import parse_plan_button_style_callback

    style = parse_plan_button_style_callback(callback.data.rsplit(":", 1)[-1])
    data = await state.get_data()
    create_kind = str(data.get("res_plan_create_kind") or "")
    mode = data.get("res_plan_mode") or "fixed"
    try:
        if create_kind == "addon":
            plan_kind = str(data.get("res_plan_kind") or "")
            plan = ResellerPlan(
                name=data.get("res_plan_name") or "بسته اضافه",
                price=int(data.get("res_plan_price") or 0),
                billing_mode="fixed",
                price_per_gb=0,
                pg_group_ids=None,
                plan_kind=plan_kind,
                duration_days=0,
                included_gb=0,
                included_users=0,
                addon_gb=int(data.get("res_addon_gb") or 0),
                addon_users=int(data.get("res_addon_users") or 0),
                renew_pricing_mode="fixed",
                allow_buy_extra=False,
                extra_gb_price=0,
                extra_user_price=0,
                renew_price=0,
                can_approve_receipts=False,
                web_permissions="",
                bot_permissions="",
                create_pg_admin=False,
                create_web_access=False,
                share_pg_panel_url=False,
                pg_role_id=None,
                button_style=style,
                is_active=True,
            )
            session.add(plan)
            await session.flush()
            await sync_plan_billing_rate(session, plan)
            await _persist(session)
            await session.refresh(plan)
            saved_label = "بسته"
        else:
            plan = await _save_reseller_plan(
                session,
                state,
                price_per_gb=int(data.get("res_plan_rate_gb") or 0) if mode == "payg" else 0,
                pg_group_ids=",".join(str(int(x)) for x in (data.get("res_plan_groups") or [])),
                pg_role_id=int(data.get("res_plan_role_id") or 0),
                button_style=style,
            )
            saved_label = "PAYG" if mode == "payg" else "ثابت"
    except Exception:
        await callback.answer("ذخیره ناموفق بود", show_alert=True)
        return
    await state.set_state(None)
    await callback.answer("ذخیره شد")
    if callback.message:
        await safe_edit_text(
            callback.message,
            f"پلن {saved_label} #{plan.id} ذخیره شد ✅\n\n{_resplan_detail_text(plan)}",
            reply_markup=_resplan_detail_keyboard(plan),
        )
        await sync_plans_reply_keyboard(
            callback.message, session, db_user, state, audience="resellers"
        )

@router.callback_query(F.data.startswith("adm:resplan:colorpick:"))
@require_bot_owner_handler
async def resplan_color_pick(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    pid = int(callback.data.rsplit(":", 1)[-1])
    plan = await session.get(ResellerPlan, pid)
    if not plan:
        await callback.answer("یافت نشد", show_alert=True)
        return
    await callback.answer()
    await _show_resplan_color_picker(
        callback,
        session=session,
        callback_prefix=f"adm:resplan:setcolor:{pid}",
        back_callback=f"adm:resplan:view:{pid}",
    )

@router.callback_query(F.data.startswith("adm:resplan:setcolor:"))
@require_bot_owner_handler
async def resplan_set_color(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) < 5:
        await callback.answer("نامعتبر", show_alert=True)
        return
    pid = int(parts[3])
    from app.services.button_styles import parse_plan_button_style_callback

    plan = await session.get(ResellerPlan, pid)
    if not plan:
        await callback.answer("یافت نشد", show_alert=True)
        return
    plan.button_style = parse_plan_button_style_callback(parts[4])
    await _persist(session)
    await callback.answer("رنگ ذخیره شد")
    if callback.message:
        await safe_edit_text(
            callback.message,
            _resplan_detail_text(plan),
            reply_markup=_resplan_detail_keyboard(plan),
        )

@router.callback_query(F.data.startswith("adm:resplan:edit:setrole:"))
@require_bot_owner_handler
async def resplan_edit_set_role(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext, db_user: BotUser
):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    from app.services.pg_admin_subscription import is_addon_plan

    parts = callback.data.split(":")
    # adm:resplan:edit:setrole:{plan_id}:{role_id}
    if len(parts) < 6:
        await callback.answer("نامعتبر", show_alert=True)
        return
    plan_id = int(parts[4])
    role_id = int(parts[5])
    plan = await session.get(ResellerPlan, plan_id)
    if not plan:
        await callback.answer("یافت نشد", show_alert=True)
        return
    if is_addon_plan(plan):
        await callback.answer("برای بسته افزونه نقش لازم نیست", show_alert=True)
        return
    plan.pg_role_id = role_id
    await _persist(session)
    await state.set_state(None)
    await callback.answer("نقش ذخیره شد")
    if callback.message:
        await safe_edit_text(
            callback.message,
            _resplan_detail_text(plan),
            reply_markup=_resplan_detail_keyboard(plan),
        )

async def _save_reseller_plan(
    session: AsyncSession,
    state: FSMContext,
    *,
    price_per_gb: int | None = None,
    pg_group_ids: str | None = None,
    pg_role_id: int | None = None,
    button_style: str | None = None,
) -> ResellerPlan:
    from app.services.billing import sync_plan_billing_rate

    data = await state.get_data()
    mode = data.get("res_plan_mode") or "fixed"
    if not pg_group_ids:
        groups = data.get("res_plan_groups") or []
        if groups:
            pg_group_ids = ",".join(str(int(x)) for x in groups)
    if not pg_group_ids:
        raise ValueError("pg_group_ids required")
    role_id = pg_role_id if pg_role_id is not None else data.get("res_plan_role_id")
    if not role_id:
        raise ValueError("pg_role_id required")
    is_payg = mode == "payg"
    plan = ResellerPlan(
        name=data.get("res_plan_name") or "پلن نماینده",
        price=int(data.get("res_plan_price") or 0),
        billing_mode=mode,
        price_per_gb=int(price_per_gb or data.get("res_plan_rate_gb") or 0) if is_payg else 0,
        pg_group_ids=pg_group_ids,
        pg_role_id=int(role_id),
        plan_kind="subscription",
        allow_buy_extra=False,
        extra_gb_price=0,
        extra_user_price=0,
        web_permissions=DEFAULT_FEATURE_PERMS,
        bot_permissions=DEFAULT_FEATURE_PERMS,
        create_pg_admin=True,
        create_web_access=True,
        button_style=button_style,
        is_active=True,
    )
    session.add(plan)
    await session.flush()
    await sync_plan_billing_rate(session, plan)
    await _persist(session)
    await session.refresh(plan)
    return plan

