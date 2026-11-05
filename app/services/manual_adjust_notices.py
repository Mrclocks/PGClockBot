"""Telegram notices to a customer after a manual wallet or points change.

The money or points change is committed first; these helpers only schedule a best-effort
message afterwards. A replayed adjust (same request key) must not send again.
"""

from __future__ import annotations

import asyncio
import html
import logging

from app.services.formatting import format_toman
from app.services.reseller_bots import ATTRIBUTED_SHOP

logger = logging.getLogger(__name__)

# Keep a strong reference until the background send finishes.
_pending: set[asyncio.Task[None]] = set()


def wallet_change_notice(*, mode: str, amount: int, balance: int, note: str | None) -> str:
    """Customer-facing HTML for a manual wallet credit or debit."""
    head = "➕ <b>کیف پول شما شارژ شد</b>" if mode == "credit" else "➖ <b>از کیف پول شما کسر شد</b>"
    lines = [
        head,
        f"مبلغ: <b>{format_toman(amount)}</b>",
        f"موجودی فعلی: <b>{format_toman(balance)}</b>",
    ]
    if note:
        lines.append(f"توضیح: {html.escape(note)}")
    return "\n".join(lines)


def points_change_notice(*, mode: str, points: int, balance: int, note: str | None) -> str:
    """Customer-facing HTML for a manual points credit or debit."""
    head = "➕ <b>به امتیاز شما افزوده شد</b>" if mode == "credit" else "➖ <b>از امتیاز شما کسر شد</b>"
    lines = [
        head,
        f"امتیاز: <b>{points:,}</b>",
        f"مانده فعلی: <b>{balance:,}</b>",
    ]
    if note:
        lines.append(f"توضیح: {html.escape(note)}")
    return "\n".join(lines)


async def _send_customer_notice(user_id: int, text: str, *, shop_id: int | None | object) -> None:
    # Own session: the request session is closed by the time this runs.
    from app.db.models import BotUser
    from app.db.session import SessionLocal
    from app.services.reseller_bots import ShopBotUnavailable, open_notify_bot_for_user

    async with SessionLocal() as session:
        user = await session.get(BotUser, int(user_id))
        if user is None or not getattr(user, "telegram_id", None):
            return
        try:
            bot, should_close = await open_notify_bot_for_user(session, user, shop_id=shop_id)
            try:
                await bot.send_message(int(user.telegram_id), text, parse_mode="HTML")
            finally:
                if should_close:
                    await bot.session.close()
        except ShopBotUnavailable:
            logger.warning(
                "customer adjust notice skipped: shop bot unavailable user=%s shop=%s",
                user_id,
                shop_id,
            )
        except Exception:
            logger.warning("customer adjust notice failed user=%s", user_id, exc_info=True)


def schedule_customer_notice(
    user_id: int,
    text: str,
    *,
    shop_id: int | None | object = ATTRIBUTED_SHOP,
    replayed: bool = False,
) -> None:
    """Send ``text`` to the customer after commit; no-op when the adjust was a replay."""
    if replayed:
        return
    task = asyncio.get_running_loop().create_task(
        _send_customer_notice(int(user_id), text, shop_id=shop_id),
        name="manual-adjust-notice",
    )
    _pending.add(task)
    task.add_done_callback(_pending.discard)


def schedule_wallet_notice(
    *,
    user_id: int,
    mode: str,
    amount: int,
    balance: int,
    note: str | None,
    shop_id: int | None = None,
    replayed: bool = False,
) -> None:
    schedule_customer_notice(
        user_id,
        wallet_change_notice(mode=mode, amount=amount, balance=balance, note=note),
        shop_id=shop_id,
        replayed=replayed,
    )


def schedule_points_notice(
    *,
    user_id: int,
    mode: str,
    points: int,
    balance: int,
    note: str | None,
    shop_id: int | None = None,
    replayed: bool = False,
) -> None:
    schedule_customer_notice(
        user_id,
        points_change_notice(mode=mode, points=points, balance=balance, note=note),
        shop_id=shop_id,
        replayed=replayed,
    )
