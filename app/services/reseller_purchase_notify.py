"""Tell the platform owner when a reseller buys capacity or renews a package from them.

Wallet buys never create a shop-scoped order, so ``notify_new_subscription`` is never
called from checkout. These helpers send the same class of report (same notify pref) to
``ADMIN_IDS`` on the platform bot. Failures are logged and never undo a completed purchase.
"""

from __future__ import annotations

import html
import logging

from aiogram.exceptions import TelegramAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import BotUser, ResellerPlan
from app.services.formatting import format_message, format_toman, info_block, kv_line
from app.services.notifications import _buyer_lines, _dispatch_dual_notify

logger = logging.getLogger(__name__)

NOTIFY_KEY = "notify_new_subscription"


async def notify_owner_of_reseller_purchase(
    session: AsyncSession,
    *,
    buyer: BotUser,
    title: str,
    detail_lines: list[str],
    amount: int,
    payment_label: str = "کیف پول",
) -> int:
    """Best-effort staff report to the platform owner; returns chats reached."""
    from app.bot import create_bot

    settings = get_settings()
    lines = [
        *_buyer_lines(buyer),
        kv_line("💰", "مبلغ", format_toman(int(amount or 0), settings.currency)),
        *detail_lines,
        kv_line("💳", "پرداخت", html.escape(payment_label)),
        kv_line("📌", "وضعیت", "تحویل‌شده"),
    ]
    text = format_message(title, info_block(lines))
    bot = create_bot()
    try:
        try:
            # Platform sale: never route to a shop bot / shop prefs.
            return await _dispatch_dual_notify(
                bot, session, NOTIFY_KEY, text, shop=False, platform=True,
            )
        except (TelegramAPIError, OSError, TimeoutError):
            logger.warning(
                "reseller purchase owner notify failed buyer=%s title=%s",
                getattr(buyer, "id", None),
                title,
                exc_info=True,
            )
            return 0
    finally:
        try:
            await bot.session.close()
        except Exception:
            logger.debug("reseller purchase notify: bot close failed", exc_info=True)


async def notify_capacity_wallet_purchase(
    session: AsyncSession,
    *,
    buyer: BotUser,
    amount: int,
    gb: int = 0,
    users: int = 0,
    pack_name: str | None = None,
) -> int:
    """Owner report after a wallet capacity / pack purchase."""
    details = [kv_line("📝", "بابت", "خرید ظرفیت نمایندگی")]
    if pack_name:
        details.append(kv_line("📦", "بسته", html.escape(str(pack_name))))
    parts = [
        f"{int(gb)} گیگ حجم" if int(gb or 0) else "",
        f"{int(users)} کاربر" if int(users or 0) else "",
    ]
    joined = " و ".join(p for p in parts if p)
    if joined:
        details.append(kv_line("➕", "مقدار افزایش", joined))
    return await notify_owner_of_reseller_purchase(
        session,
        buyer=buyer,
        title="➕ خرید ظرفیت نمایندگی",
        detail_lines=details,
        amount=amount,
    )


async def notify_package_wallet_renew(
    session: AsyncSession,
    *,
    buyer: BotUser,
    amount: int,
    plan_name: str | None,
    changed: bool,
) -> int:
    """Owner report after a wallet renew / upgrade of the reseller's own package."""
    title = "🔄 ارتقا بسته نمایندگی" if changed else "🔄 تمدید بسته نمایندگی"
    details = [kv_line("📝", "بابت", "تمدید بسته نمایندگی")]
    if plan_name:
        details.append(kv_line("💎", "بسته", html.escape(str(plan_name))))
    return await notify_owner_of_reseller_purchase(
        session,
        buyer=buyer,
        title=title,
        detail_lines=details,
        amount=amount,
    )


async def pack_name_for_offer_reason(session: AsyncSession, reason: str) -> str | None:
    """``reseller_addon:{pack_id}:…`` → pack display name; other reasons → None."""
    text = (reason or "").strip()
    if not text.startswith("reseller_addon:"):
        return None
    parts = text.split(":")
    if len(parts) < 2 or not parts[1].isdigit():
        return None
    pack = await session.get(ResellerPlan, int(parts[1]))
    return str(pack.name) if pack is not None else None
