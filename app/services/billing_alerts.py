"""Owner and reseller alerts for PAYG billing.

Low balance: a reseller is warned once per level as the wallet drains (see
``LOW_BALANCE_WARN_LEVELS``); the admin who owns the reseller gets the same
warning. Billing problems (a tick that cannot bill a reseller) are announced to
that admin once per episode.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timezone

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import or_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import BotUser, OrgPrincipal, ResellerProfile
from app.py_compat import StrEnum
from app.services.org_principals import DEPTH_OWNER, get_principal_by_reseller_profile
from app.services.platform_identity import deliverable_telegram_id
from app.services.reseller_access import parse_telegram_ids

# Opens the shop operator's billing screen (the reseller hub entry ``res_billing``).
BILLING_SCREEN_CALLBACK = "nv:res:ra:res_billing"

logger = logging.getLogger(__name__)

LOW_BALANCE_WARN_LEVELS: tuple[int, ...] = (100_000, 50_000)


class TickIssue(StrEnum):
    NO_ADMIN = "no_admin"
    ADMIN_MISSING = "admin_missing"
    PANEL_UNAVAILABLE = "panel_unavailable"
    NO_TRAFFIC_FIELD = "no_traffic_field"
    RATE_ZERO = "rate_zero"
    WALLET_SHORT = "wallet_short"


PANEL_TICK_ISSUES: tuple[TickIssue, ...] = (
    TickIssue.NO_ADMIN,
    TickIssue.ADMIN_MISSING,
    TickIssue.PANEL_UNAVAILABLE,
    TickIssue.NO_TRAFFIC_FIELD,
)

_TICK_ISSUE_TEXT: dict[TickIssue, str] = {
    TickIssue.NO_ADMIN: "هیچ ادمین پاسارگاردی به این نماینده متصل نیست",
    TickIssue.ADMIN_MISSING: "ادمین پاسارگارد این نماینده در پنل پیدا نشد",
    TickIssue.PANEL_UNAVAILABLE: "دریافت مصرف از پاسارگارد ناموفق بود",
    TickIssue.NO_TRAFFIC_FIELD: "پاسخ پاسارگارد فیلد مصرف ترافیک ندارد",
    TickIssue.RATE_ZERO: (
        "نرخ هر گیگ این نماینده صفر است؛ یک پلن PAYG با نرخ مثبت به او بدهید "
        "(مصرف تا آن زمان نگه داشته می‌شود و بعد از اصلاح حساب می‌شود)"
    ),
}


def warn_band(balance: int) -> int | None:
    """Return the deepest warning level the balance has fallen to, or None above all levels."""
    reached = [level for level in LOW_BALANCE_WARN_LEVELS if int(balance) <= level]
    return min(reached) if reached else None


async def claim_low_balance_warning(
    session: AsyncSession,
    profile: ResellerProfile,
    balance: int,
) -> int | None:
    """Atomically claim the next warning level; return it when a notice must be sent.

    The recorded level follows the balance back up after a top-up, so the next
    drop below a level warns again. Only a deeper level than the recorded one
    produces a notice, which keeps repeated ticks idempotent.
    """
    band = warn_band(balance)
    seen = profile.billing_low_warned_level
    if seen is not None and (band is None or band > seen):
        await _record_recovered_level(session, profile, expected=int(seen), level=band)
        return None
    if band is None or (seen is not None and band >= seen):
        return None

    result = await session.execute(
        update(ResellerProfile)
        .where(
            ResellerProfile.user_id == int(profile.user_id),
            or_(
                ResellerProfile.billing_low_warned_level.is_(None),
                ResellerProfile.billing_low_warned_level > band,
            ),
        )
        .values(
            billing_low_warned_level=band,
            billing_low_warned_at=datetime.now(timezone.utc),
        )
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    if result.rowcount != 1:
        return None
    await session.refresh(profile)
    return band


async def _record_recovered_level(
    session: AsyncSession,
    profile: ResellerProfile,
    *,
    expected: int,
    level: int | None,
) -> None:
    await session.execute(
        update(ResellerProfile)
        .where(
            ResellerProfile.user_id == int(profile.user_id),
            ResellerProfile.billing_low_warned_level == expected,
        )
        .values(billing_low_warned_level=level)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    await session.refresh(profile)


async def _owner_recipients(
    session: AsyncSession, profile: ResellerProfile
) -> tuple[int | None, list[int]]:
    """Return ``(parent_shop_user_id, chat_ids)`` for the admin who owns this reseller.

    A sub-representative reports to the representative that created it; every
    other reseller reports to the platform admins.
    """
    principal = await get_principal_by_reseller_profile(session, int(profile.id))
    parent = (
        await session.get(OrgPrincipal, int(principal.parent_id))
        if principal is not None and principal.parent_id
        else None
    )
    if parent is not None and int(parent.depth) != DEPTH_OWNER and parent.bot_user_id:
        parent_user = await session.get(BotUser, int(parent.bot_user_id))
        parent_profile = (
            await session.get(ResellerProfile, int(parent.reseller_profile_id))
            if parent.reseller_profile_id
            else None
        )
        chat_ids: set[int] = set()
        tid = deliverable_telegram_id(getattr(parent_user, "telegram_id", None))
        if tid is not None:
            chat_ids.add(tid)
        if parent_profile is not None:
            chat_ids |= parse_telegram_ids(parent_profile.bot_admin_ids)
        if chat_ids:
            return int(parent.bot_user_id), sorted(chat_ids)
    return None, [int(i) for i in get_settings().admin_ids]


async def _claim_issue(session: AsyncSession, profile: ResellerProfile, issue: TickIssue) -> bool:
    """Record ``issue`` on the shop once; True only for the call that recorded it."""
    result = await session.execute(
        update(ResellerProfile)
        .where(
            ResellerProfile.user_id == int(profile.user_id),
            or_(
                ResellerProfile.billing_tick_issue.is_(None),
                ResellerProfile.billing_tick_issue != issue.value,
            ),
        )
        .values(billing_tick_issue=issue.value)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    if result.rowcount != 1:
        return False
    await session.refresh(profile)
    return True


async def report_tick_issue(
    session: AsyncSession,
    profile: ResellerProfile,
    issue: TickIssue,
) -> bool:
    """Tell the owner admin about a billing problem once per episode; True when announced."""
    if not await _claim_issue(session, profile, issue):
        return False
    owner_shop_id, owner_chat_ids = await _owner_recipients(session, profile)
    await _send(
        session, owner_shop_id, owner_chat_ids, _tick_issue_text(profile, issue),
        markup=_reseller_card_markup(profile, owner_shop_id),
    )
    return True


async def clear_tick_issue(
    session: AsyncSession,
    profile: ResellerProfile,
    issues: tuple[TickIssue, ...],
) -> None:
    """Forget a resolved problem so the same one is announced again if it returns."""
    if profile.billing_tick_issue not in {issue.value for issue in issues}:
        return
    await session.execute(
        update(ResellerProfile)
        .where(
            ResellerProfile.user_id == int(profile.user_id),
            ResellerProfile.billing_tick_issue.in_([issue.value for issue in issues]),
        )
        .values(billing_tick_issue=None)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    await session.refresh(profile)


WALLET_SHORT_RESELLER_TEXT = (
    "⚠️ <b>موجودی کیف پول برای هزینهٔ PAYG کافی نیست</b>\n"
    "فروش و تحویل سرویس تازه تا شارژ کیف پول انجام نمی‌شود."
)


async def announce_wallet_short(session: AsyncSession, profile: ResellerProfile) -> bool:
    """Tell the reseller and their owner once per episode that sales stopped for lack of funds."""
    if not await _claim_issue(session, profile, TickIssue.WALLET_SHORT):
        return False
    reseller = await session.get(BotUser, int(profile.user_id))
    reseller_tid = deliverable_telegram_id(getattr(reseller, "telegram_id", None))
    if reseller_tid is not None:
        await _send(
            session, int(profile.user_id), [reseller_tid], WALLET_SHORT_RESELLER_TEXT,
            markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="💳 صورتحساب و شارژ", callback_data=BILLING_SCREEN_CALLBACK),
            ]]),
        )
    owner_shop_id, owner_chat_ids = await _owner_recipients(session, profile)
    await _send(
        session, owner_shop_id, owner_chat_ids,
        "⚠️ <b>موجودی نماینده PAYG کافی نیست</b>\n"
        f"نماینده: <code>{_reseller_label(profile)}</code>\n"
        "فروش و تحویل سرویس تازهٔ این نماینده تا شارژ کیف پول متوقف است.",
        markup=_reseller_card_markup(profile, owner_shop_id),
    )
    return True


def _tick_issue_text(profile: ResellerProfile, issue: TickIssue) -> str:
    return (
        "⚠️ <b>مصرف نماینده PAYG حساب نمی‌شود</b>\n"
        f"نماینده: <code>{_reseller_label(profile)}</code>\n"
        f"{_TICK_ISSUE_TEXT[issue]}."
    )


def _reseller_label(profile: ResellerProfile) -> str:
    name = profile.pg_admin_username or profile.web_username or f"#{profile.user_id}"
    return html.escape(str(name))


def _reseller_text(level: int, balance: int) -> str:
    from app.services.formatting import format_toman

    return (
        "⚠️ <b>موجودی کیف پول شما رو به اتمام است</b>\n"
        f"موجودی فعلی: <b>{format_toman(balance)}</b> (کمتر از {format_toman(level)})\n"
        "مصرف PAYG از همین کیف پول کسر می‌شود. با تمام‌شدن موجودی، "
        "سرویس‌های شما قطع می‌شوند؛ لطفاً شارژ کنید."
    )


def _owner_text(profile: ResellerProfile, level: int, balance: int) -> str:
    from app.services.formatting import format_toman

    return (
        "⚠️ <b>موجودی نماینده PAYG کم است</b>\n"
        f"نماینده: <code>{_reseller_label(profile)}</code>\n"
        f"موجودی فعلی: <b>{format_toman(balance)}</b> (کمتر از {format_toman(level)})\n"
        "در صورت تمام‌شدن موجودی، سرویس‌های این نماینده قطع می‌شود."
    )


async def notify_low_balance(
    session: AsyncSession,
    profile: ResellerProfile,
    level: int,
    balance: int,
) -> None:
    """Send the warning to the reseller and to the admin who owns the reseller."""
    reseller = await session.get(BotUser, int(profile.user_id))
    reseller_tid = deliverable_telegram_id(getattr(reseller, "telegram_id", None))
    owner_shop_id, owner_chat_ids = await _owner_recipients(session, profile)
    if reseller_tid is not None:
        await _send(
            session, int(profile.user_id), [reseller_tid], _reseller_text(level, balance),
            markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="💳 صورتحساب و شارژ", callback_data=BILLING_SCREEN_CALLBACK),
            ]]),
        )
    await _send(
        session, owner_shop_id, owner_chat_ids, _owner_text(profile, level, balance),
        markup=_reseller_card_markup(profile, owner_shop_id),
    )


def _reseller_card_markup(profile: ResellerProfile, owner_shop_id: int | None) -> InlineKeyboardMarkup | None:
    """The platform owner opens the reseller card; a parent shop has no such screen."""
    if owner_shop_id is not None:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🤝 کارت نماینده", callback_data=f"adm:resellers:view:{int(profile.user_id)}"),
    ]])


async def _send(
    session: AsyncSession,
    shop_user_id: int | None,
    chat_ids: list[int],
    text: str,
    *,
    markup: InlineKeyboardMarkup | None = None,
) -> None:
    if not chat_ids:
        return
    from app.bot import create_bot
    from app.services.notifications import _send_to_chats
    from app.services.reseller_bots import open_notify_bot_for_reseller

    bot, owned = (None, False)
    if shop_user_id is not None:
        bot, owned = await open_notify_bot_for_reseller(session, int(shop_user_id))
    if bot is None:
        bot, owned = create_bot(), True
    try:
        await _send_to_chats(bot, chat_ids, text, markup=markup)
    finally:
        if owned:
            await bot.session.close()
