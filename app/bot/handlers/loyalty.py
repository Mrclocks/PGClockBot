"""Telegram Referral + Loyalty / Points UX (extends existing ref:home entry)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.tg_utils import safe_edit_text
from app.config import get_settings
from app.db.models import BotUser, UserService
from app.services.formatting import format_message, format_toman, kv_line
from app.services.loyalty import (
    REWARD_TYPE_LABELS,
    ensure_loyalty_defaults,
    get_tier_for_points,
    list_active_rewards,
    list_available_discounts,
    list_points_history,
    loyalty_enabled,
    month_earned_points,
    redeem_reward,
    referral_link,
    referral_stats,
)
from app.services.shortcodes import render_user_message
from app.services.users import get_all_settings

router = Router(name="loyalty")


def _ref_keyboard(*, share_url: str | None = None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if share_url:
        rows.append(
            [InlineKeyboardButton(text="📤 اشتراک‌گذاری لینک", url=share_url)]
        )
    rows.append(
        [
            InlineKeyboardButton(text="📊 آمار دعوت", callback_data="ref:stats"),
            InlineKeyboardButton(text="⭐ امتیاز من", callback_data="loy:home"),
        ]
    )
    rows.append([InlineKeyboardButton(text="🎁 جوایز", callback_data="loy:rewards")])
    rows.append([InlineKeyboardButton(text="🏠 خانه", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _loy_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🎁 جوایز", callback_data="loy:rewards"),
                InlineKeyboardButton(text="📜 تاریخچه", callback_data="loy:hist:0"),
            ],
            [InlineKeyboardButton(text="🏷 تخفیف‌های من", callback_data="loy:discounts")],
            [InlineKeyboardButton(text="👥 دعوت دوستان", callback_data="ref:home")],
            [InlineKeyboardButton(text="🏠 خانه", callback_data="menu:home")],
        ]
    )


async def _bot_username(callback_or_message) -> str:
    bot = callback_or_message.bot
    me = await bot.get_me()
    return me.username or get_settings().bot_username or "bot"


async def build_referral_text(session: AsyncSession, db_user: BotUser, uname: str) -> tuple[str, str]:
    ui = await get_all_settings(session)
    link = referral_link(uname, db_user.referral_code)
    try:
        await ensure_loyalty_defaults(session)
    except Exception:
        pass
    stats = await referral_stats(session, db_user.id)
    body = render_user_message(
        ui.get("referral_text"),
        f"کد دعوت: <code>{db_user.referral_code}</code>\n{link}",
        code=db_user.referral_code,
        link=link,
        referral_code=db_user.referral_code,
        referral_count=stats.get("qualified") or stats.get("total") or 0,
        points=getattr(db_user, "points_balance", None) or 0,
    )
    extra = "\n".join(
        [
            "",
            kv_line("👥", "دعوت‌های موفق", str(stats["qualified"] or stats["total"])),
            kv_line("⭐", "امتیاز از دعوت", str(stats["earned_points"])),
            "",
            f"لینک دعوت:\n<code>{link}</code>",
        ]
    )
    text = format_message("🎁 دعوت دوستان", body + extra)
    return text, link


async def build_loyalty_text(session: AsyncSession, db_user: BotUser) -> str:
    await session.refresh(db_user)
    try:
        await ensure_loyalty_defaults(session)
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


@router.callback_query(F.data == "ref:home")
async def referral_home(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    await callback.answer()
    uname = await _bot_username(callback)
    text, link = await build_referral_text(session, db_user, uname)
    share = f"https://t.me/share/url?url={link}&text="
    if callback.message:
        await safe_edit_text(
            callback.message,
            text,
            reply_markup=_ref_keyboard(share_url=share),
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
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="بازگشت", callback_data="ref:home")],
            [InlineKeyboardButton(text="🏠 خانه", callback_data="menu:home")],
        ]
    )
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message("📊 آمار دعوت", body),
            reply_markup=kb,
        )


@router.callback_query(F.data == "loy:home")
async def loyalty_home(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    await callback.answer()
    if not await loyalty_enabled(session, reseller_id=db_user.reseller_id):
        if callback.message:
            await safe_edit_text(
                callback.message,
                format_message("⭐ باشگاه مشتریان", "این بخش فعلاً غیرفعال است."),
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="🏠 خانه", callback_data="menu:home")]]
                ),
            )
        return
    text = await build_loyalty_text(session, db_user)
    if callback.message:
        await safe_edit_text(callback.message, text, reply_markup=_loy_keyboard())


@router.callback_query(F.data == "loy:rewards")
async def loyalty_rewards(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    await callback.answer()
    await session.refresh(db_user)
    rewards = await list_active_rewards(session, reseller_id=db_user.reseller_id)
    lines = [kv_line("⭐", "امتیاز فعلی", f"<b>{int(db_user.points_balance or 0)}</b>"), ""]
    rows: list[list[InlineKeyboardButton]] = []
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
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"دریافت «{r.name}»",
                        callback_data=f"loy:redeem:{r.id}",
                    )
                ]
            )
    rows.append([InlineKeyboardButton(text="بازگشت", callback_data="loy:home")])
    rows.append([InlineKeyboardButton(text="🏠 خانه", callback_data="menu:home")])
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message("🎁 جوایز", "\n".join(lines)),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )


@router.callback_query(F.data.startswith("loy:redeem:"))
async def loyalty_redeem(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    try:
        reward_id = int(callback.data.split(":")[-1])
    except (TypeError, ValueError):
        await callback.answer("نامعتبر", show_alert=True)
        return

    from app.db.models import LoyaltyReward

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
    key = f"tg:{callback.id}:{db_user.id}:{reward_id}:{service_id or 0}"
    try:
        red = await redeem_reward(
            session,
            db_user,
            reward_id,
            service_id=service_id,
            idempotency_key=key,
        )
    except ValueError as e:
        await callback.answer(str(e)[:180], show_alert=True)
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
            reply_markup=_loy_keyboard(),
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
                    [InlineKeyboardButton(text="بازگشت", callback_data="loy:home")],
                    [InlineKeyboardButton(text="🏠 خانه", callback_data="menu:home")],
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
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ قبلی", callback_data=f"loy:hist:{page - 1}"))
    if len(rows) >= limit:
        nav.append(InlineKeyboardButton(text="بعدی ▶️", callback_data=f"loy:hist:{page + 1}"))
    kb_rows: list[list[InlineKeyboardButton]] = []
    if nav:
        kb_rows.append(nav)
    kb_rows.append([InlineKeyboardButton(text="بازگشت", callback_data="loy:home")])
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message("📜 تاریخچه امتیاز", body),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        )


async def open_referral_message(message: Message, session: AsyncSession, db_user: BotUser) -> None:
    """Reply-keyboard entry — mirrors enhanced referral home."""
    from app.bot.menu_nav import build_main_reply_keyboard

    uname = await _bot_username(message)
    text, link = await build_referral_text(session, db_user, uname)
    share = f"https://t.me/share/url?url={link}&text="
    main_kb, _, _ = await build_main_reply_keyboard(session, db_user)
    await message.answer(text, reply_markup=main_kb)
    await message.answer(
        "گزینه‌ها:",
        reply_markup=_ref_keyboard(share_url=share),
    )
