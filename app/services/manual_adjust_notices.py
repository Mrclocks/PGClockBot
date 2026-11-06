"""Telegram notices to a customer after a manual wallet or points change.

The money or points change is committed first; these helpers only schedule a best-effort
message afterwards. A replayed adjust (same request key) must not send again.

The staff ``reason`` / note is an internal audit field and is never included in the
customer message (option ب: notify without the internal reason).
"""

from __future__ import annotations

import asyncio
import logging

from app.services.formatting import format_toman
from app.services.reseller_bots import ATTRIBUTED_SHOP

logger = logging.getLogger(__name__)

# Strong refs until the background send finishes (create_task alone can be GC'd).
_pending: set[asyncio.Task[None]] = set()


def wallet_change_notice(*, mode: str, amount: int, balance: int) -> str:
    """Customer-facing HTML for a manual wallet credit or debit (no staff reason)."""
    head = "➕ <b>کیف پول شما شارژ شد</b>" if mode == "credit" else "➖ <b>از کیف پول شما کسر شد</b>"
    return "\n".join(
        [
            head,
            f"مبلغ: <b>{format_toman(amount)}</b>",
            f"موجودی فعلی: <b>{format_toman(balance)}</b>",
        ]
    )


def points_change_notice(*, mode: str, points: int, balance: int) -> str:
    """Customer-facing HTML for a manual points credit or debit (no staff reason)."""
    head = "➕ <b>به امتیاز شما افزوده شد</b>" if mode == "credit" else "➖ <b>از امتیاز شما کسر شد</b>"
    return "\n".join(
        [
            head,
            f"امتیاز: <b>{points:,}</b>",
            f"مانده فعلی: <b>{balance:,}</b>",
        ]
    )


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
            # Never raise into the event loop; the wallet/points commit already succeeded.
            logger.warning("customer adjust notice failed user=%s", user_id, exc_info=True)


def _notice_task_done(task: asyncio.Task[None]) -> None:
    _pending.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("customer adjust notice task crashed", exc_info=exc)


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
    task.add_done_callback(_notice_task_done)


def schedule_wallet_notice(
    *,
    user_id: int,
    mode: str,
    amount: int,
    balance: int,
    shop_id: int | None = None,
    replayed: bool = False,
) -> None:
    schedule_customer_notice(
        user_id,
        wallet_change_notice(mode=mode, amount=amount, balance=balance),
        shop_id=shop_id,
        replayed=replayed,
    )


def schedule_points_notice(
    *,
    user_id: int,
    mode: str,
    points: int,
    balance: int,
    shop_id: int | None = None,
    replayed: bool = False,
) -> None:
    schedule_customer_notice(
        user_id,
        points_change_notice(mode=mode, points=points, balance=balance),
        shop_id=shop_id,
        replayed=replayed,
    )
