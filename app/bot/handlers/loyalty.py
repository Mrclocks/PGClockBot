"""Telegram Referral + Loyalty / Points UX (customer + staff manage)."""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.tg_utils import safe_edit_text
from app.config import get_settings
from app.db.models import BotUser, LoyaltyReward, LoyaltyTier, PointsRule, Role, UserService
from app.services.formatting import format_message, format_toman, kv_line
from app.services.loyalty import (
    EVENT_LABELS,
    REWARD_TYPE_LABELS,
    SETTING_LOYALTY_ENABLED,
    SETTING_POINTS_TO_WALLET_RATE,
    ensure_loyalty_defaults,
    get_tier_for_points,
    list_active_rewards,
    list_available_discounts,
    list_points_history,
    loyalty_enabled,
    month_earned_points,
    overview_metrics,
    redeem_reward,
    referral_link,
    referral_stats,
)
from app.services.lucky_wheel import (
    PRIZE_TYPE_LABELS as WHEEL_PRIZE_TYPE_LABELS,
    get_user_wheel_status,
    list_active_pool,
    spin as wheel_spin,
    wheel_feature_enabled,
)
from app.services.message_variables import DOMAIN_REFERRAL, render_message_template
from app.services.users import get_all_settings, get_setting, on, set_setting
from app.services.redact import user_safe_error

router = Router(name="loyalty")


class LoyaltyManageStates(StatesGroup):
    edit_wallet_rate = State()
    edit_referral_text = State()


# ---------------------------------------------------------------------------
# Customer inline keyboards — secondary actions ONLY
# Main club subsets live on the reply keyboard (like wallet/support).
# Never mirror دعوت / امتیاز / جوایز / تاریخچه / خانه here.
# ---------------------------------------------------------------------------


def _ref_actions_keyboard(*, share_url: str | None = None) -> InlineKeyboardMarkup:
    """Invite extras: share URL + stats drill-down."""
    rows: list[list[InlineKeyboardButton]] = []
    if share_url:
        rows.append(
            [InlineKeyboardButton(text="📤 اشتراک‌گذاری لینک", url=share_url)]
        )
    rows.append(
        [InlineKeyboardButton(text="📊 آمار دعوت", callback_data="ref:stats")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _points_extras_keyboard() -> InlineKeyboardMarkup:
    """Under امتیاز من — discounts are not a reply-KB main subset."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏷 تخفیف‌های من", callback_data="loy:discounts")],
        ]
    )


def _rewards_redeem_keyboard(
    rewards: list, *, include_back: bool = False
) -> InlineKeyboardMarkup | None:
    """Per-reward redeem buttons (item pickers). Optional back only for callback trees."""
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=f"دریافت «{r.name}»",
                callback_data=f"loy:redeem:{r.id}",
            )
        ]
        for r in rewards
    ]
    if include_back:
        rows.append(
            [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="loy:home")]
        )
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _hist_page_keyboard(
    *, page: int, has_more: bool, include_back: bool = False
) -> InlineKeyboardMarkup | None:
    nav_btns: list[InlineKeyboardButton] = []
    if page > 0:
        nav_btns.append(
            InlineKeyboardButton(text="◀️ قبلی", callback_data=f"loy:hist:{page - 1}")
        )
    if has_more:
        nav_btns.append(
            InlineKeyboardButton(text="بعدی ▶️", callback_data=f"loy:hist:{page + 1}")
        )
    rows: list[list[InlineKeyboardButton]] = []
    if nav_btns:
        rows.append(nav_btns)
    if include_back:
        rows.append(
            [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="loy:home")]
        )
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _bot_username(callback_or_message) -> str:
    bot = callback_or_message.bot
    me = await bot.get_me()
    return me.username or get_settings().bot_username or "bot"


async def build_referral_text(
    session: AsyncSession, db_user: BotUser, uname: str
) -> tuple[str, str, dict]:
    from app.services.rich_text import (
        outbound_setting_text,
        rich_plain_text,
        unpack_rich_text,
    )

    ui = await get_all_settings(session)
    link = referral_link(uname, db_user.referral_code)
    try:
        await ensure_loyalty_defaults(session, reseller_id=db_user.reseller_id)
    except Exception:
        pass
    stats = await referral_stats(session, db_user.id)
    _, packed_ents = unpack_rich_text(ui.get("referral_text"))
    if packed_ents:
        extra = "\n".join(
            [
                "",
                f"👥 دعوت‌های موفق: {stats['qualified'] or stats['total']}",
                f"⭐ امتیاز از دعوت: {stats['earned_points']}",
                "",
                f"لینک دعوت:\n{link}",
            ]
        )
    else:
        extra = "\n".join(
            [
                "",
                kv_line("👥", "دعوت‌های موفق", str(stats["qualified"] or stats["total"])),
                kv_line("⭐", "امتیاز از دعوت", str(stats["earned_points"])),
                "",
                f"لینک دعوت:\n<code>{link}</code>",
            ]
        )
    try:
        text, send_kw = outbound_setting_text(
            ui.get("referral_text") or "",
            title="👥 دعوت دوستان",
            domain=DOMAIN_REFERRAL,
            append=extra,
            code=db_user.referral_code,
            link=link,
            shop_title=rich_plain_text(ui.get("shop_title")) or "",
        )
    except Exception:
        body = f"کد دعوت: <code>{db_user.referral_code}</code>\n{link}"
        text = format_message("👥 دعوت دوستان", body + extra)
        send_kw = {}
    return text, link, send_kw


async def build_loyalty_text(session: AsyncSession, db_user: BotUser) -> str:
    await session.refresh(db_user)
    try:
        await ensure_loyalty_defaults(session, reseller_id=db_user.reseller_id)
    except Exception:
        pass
    tier = await get_tier_for_points(session, int(db_user.points_balance or 0))
    stats = await referral_stats(session, db_user.id)
    month = await month_earned_points(session, db_user.id)
    lines = [
        kv_line("⭐", "امتیاز شما", f"<b>{int(db_user.points_balance or 0)}</b>"),
        kv_line("🏅", "سطح", tier.name),
    ]
    if tier.points_to_next is not None:
        lines.append(kv_line("📈", "تا سطح بعد", f"{tier.points_to_next} امتیاز"))
    lines.extend(
        [
            kv_line("👥", "دعوت‌های موفق", str(stats["qualified"] or stats["total"])),
            kv_line("📅", "امتیاز این ماه", str(month)),
        ]
    )
    return format_message("⭐ باشگاه مشتریان", "\n".join(lines))


# ---------------------------------------------------------------------------
# Staff ACL — fail closed; never use Owner PG from reseller path
# ---------------------------------------------------------------------------


async def resolve_loyalty_manage_scope(
    session: AsyncSession,
    db_user: BotUser,
    *,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> tuple[int | None, bool] | None:
    """Return ``(scope_reseller_id, can_manage_tiers)`` or ``None`` if denied.

    - Platform admin on main bot → ``(None, True)``
    - Reseller actor on shop bot with loyalty perm → ``(owner_id, False)``
    - Anything else (incl. admin on reseller bot) → denied
    """
    if is_reseller_bot:
        from app.services.reseller_access import load_reseller_actor
        from app.services.resellers import has_bot_perm

        owner_id, profile = await load_reseller_actor(
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        if not owner_id or not profile:
            return None
        if not has_bot_perm(profile, "loyalty"):
            return None
        return int(owner_id), False

    if db_user.role == Role.ADMIN.value:
        return None, True
    return None


def _rule_in_scope(rule: PointsRule, scope: int | None) -> bool:
    if scope is None:
        return rule.reseller_id is None
    return rule.reseller_id is not None and int(rule.reseller_id) == int(scope)


def _reward_in_scope(reward: LoyaltyReward, scope: int | None) -> bool:
    if scope is None:
        return reward.reseller_id is None
    return reward.reseller_id is not None and int(reward.reseller_id) == int(scope)


# ---------------------------------------------------------------------------
# Customer reply-keyboard openers
# ---------------------------------------------------------------------------


async def open_loyalty_home_message(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
    *,
    push: bool = True,
) -> None:
    """Club hub: overview + reply submenu (classic) or single inline hub (inline)."""
    from app.bot import menu_nav as nav
    from app.bot.nav_mode import is_inline_nav
    from app.bot.nav_inline import loyalty_hub_keyboard, present_inline_only

    ui = await get_all_settings(session)
    if not await loyalty_enabled(session, reseller_id=db_user.reseller_id):
        disabled = format_message("⭐ باشگاه مشتریان", "این بخش فعلاً غیرفعال است.")
        if is_inline_nav(ui):
            if state is not None:
                await nav.set_nav_level(state, nav.NAV_LOYALTY, push=push)
            await present_inline_only(
                message, text=disabled, inline=loyalty_hub_keyboard(ui)
            )
            return
        await nav.show_nav_keyboard(
            message,
            session,
            db_user,
            nav.NAV_LOYALTY,
            text=disabled,
            state=state,
            push=push,
        )
        return
    text = await build_loyalty_text(session, db_user)
    if is_inline_nav(ui):
        if state is not None:
            await nav.set_nav_level(state, nav.NAV_LOYALTY, push=push)
        await present_inline_only(
            message,
            text=text + "\n\nیک بخش را از دکمه‌های زیر انتخاب کنید.",
            inline=loyalty_hub_keyboard(ui),
        )
        return
    await nav.show_nav_keyboard(
        message,
        session,
        db_user,
        nav.NAV_LOYALTY,
        text=text + "\n\nاز کیبورد پایین بخش مورد نظر را انتخاب کنید.",
        state=state,
        push=push,
    )


async def open_loyalty_referral_message(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
    *,
    push: bool = True,
) -> None:
    """Invite subset: reply club KB + inline share/stats only."""
    from app.bot import menu_nav as nav

    uname = await _bot_username(message)
    text, link, send_kw = await build_referral_text(session, db_user, uname)
    share = f"https://t.me/share/url?url={link}&text="
    from app.bot.nav_mode import is_inline_nav
    from app.bot.nav_inline import present_inline_only
    from app.bot.tg_utils import attach_reply_keyboard

    ui = await get_all_settings(session)
    await nav.set_loy_step(state, "referral")
    ref_kb = _ref_actions_keyboard(share_url=share)
    if is_inline_nav(ui):
        if state is not None:
            await nav.set_nav_level(state, nav.NAV_LOYALTY, push=push)
        # One message: referral text + share/stats inline (no chrome carrier).
        await present_inline_only(message, text=text, inline=ref_kb, **send_kw)
        return
    await nav.show_nav_keyboard(
        message,
        session,
        db_user,
        nav.NAV_LOYALTY,
        text=text,
        state=state,
        push=push,
        **send_kw,
    )
    await message.answer(
        "اشتراک و آمار:",
        reply_markup=ref_kb,
    )
    await attach_reply_keyboard(
        message, kb.loyalty_reply_keyboard(ui), text="⌨️ باشگاه مشتریان"
    )


async def open_loyalty_points_message(
    message: Message, session: AsyncSession, db_user: BotUser
) -> None:
    """Points overview; discounts inline. Inline nav: single message, no chrome."""
    ui = await get_all_settings(session)
    from app.bot.nav_mode import is_inline_nav
    from app.bot.nav_inline import present_inline_only

    if not await loyalty_enabled(session, reseller_id=db_user.reseller_id):
        disabled = format_message("⭐ باشگاه مشتریان", "این بخش فعلاً غیرفعال است.")
        if is_inline_nav(ui):
            await present_inline_only(
                message,
                text=disabled,
                inline=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="⬅️ بازگشت", callback_data="nv:loy:home"
                            )
                        ]
                    ]
                ),
            )
        else:
            await message.answer(disabled, reply_markup=kb.loyalty_reply_keyboard(ui))
        return
    from app.bot.tg_utils import attach_reply_keyboard

    text = await build_loyalty_text(session, db_user)
    extras = _points_extras_keyboard()
    if is_inline_nav(ui):
        rows = list(extras.inline_keyboard)
        rows.append(
            [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="nv:loy:home")]
        )
        await present_inline_only(
            message,
            text=text,
            inline=InlineKeyboardMarkup(inline_keyboard=rows),
        )
        return
    await message.answer(text, reply_markup=kb.loyalty_reply_keyboard(ui))
    await message.answer(
        "جزئیات بیشتر:",
        reply_markup=extras,
    )
    await attach_reply_keyboard(message, kb.loyalty_reply_keyboard(ui), text="⌨️ باشگاه مشتریان")


async def open_loyalty_rewards_message(
    message: Message, session: AsyncSession, db_user: BotUser
) -> None:
    """Rewards list; redeem pickers inline (not main nav)."""
    ui = await get_all_settings(session)
    from app.bot.nav_mode import is_inline_nav
    from app.bot.nav_inline import present_inline_only

    await session.refresh(db_user)
    rewards = await list_active_rewards(session, reseller_id=db_user.reseller_id)
    lines = [kv_line("⭐", "امتیاز فعلی", f"<b>{int(db_user.points_balance or 0)}</b>"), ""]
    if not rewards:
        lines.append("هنوز جایزه‌ای تعریف نشده است.")
    else:
        for r in rewards:
            type_label = REWARD_TYPE_LABELS.get(r.reward_type, r.reward_type)
            desc = (r.description or "").strip()
            lines.append(
                f"• <b>{r.name}</b>\n"
                f"  {type_label}: {r.reward_value} · هزینه: {r.points_cost} امتیاز"
                + (f"\n  <i>{desc}</i>" if desc else "")
            )
    from app.bot.tg_utils import attach_reply_keyboard

    body = format_message("🎁 جوایز", "\n".join(lines))
    redeem_kb = _rewards_redeem_keyboard(
        rewards, include_back=is_inline_nav(ui)
    )
    if is_inline_nav(ui):
        # Single message: list + redeem buttons (back → club hub via loy:home/nv).
        if redeem_kb is None:
            redeem_kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="⬅️ بازگشت", callback_data="nv:loy:home"
                        )
                    ]
                ]
            )
        else:
            rows = list(redeem_kb.inline_keyboard)
            # replace loy:home back with nv:loy:home for hub re-open
            rows = [
                [
                    InlineKeyboardButton(text=b.text, callback_data="nv:loy:home")
                    if b.callback_data == "loy:home"
                    else b
                    for b in row
                ]
                for row in rows
            ]
            redeem_kb = InlineKeyboardMarkup(inline_keyboard=rows)
        await present_inline_only(message, text=body, inline=redeem_kb)
        return
    await message.answer(body, reply_markup=kb.loyalty_reply_keyboard(ui))
    if redeem_kb is not None:
        await message.answer("برای دریافت، جایزه را انتخاب کنید:", reply_markup=redeem_kb)
        await attach_reply_keyboard(
            message, kb.loyalty_reply_keyboard(ui), text="⌨️ باشگاه مشتریان"
        )




def _wheel_spin_keyboard(*, can_spin: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if can_spin:
        rows.append(
            [InlineKeyboardButton(text="🎡 بچرخ", callback_data="loy:wheel:spin")]
        )
    rows.append(
        [InlineKeyboardButton(text="🔄 به‌روزرسانی", callback_data="loy:wheel:hub")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _build_wheel_hub_text(session: AsyncSession, db_user: BotUser) -> tuple[str, bool]:
    status = await get_user_wheel_status(session, db_user)
    if not status.enabled:
        return "چرخ شانس فعلاً غیرفعال است.", False
    pool = await list_active_pool(session, reseller_id=db_user.reseller_id)
    lines = [
        kv_line("⭐", "امتیاز شما", str(status.points_balance)),
        kv_line("🎟", "هزینه هر چرخش", f"{status.spin_cost} امتیاز" if status.spin_cost else "رایگان"),
        kv_line("🎁", "چرخش رایگان باقی‌مانده", str(status.free_spins_left)),
    ]
    if status.daily_limit > 0:
        lines.append(
            kv_line(
                "📅",
                "چرخش امروز",
                f"{status.spins_today} / {status.daily_limit}",
            )
        )
    if status.cooldown_remaining > 0:
        lines.append(kv_line("⏳", "زمان انتظار", f"{status.cooldown_remaining} ثانیه"))
    if status.block_reason:
        lines.extend(["", f"⚠️ {html.escape(status.block_reason)}"])
    if pool:
        lines.extend(["", "<b>بخش‌های فعال:</b>"])
        for p in pool[:12]:
            type_label = WHEEL_PRIZE_TYPE_LABELS.get(p.prize_type, p.prize_type)
            lines.append(
                f"• {html.escape(p.label or '')} "
                f"<i>({html.escape(type_label)}"
                f"{f': {int(p.prize_value)}' if p.prize_type != 'none' else ''})</i>"
            )
    else:
        lines.extend(["", "هنوز جایزه‌ای برای چرخ تعریف نشده است."])
    return "\n".join(lines), status.can_spin


async def open_loyalty_wheel_message(
    message: Message, session: AsyncSession, db_user: BotUser
) -> None:
    """Lucky wheel hub: status + inline spin."""
    ui = await get_all_settings(session)
    from app.bot.nav_mode import is_inline_nav
    from app.bot.nav_inline import present_inline_only

    if not await loyalty_enabled(session, reseller_id=db_user.reseller_id):
        disabled = format_message("⭐ باشگاه مشتریان", "این بخش فعلاً غیرفعال است.")
        if is_inline_nav(ui):
            await present_inline_only(
                message,
                text=disabled,
                inline=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="⬅️ بازگشت", callback_data="nv:loy:home"
                            )
                        ]
                    ]
                ),
            )
        else:
            await message.answer(disabled, reply_markup=kb.loyalty_reply_keyboard(ui))
        return
    body, can_spin = await _build_wheel_hub_text(session, db_user)
    spin_kb = _wheel_spin_keyboard(can_spin=can_spin)
    if is_inline_nav(ui):
        rows = list(spin_kb.inline_keyboard)
        rows.append(
            [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="nv:loy:home")]
        )
        await present_inline_only(
            message,
            text=format_message("🎡 چرخ شانس", body),
            inline=InlineKeyboardMarkup(inline_keyboard=rows),
        )
        return
    await message.answer(
        format_message("🎡 چرخ شانس", body),
        reply_markup=kb.loyalty_reply_keyboard(ui),
    )
    await message.answer(
        "چرخش:",
        reply_markup=spin_kb,
    )

async def open_loyalty_history_message(
    message: Message, session: AsyncSession, db_user: BotUser
) -> None:
    """History; pagination inline only when needed."""
    ui = await get_all_settings(session)
    from app.bot.nav_mode import is_inline_nav
    from app.bot.nav_inline import present_inline_only

    rows = await list_points_history(session, db_user.id, limit=8, offset=0)
    await session.refresh(db_user)
    if not rows:
        body = "تاریخچه‌ای نیست."
    else:
        lines = []
        for t in rows:
            sign = "+" if t.amount >= 0 else ""
            lines.append(f"{sign}{t.amount} — {t.description or t.tx_type}")
        body = "\n".join(lines)
    body += f"\n\nموجودی فعلی: <b>{int(db_user.points_balance or 0)}</b>"
    title_body = format_message("📜 تاریخچه امتیاز", body)
    page_kb = _hist_page_keyboard(
        page=0, has_more=len(rows) >= 8, include_back=False
    )
    if is_inline_nav(ui):
        rows_kb: list[list[InlineKeyboardButton]] = []
        if page_kb is not None:
            rows_kb.extend(page_kb.inline_keyboard)
        rows_kb.append(
            [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="nv:loy:home")]
        )
        await present_inline_only(
            message,
            text=title_body,
            inline=InlineKeyboardMarkup(inline_keyboard=rows_kb),
        )
        return
    await message.answer(title_body, reply_markup=kb.loyalty_reply_keyboard(ui))
    if page_kb is not None:
        await message.answer("صفحه‌بندی:", reply_markup=page_kb)


async def open_referral_message(message: Message, session: AsyncSession, db_user: BotUser) -> None:
    """Legacy reply entry — invite is a subset of customer club."""
    await open_loyalty_referral_message(message, session, db_user, state=None, push=True)


# ---------------------------------------------------------------------------
# Staff reply-keyboard openers
# ---------------------------------------------------------------------------



async def _staff_loyalty_answer(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    text: str,
    *,
    can_tiers: bool,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
    content_inline: InlineKeyboardMarkup | None = None,
    content_caption: str | None = None,
) -> None:
    """Wave F: loyalty manage leaves — content/hub inline + stable main KB."""
    from app.bot.menu_nav import build_main_reply_keyboard
    from app.bot.nav_inline import admin_loyalty_manage_hub_keyboard, present_inline_only
    from app.bot.nav_mode import is_inline_nav
    from app.services.users import get_all_settings

    ui = await get_all_settings(session)
    if is_inline_nav(ui):
        await present_inline_only(
            message,
            text=text,
            inline=content_inline
            or admin_loyalty_manage_hub_keyboard(
                ui,
                include_tiers=can_tiers,
                back_callback=(
                    "nv:res:home" if is_reseller_bot else "nv:adm:people"
                ),
            ),
        )
        main_kb, _, _ = await build_main_reply_keyboard(
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
            ui=ui,
        )
        await message.answer(
            "از منوی پایین یا دکمه‌های بالا ادامه دهید.",
            reply_markup=main_kb,
        )
        return
    await message.answer(
        text,
        reply_markup=kb.admin_loyalty_reply_keyboard(None, include_tiers=can_tiers),
    )
    if content_inline is not None:
        await message.answer(
            content_caption or "·",
            reply_markup=content_inline,
        )


async def open_admin_loyalty_hub(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
    *,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
    push: bool = True,
) -> None:
    from app.bot import menu_nav as nav

    scope_info = await resolve_loyalty_manage_scope(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if scope_info is None:
        await message.answer("دسترسی به مدیریت باشگاه مشتریان ندارید.")
        return
    scope, can_tiers = scope_info
    await ensure_loyalty_defaults(session, reseller_id=scope)
    enabled = await loyalty_enabled(session, reseller_id=scope)
    status = "فعال ✅" if enabled else "غیرفعال ⛔️"
    who = "فروشگاه شما" if scope is not None else "پلتفرم"
    extra = ""
    if not can_tiers:
        extra = "\nسطح‌ها سراسری‌اند و فقط ادمین پلتفرم مدیریت می‌کند."
    from app.bot.nav_mode import is_inline_nav
    from app.bot.nav_inline import (
        admin_loyalty_manage_hub_keyboard,
        present_inline_only,
    )
    from app.services.users import get_all_settings

    ui = await get_all_settings(session)
    body = (
        f"⭐ <b>باشگاه مشتریان</b> ({who})\n"
        f"وضعیت: <b>{status}</b>\n"
        + (
            "یک بخش را از دکمه‌های زیر انتخاب کنید."
            if is_inline_nav(ui)
            else "از کیبورد پایین بخش را انتخاب کنید؛ جزئیات زیر پیام اینلاین است."
        )
        + f"{extra}"
    )
    if is_inline_nav(ui):
        if state is not None:
            await nav.set_nav_level(state, nav.NAV_ADMIN_LOYALTY, push=push)
        await present_inline_only(
            message,
            text=body,
            inline=admin_loyalty_manage_hub_keyboard(
                ui,
                include_tiers=can_tiers,
                back_callback=(
                    "nv:res:home" if is_reseller_bot else "nv:adm:people"
                ),
            ),
        )
        return
    await nav.show_nav_keyboard(
        message,
        session,
        db_user,
        nav.NAV_ADMIN_LOYALTY,
        text=body,
        state=state,
        push=push,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )


async def open_admin_loyalty_overview(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    *,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    scope_info = await resolve_loyalty_manage_scope(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if scope_info is None:
        await message.answer("دسترسی ندارید.")
        return
    scope, can_tiers = scope_info
    await ensure_loyalty_defaults(session, reseller_id=scope)
    metrics = await overview_metrics(session, reseller_id=scope)
    enabled = await loyalty_enabled(session, reseller_id=scope)
    lines = [
        kv_line("🔘", "وضعیت", "فعال" if enabled else "غیرفعال"),
        kv_line("👥", "دعوت‌ها", str(metrics.get("total_referrals") or 0)),
        kv_line("✅", "واجد شرایط", str(metrics.get("qualified_referrals") or 0)),
        kv_line("⭐", "امتیاز صادرشده", str(metrics.get("points_issued") or 0)),
        kv_line("♻️", "امتیاز بازخرید", str(metrics.get("points_redeemed") or 0)),
        kv_line("💰", "اعتبار کیف از جایزه", str(metrics.get("wallet_credits_issued") or 0)),
        kv_line("🎁", "جوایز فعال", str(metrics.get("active_rewards") or 0)),
    ]
    await _staff_loyalty_answer(
        message,
        session,
        db_user,
        format_message("📊 نمای کلی باشگاه", "\n".join(lines)),
        can_tiers=can_tiers,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )


async def _scoped_rules(session: AsyncSession, scope: int | None) -> list[PointsRule]:
    if scope is None:
        q = select(PointsRule).where(PointsRule.reseller_id.is_(None))
    else:
        q = select(PointsRule).where(PointsRule.reseller_id == int(scope))
    return list(
        (
            await session.execute(q.order_by(PointsRule.sort_order.asc(), PointsRule.id.asc()))
        ).scalars().all()
    )


async def _scoped_rewards(session: AsyncSession, scope: int | None) -> list[LoyaltyReward]:
    if scope is None:
        q = select(LoyaltyReward).where(LoyaltyReward.reseller_id.is_(None))
    else:
        q = select(LoyaltyReward).where(LoyaltyReward.reseller_id == int(scope))
    return list(
        (
            await session.execute(
                q.order_by(LoyaltyReward.sort_order.asc(), LoyaltyReward.id.asc())
            )
        ).scalars().all()
    )


def _staff_rules_markup(rules: list[PointsRule]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for r in rules[:20]:
        mark = "✅" if r.enabled else "⛔️"
        label = EVENT_LABELS.get(r.event_key, r.name)[:28]
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark} {label}",
                    callback_data=f"loyadm:rule:tog:{r.id}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="🔄 تازه‌سازی", callback_data="loyadm:rules")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _staff_rewards_markup(rewards: list[LoyaltyReward]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for r in rewards[:20]:
        if r.archived:
            continue
        mark = "✅" if r.enabled else "⛔️"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark} {r.name[:28]}",
                    callback_data=f"loyadm:rew:tog:{r.id}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="🔄 تازه‌سازی", callback_data="loyadm:rewards")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def open_admin_loyalty_rules(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    *,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    scope_info = await resolve_loyalty_manage_scope(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if scope_info is None:
        await message.answer("دسترسی ندارید.")
        return
    scope, can_tiers = scope_info
    await ensure_loyalty_defaults(session, reseller_id=scope)
    rules = await _scoped_rules(session, scope)
    if not rules:
        body = "قانونی تعریف نشده — پیش‌فرض‌ها را از وب‌پنل هم می‌توانید ببینید."
    else:
        lines = []
        for r in rules:
            ev = EVENT_LABELS.get(r.event_key, r.event_key)
            mode = "به‌ازای گیگ" if r.amount_mode == "per_gb" else "ثابت"
            st = "فعال" if r.enabled else "خاموش"
            lines.append(f"• <b>{r.name}</b>\n  {ev} · {mode}: {r.amount} · {st}")
        body = "\n".join(lines)
    await _staff_loyalty_answer(
        message,
        session,
        db_user,
        format_message(
            "📐 قوانین امتیاز",
            body + "\n\nبرای روشن/خاموش کردن از دکمه‌های زیر استفاده کنید.",
        ),
        can_tiers=can_tiers,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
        content_inline=_staff_rules_markup(rules),
        content_caption="قوانین:",
    )


async def open_admin_loyalty_rewards(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    *,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    scope_info = await resolve_loyalty_manage_scope(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if scope_info is None:
        await message.answer("دسترسی ندارید.")
        return
    scope, can_tiers = scope_info
    await ensure_loyalty_defaults(session, reseller_id=scope)
    rewards = await _scoped_rewards(session, scope)
    active = [r for r in rewards if not r.archived]
    if not active:
        body = "جایزه‌ای نیست."
    else:
        lines = []
        for r in active:
            tl = REWARD_TYPE_LABELS.get(r.reward_type, r.reward_type)
            st = "فعال" if r.enabled else "خاموش"
            lines.append(
                f"• <b>{r.name}</b>\n  {tl}: {r.reward_value} · {r.points_cost} امتیاز · {st}"
            )
        body = "\n".join(lines)
    await _staff_loyalty_answer(
        message,
        session,
        db_user,
        format_message(
            "🎁 جوایز باشگاه",
            body + "\n\nافزودن/ویرایش کامل از وب‌پنل؛ اینجا روشن/خاموش.",
        ),
        can_tiers=can_tiers,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
        content_inline=_staff_rewards_markup(rewards),
        content_caption="جوایز:",
    )


async def open_admin_loyalty_tiers(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    *,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    scope_info = await resolve_loyalty_manage_scope(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if scope_info is None:
        await message.answer("دسترسی ندارید.")
        return
    scope, can_tiers = scope_info
    if not can_tiers:
        await _staff_loyalty_answer(
            message,
            session,
            db_user,
            "سطح‌ها سراسری‌اند و فقط ادمین پلتفرم می‌تواند آن‌ها را مدیریت کند.",
            can_tiers=False,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        return
    await ensure_loyalty_defaults(session, reseller_id=scope)
    tiers = list(
        (
            await session.execute(
                select(LoyaltyTier).order_by(LoyaltyTier.sort_order.asc(), LoyaltyTier.id.asc())
            )
        ).scalars().all()
    )
    if not tiers:
        body = "سطحی تعریف نشده."
    else:
        lines = []
        for t in tiers:
            mx = "∞" if t.max_points is None else str(t.max_points)
            lines.append(
                f"• <b>{t.name}</b> — {t.min_points} تا {mx}"
                f" · ضریب {int(t.multiplier_bps or 10000) / 100:.0f}٪"
            )
        body = "\n".join(lines)
    await _staff_loyalty_answer(
        message,
        session,
        db_user,
        format_message(
            "🏅 سطوح باشگاه",
            body + "\n\nویرایش سطوح فقط از وب‌پنل (ادمین پلتفرم).",
        ),
        can_tiers=True,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )


def _staff_settings_markup(*, enabled: bool) -> InlineKeyboardMarkup:
    mark = "✅ فعال" if enabled else "⛔️ غیرفعال"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"باشگاه: {mark}",
                    callback_data="loyadm:tog:enabled",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✏️ نرخ امتیاز→کیف پول",
                    callback_data="loyadm:edit:rate",
                )
            ],
            [InlineKeyboardButton(text="🔄 تازه‌سازی", callback_data="loyadm:settings")],
        ]
    )


async def open_admin_loyalty_settings(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    *,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    scope_info = await resolve_loyalty_manage_scope(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if scope_info is None:
        await message.answer("دسترسی ندارید.")
        return
    scope, can_tiers = scope_info
    await ensure_loyalty_defaults(session, reseller_id=scope)
    enabled = await loyalty_enabled(session, reseller_id=scope)
    rate = await get_setting(
        session, SETTING_POINTS_TO_WALLET_RATE, "100", reseller_id=scope
    )
    body = "\n".join(
        [
            kv_line("🔘", "باشگاه", "فعال" if enabled else "غیرفعال"),
            kv_line("💱", "نرخ امتیاز→تومان", str(rate)),
            "",
            "تغییر وضعیت و نرخ از دکمه‌های اینلاین؛ جزئیات بیشتر در وب‌پنل.",
        ]
    )
    await _staff_loyalty_answer(
        message,
        session,
        db_user,
        format_message("⚙️ تنظیمات باشگاه", body),
        can_tiers=can_tiers,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
        content_inline=_staff_settings_markup(enabled=enabled),
        content_caption="تنظیمات:",
    )


async def open_admin_loyalty_ref_text(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    *,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    scope_info = await resolve_loyalty_manage_scope(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if scope_info is None:
        await message.answer("دسترسی ندارید.")
        return
    scope, can_tiers = scope_info
    # Reseller may edit referral text only with shop_settings (same as web panel)
    if scope is not None:
        from app.services.reseller_access import load_reseller_actor
        from app.services.resellers import has_bot_perm

        _, profile = await load_reseller_actor(
            session,
            db_user,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        if not profile or not (
            has_bot_perm(profile, "loyalty") and has_bot_perm(profile, "shop_settings")
        ):
            await _staff_loyalty_answer(
                message,
                session,
                db_user,
                "ویرایش متن دعوت نیاز به دسترسی «تنظیمات فروشگاه» هم دارد.",
                can_tiers=False,
                is_reseller_bot=is_reseller_bot,
                reseller_owner_id=reseller_owner_id,
            )
            return
    from app.services.rich_text import rich_plain_text

    cur = await get_setting(session, "referral_text", "", reseller_id=scope)
    preview = (rich_plain_text(cur) or "").strip() or "—"
    if len(preview) > 400:
        preview = preview[:399] + "…"
    await message.answer(
        format_message(
            "📝 متن دعوت",
            f"فعلی:\n<code>{preview}</code>\n\n"
            "متغیرها: <code>{code}</code> و <code>{link}</code>\n"
            "متن جدید را بفرستید یا «انصراف» بزنید.\n"
            "<i>ایموجی پریمیوم از همین‌جا حفظ می‌شود.</i>",
        ),
        reply_markup=kb.cancel_reply(),
    )
    await state.set_state(LoyaltyManageStates.edit_referral_text)
    await state.update_data(
        _loy_edit_scope=scope if scope is not None else 0,
        _loy_edit_is_shop=1 if scope is not None else 0,
        _loy_edit_reseller_bot=1 if is_reseller_bot else 0,
        _loy_edit_owner=int(reseller_owner_id or 0),
    )
    _ = can_tiers


# ---------------------------------------------------------------------------
# Customer callbacks (legacy inline)
# ---------------------------------------------------------------------------


@router.callback_query(F.data == "ref:home")
async def referral_home(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    await callback.answer()
    uname = await _bot_username(callback)
    text, link, send_kw = await build_referral_text(session, db_user, uname)
    share = f"https://t.me/share/url?url={link}&text="
    if callback.message:
        await safe_edit_text(
            callback.message,
            text,
            reply_markup=_ref_actions_keyboard(share_url=share),
            **send_kw,
        )


@router.callback_query(F.data == "ref:stats")
async def referral_stats_cb(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    await callback.answer()
    stats = await referral_stats(session, db_user.id)
    body = "\n".join(
        [
            kv_line("👤", "کل دعوت‌شده‌ها", str(stats["total"])),
            kv_line("✅", "واجد شرایط", str(stats["qualified"])),
            kv_line("⭐", "امتیاز کسب‌شده", str(stats["earned_points"])),
        ]
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="ref:home")],
        ]
    )
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message("📊 آمار دعوت", body),
            reply_markup=markup,
        )


@router.callback_query(F.data == "loy:home")
async def loyalty_home(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    """Legacy/deep-link entry — content + discounts extra only (nav = reply KB)."""
    await callback.answer()
    if not await loyalty_enabled(session, reseller_id=db_user.reseller_id):
        if callback.message:
            await safe_edit_text(
                callback.message,
                format_message("⭐ باشگاه مشتریان", "این بخش فعلاً غیرفعال است."),
                reply_markup=None,
            )
        return
    text = await build_loyalty_text(session, db_user)
    if callback.message:
        await safe_edit_text(
            callback.message,
            text,
            reply_markup=_points_extras_keyboard(),
        )


@router.callback_query(F.data == "loy:rewards")
async def loyalty_rewards(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    await callback.answer()
    await session.refresh(db_user)
    rewards = await list_active_rewards(session, reseller_id=db_user.reseller_id)
    lines = [kv_line("⭐", "امتیاز فعلی", f"<b>{int(db_user.points_balance or 0)}</b>"), ""]
    if not rewards:
        lines.append("هنوز جایزه‌ای تعریف نشده است.")
    else:
        for r in rewards:
            type_label = REWARD_TYPE_LABELS.get(r.reward_type, r.reward_type)
            desc = (r.description or "").strip()
            lines.append(
                f"• <b>{r.name}</b>\n"
                f"  {type_label}: {r.reward_value} · هزینه: {r.points_cost} امتیاز"
                + (f"\n  <i>{desc}</i>" if desc else "")
            )
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message("🎁 جوایز", "\n".join(lines)),
            reply_markup=_rewards_redeem_keyboard(rewards, include_back=True),
        )


@router.callback_query(F.data.startswith("loy:redeem:"))
async def loyalty_redeem(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    try:
        reward_id = int(callback.data.split(":")[-1])
    except (TypeError, ValueError):
        await callback.answer("نامعتبر", show_alert=True)
        return

    reward = await session.get(LoyaltyReward, reward_id)
    if not reward or not reward.enabled or reward.archived:
        await callback.answer("جایزه فعال نیست", show_alert=True)
        return

    if reward.reward_type in ("traffic_gb", "time_days"):
        services = list(
            (
                await session.execute(
                    select(UserService)
                    .where(UserService.bot_user_id == db_user.id)
                    .order_by(UserService.id.desc())
                    .limit(10)
                )
            ).scalars().all()
        )
        if not services:
            await callback.answer("سرویس فعالی ندارید", show_alert=True)
            return
        if len(services) == 1:
            await _do_redeem(callback, session, db_user, reward_id, services[0].id)
            return
        rows = [
            [
                InlineKeyboardButton(
                    text=s.pg_username or f"سرویس #{s.id}",
                    callback_data=f"loy:redeemsvc:{reward_id}:{s.id}",
                )
            ]
            for s in services
        ]
        rows.append([InlineKeyboardButton(text="انصراف", callback_data="loy:rewards")])
        await callback.answer()
        if callback.message:
            await safe_edit_text(
                callback.message,
                format_message("🎁 انتخاب سرویس", "سرویسی که جایزه روی آن اعمال شود را انتخاب کنید:"),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )
        return

    await _do_redeem(callback, session, db_user, reward_id, None)


@router.callback_query(F.data.startswith("loy:redeemsvc:"))
async def loyalty_redeem_svc(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    parts = (callback.data or "").split(":")
    try:
        reward_id = int(parts[2])
        service_id = int(parts[3])
    except (IndexError, TypeError, ValueError):
        await callback.answer("نامعتبر", show_alert=True)
        return
    await _do_redeem(callback, session, db_user, reward_id, service_id)


async def _do_redeem(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    reward_id: int,
    service_id: int | None,
) -> None:
    # Stable slot key (not callback.id): double-tap shares the same next slot
    # so uniqueness + IntegrityError replay prevent double spend.
    from sqlalchemy import func, select

    from app.db.models import RewardRedemption

    used = int(
        (
            await session.execute(
                select(func.count())
                .select_from(RewardRedemption)
                .where(
                    RewardRedemption.user_id == int(db_user.id),
                    RewardRedemption.reward_id == int(reward_id),
                )
            )
        ).scalar_one()
        or 0
    )
    key = f"tg:redeem:{db_user.id}:{reward_id}:{service_id or 0}:{used}"
    try:
        red = await redeem_reward(
            session,
            db_user,
            reward_id,
            service_id=service_id,
            idempotency_key=key,
        )
    except ValueError as e:
        await callback.answer(user_safe_error(e, limit=180), show_alert=True)
        return
    except Exception:
        await callback.answer("خطا در بازخرید. دوباره تلاش کنید.", show_alert=True)
        return
    await callback.answer("جایزه با موفقیت دریافت شد ✅", show_alert=True)
    await session.refresh(db_user)
    type_label = REWARD_TYPE_LABELS.get(red.reward_type, red.reward_type)
    lines = [
        "جایزه اعمال شد.",
        kv_line("🎁", "نوع", type_label),
        kv_line("📦", "مقدار", str(red.reward_value)),
        kv_line("⭐", "امتیاز باقی‌مانده", str(int(db_user.points_balance or 0))),
    ]
    if red.reward_type == "discount_percent" and red.discount_code:
        lines.extend(
            [
                "",
                kv_line("🏷", "کد تخفیف", f"<code>{red.discount_code}</code>"),
                "در خرید بعدی از دکمه «کد تخفیف» استفاده کنید.",
            ]
        )
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message("✅ بازخرید موفق", "\n".join(lines)),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="⬅️ بازگشت به جوایز",
                            callback_data="loy:rewards",
                        )
                    ],
                ]
            ),
        )




@router.callback_query(F.data == "loy:wheel:hub")
async def loyalty_wheel_hub(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    await callback.answer()
    if not await wheel_feature_enabled(session, reseller_id=db_user.reseller_id):
        if callback.message:
            await safe_edit_text(
                callback.message,
                format_message("🎡 چرخ شانس", "چرخ شانس فعلاً غیرفعال است."),
                reply_markup=None,
            )
        return
    body, can_spin = await _build_wheel_hub_text(session, db_user)
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message("🎡 چرخ شانس", body),
            reply_markup=_wheel_spin_keyboard(can_spin=can_spin),
        )


@router.callback_query(F.data == "loy:wheel:spin")
async def loyalty_wheel_spin(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    """Server-side spin — stable slot key (not callback.id) prevents double-tap."""
    if not await wheel_feature_enabled(session, reseller_id=db_user.reseller_id):
        await callback.answer("چرخ شانس غیرفعال است", show_alert=True)
        return
    from sqlalchemy import func, select

    from app.db.models import LuckyWheelSpin

    spins_done = int(
        (
            await session.execute(
                select(func.count())
                .select_from(LuckyWheelSpin)
                .where(LuckyWheelSpin.user_id == int(db_user.id))
            )
        ).scalar_one()
        or 0
    )
    key = f"tg:wheel:{db_user.id}:{spins_done}"
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message("🎡 چرخ شانس", "در حال چرخش…"),
            reply_markup=None,
        )
    try:
        result = await wheel_spin(session, db_user, idempotency_key=key)
    except ValueError as e:
        await callback.answer(user_safe_error(e, limit=180), show_alert=True)
        body, can_spin = await _build_wheel_hub_text(session, db_user)
        if callback.message:
            await safe_edit_text(
                callback.message,
                format_message("🎡 چرخ شانس", body),
                reply_markup=_wheel_spin_keyboard(can_spin=can_spin),
            )
        return
    except Exception:
        await callback.answer("خطا در چرخش. دوباره تلاش کنید.", show_alert=True)
        return

    await callback.answer("نتیجه آماده است ✅")
    await session.refresh(db_user)
    type_label = WHEEL_PRIZE_TYPE_LABELS.get(result.prize_type, result.prize_type)
    lines = [
        "نتیجه چرخش:",
        kv_line("🏷", "جایزه", html.escape(result.prize_label or "—")),
        kv_line("🎁", "نوع", html.escape(type_label)),
    ]
    if result.prize_type != "none":
        lines.append(kv_line("📦", "مقدار", str(result.prize_value)))
    if result.used_free_spin:
        lines.append(kv_line("🎟", "هزینه", "چرخش رایگان"))
    elif result.cost_points:
        lines.append(kv_line("🎟", "هزینه", f"{result.cost_points} امتیاز"))
    else:
        lines.append(kv_line("🎟", "هزینه", "رایگان"))
    lines.append(kv_line("⭐", "امتیاز باقی‌مانده", str(int(db_user.points_balance or 0))))
    if result.discount_code:
        lines.extend(
            [
                "",
                kv_line("🏷", "کد تخفیف", f"<code>{html.escape(result.discount_code)}</code>"),
                "در خرید بعدی از دکمه «کد تخفیف» استفاده کنید.",
            ]
        )
    body, can_spin = await _build_wheel_hub_text(session, db_user)
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message("🎡 نتیجه چرخ شانس", "\n".join(lines) + "\n\n" + body),
            reply_markup=_wheel_spin_keyboard(can_spin=can_spin),
        )

@router.callback_query(F.data == "loy:discounts")
async def loyalty_discounts(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    await callback.answer()
    ents = await list_available_discounts(session, db_user.id)
    if not ents:
        body = "تخفیف فعالی ندارید.\nاز بخش جوایز یک تخفیف بازخرید کنید."
    else:
        lines = []
        for e in ents:
            exp = "بدون انقضا"
            if e.expires_at:
                exp = e.expires_at.strftime("%Y-%m-%d")
            max_d = (
                format_toman(int(e.max_discount_toman), get_settings().currency)
                if e.max_discount_toman is not None
                else "بدون سقف"
            )
            min_p = (
                format_toman(int(e.min_purchase_toman), get_settings().currency)
                if int(e.min_purchase_toman or 0) > 0
                else "—"
            )
            lines.append(
                "\n".join(
                    [
                        f"• <b>{e.percent}٪</b> — کد: <code>{e.code}</code>",
                        f"  انقضا: {exp}",
                        f"  حداقل خرید: {min_p}",
                        f"  سقف تخفیف: {max_d}",
                    ]
                )
            )
        body = "\n\n".join(lines) + "\n\nدر صفحه پرداخت سفارش، کد را وارد کنید."
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message("🏷 تخفیف‌های من", body),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="loy:home")],
                ]
            ),
        )


@router.callback_query(F.data.startswith("loy:hist:"))
async def loyalty_history(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    await callback.answer()
    try:
        page = int((callback.data or "loy:hist:0").split(":")[-1])
    except ValueError:
        page = 0
    page = max(0, page)
    limit = 8
    rows = await list_points_history(session, db_user.id, limit=limit, offset=page * limit)
    await session.refresh(db_user)
    if not rows:
        body = "تاریخچه‌ای نیست."
    else:
        lines = []
        for t in rows:
            sign = "+" if t.amount >= 0 else ""
            lines.append(f"{sign}{t.amount} — {t.description or t.tx_type}")
        body = "\n".join(lines)
    body += f"\n\nموجودی فعلی: <b>{int(db_user.points_balance or 0)}</b>"
    page_kb = _hist_page_keyboard(
        page=page, has_more=len(rows) >= limit, include_back=True
    )
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message("📜 تاریخچه امتیاز", body),
            reply_markup=page_kb,
        )


# ---------------------------------------------------------------------------
# Staff inline callbacks
# ---------------------------------------------------------------------------


async def _staff_scope_from_callback(
    session: AsyncSession,
    db_user: BotUser,
    *,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> tuple[int | None, bool] | None:
    return await resolve_loyalty_manage_scope(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )


@router.callback_query(F.data == "loyadm:tog:enabled")
async def staff_tog_enabled(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    scope_info = await _staff_scope_from_callback(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if scope_info is None:
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    scope, _ = scope_info
    cur = await get_setting(session, SETTING_LOYALTY_ENABLED, "1", reseller_id=scope)
    new_val = "0" if on(cur) else "1"
    await set_setting(session, SETTING_LOYALTY_ENABLED, new_val, reseller_id=scope)
    await callback.answer("ذخیره شد")
    if callback.message:
        await safe_edit_text(
            callback.message,
            "تنظیمات باشگاه به‌روز شد.",
            reply_markup=_staff_settings_markup(enabled=new_val == "1"),
        )


@router.callback_query(F.data == "loyadm:settings")
async def staff_settings_refresh(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    await callback.answer()
    scope_info = await _staff_scope_from_callback(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if scope_info is None or not callback.message:
        return
    scope, _ = scope_info
    enabled = await loyalty_enabled(session, reseller_id=scope)
    rate = await get_setting(
        session, SETTING_POINTS_TO_WALLET_RATE, "100", reseller_id=scope
    )
    body = "\n".join(
        [
            kv_line("🔘", "باشگاه", "فعال" if enabled else "غیرفعال"),
            kv_line("💱", "نرخ امتیاز→تومان", str(rate)),
        ]
    )
    await safe_edit_text(
        callback.message,
        format_message("⚙️ تنظیمات باشگاه", body),
        reply_markup=_staff_settings_markup(enabled=enabled),
    )


@router.callback_query(F.data == "loyadm:edit:rate")
async def staff_edit_rate_start(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    scope_info = await _staff_scope_from_callback(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if scope_info is None:
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    scope, _ = scope_info
    cur = await get_setting(
        session, SETTING_POINTS_TO_WALLET_RATE, "100", reseller_id=scope
    )
    await callback.answer()
    await state.set_state(LoyaltyManageStates.edit_wallet_rate)
    await state.update_data(
        _loy_edit_scope=scope if scope is not None else 0,
        _loy_edit_is_shop=1 if scope is not None else 0,
        _loy_edit_reseller_bot=1 if is_reseller_bot else 0,
        _loy_edit_owner=int(reseller_owner_id or 0),
    )
    if callback.message:
        await callback.message.answer(
            f"نرخ فعلی: <code>{cur}</code>\nعدد جدید (تومان به‌ازای ۱ امتیاز) را بفرستید:",
            reply_markup=kb.cancel_reply(),
        )


@router.callback_query(F.data.startswith("loyadm:rule:tog:"))
async def staff_rule_toggle(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    scope_info = await _staff_scope_from_callback(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if scope_info is None:
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    scope, _ = scope_info
    try:
        rid = int((callback.data or "").split(":")[-1])
    except ValueError:
        await callback.answer("نامعتبر", show_alert=True)
        return
    rule = await session.get(PointsRule, rid)
    if not rule or not _rule_in_scope(rule, scope):
        await callback.answer("این قانون در محدوده شما نیست", show_alert=True)
        return
    rule.enabled = not bool(rule.enabled)
    await session.commit()
    await callback.answer("ذخیره شد")
    rules = await _scoped_rules(session, scope)
    if callback.message:
        await safe_edit_text(
            callback.message,
            "قوانین (لمس برای روشن/خاموش):",
            reply_markup=_staff_rules_markup(rules),
        )


@router.callback_query(F.data.startswith("loyadm:rew:tog:"))
async def staff_reward_toggle(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    scope_info = await _staff_scope_from_callback(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if scope_info is None:
        await callback.answer("دسترسی ندارید", show_alert=True)
        return
    scope, _ = scope_info
    try:
        rid = int((callback.data or "").split(":")[-1])
    except ValueError:
        await callback.answer("نامعتبر", show_alert=True)
        return
    reward = await session.get(LoyaltyReward, rid)
    if not reward or not _reward_in_scope(reward, scope):
        await callback.answer("این جایزه در محدوده شما نیست", show_alert=True)
        return
    reward.enabled = not bool(reward.enabled)
    await session.commit()
    await callback.answer("ذخیره شد")
    rewards = await _scoped_rewards(session, scope)
    if callback.message:
        await safe_edit_text(
            callback.message,
            "جوایز (لمس برای روشن/خاموش):",
            reply_markup=_staff_rewards_markup(rewards),
        )


@router.callback_query(F.data == "loyadm:rules")
async def staff_rules_refresh(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    await callback.answer()
    scope_info = await _staff_scope_from_callback(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if scope_info is None or not callback.message:
        return
    scope, _ = scope_info
    rules = await _scoped_rules(session, scope)
    await safe_edit_text(
        callback.message,
        "قوانین (لمس برای روشن/خاموش):",
        reply_markup=_staff_rules_markup(rules),
    )


@router.callback_query(F.data == "loyadm:rewards")
async def staff_rewards_refresh(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    await callback.answer()
    scope_info = await _staff_scope_from_callback(
        session,
        db_user,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
    if scope_info is None or not callback.message:
        return
    scope, _ = scope_info
    rewards = await _scoped_rewards(session, scope)
    await safe_edit_text(
        callback.message,
        "جوایز (لمس برای روشن/خاموش):",
        reply_markup=_staff_rewards_markup(rewards),
    )


@router.message(LoyaltyManageStates.edit_wallet_rate)
async def staff_save_wallet_rate(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    text = (message.text or "").strip()
    data = await state.get_data()
    shop = bool(data.get("_loy_edit_is_shop"))
    scope = int(data.get("_loy_edit_scope") or 0) if shop else None
    # Re-check ACL — never trust FSM alone
    scope_info = await resolve_loyalty_manage_scope(
        session,
        db_user,
        is_reseller_bot=bool(data.get("_loy_edit_reseller_bot")) or is_reseller_bot,
        reseller_owner_id=int(data.get("_loy_edit_owner") or 0) or reseller_owner_id,
    )
    if scope_info is None:
        await state.clear()
        await message.answer("دسترسی ندارید.")
        return
    live_scope, can_tiers = scope_info
    if live_scope != scope:
        await state.clear()
        await message.answer("محدوده تنظیمات نامعتبر است.")
        return
    if kb.is_cancel_text(text):
        await state.clear()
        await _staff_loyalty_answer(
            message,
            session,
            db_user,
            "لغو شد.",
            can_tiers=can_tiers,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        return
    raw = text.replace(",", "").replace("٬", "").strip()
    try:
        rate = int(raw)
        if rate < 0:
            raise ValueError("neg")
    except ValueError:
        await message.answer("عدد معتبر بفرستید.")
        return
    await set_setting(
        session, SETTING_POINTS_TO_WALLET_RATE, str(rate), reseller_id=scope
    )
    await state.clear()
    await _staff_loyalty_answer(
        message,
        session,
        db_user,
        f"نرخ ذخیره شد: <b>{rate}</b>",
        can_tiers=can_tiers,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )


@router.message(LoyaltyManageStates.edit_referral_text)
async def staff_save_referral_text(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    text = (message.text or "").strip()
    data = await state.get_data()
    shop = bool(data.get("_loy_edit_is_shop"))
    scope = int(data.get("_loy_edit_scope") or 0) if shop else None
    scope_info = await resolve_loyalty_manage_scope(
        session,
        db_user,
        is_reseller_bot=bool(data.get("_loy_edit_reseller_bot")) or is_reseller_bot,
        reseller_owner_id=int(data.get("_loy_edit_owner") or 0) or reseller_owner_id,
    )
    if scope_info is None:
        await state.clear()
        await message.answer("دسترسی ندارید.")
        return
    live_scope, can_tiers = scope_info
    if live_scope != scope:
        await state.clear()
        await message.answer("محدوده تنظیمات نامعتبر است.")
        return
    if scope is not None:
        from app.services.reseller_access import load_reseller_actor
        from app.services.resellers import has_bot_perm

        _, profile = await load_reseller_actor(
            session,
            db_user,
            is_reseller_bot=True,
            reseller_owner_id=live_scope,
        )
        if not profile or not has_bot_perm(profile, "shop_settings"):
            await state.clear()
            await message.answer("دسترسی تنظیمات فروشگاه ندارید.")
            return
    if kb.is_cancel_text(text):
        await state.clear()
        await _staff_loyalty_answer(
            message,
            session,
            db_user,
            "لغو شد.",
            can_tiers=can_tiers,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
        return
    if not text:
        await message.answer("متن خالی نباشد.")
        return
    from app.services.rich_text import pack_setting_from_message

    packed = pack_setting_from_message("referral_text", message)
    await set_setting(session, "referral_text", packed, reseller_id=scope)
    await state.clear()
    await _staff_loyalty_answer(
        message,
        session,
        db_user,
        "متن دعوت ذخیره شد ✅",
        can_tiers=can_tiers,
        is_reseller_bot=is_reseller_bot,
        reseller_owner_id=reseller_owner_id,
    )
