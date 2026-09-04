"""Quick ops on /users: staff DM, quick renew, scoped bulk message."""

from __future__ import annotations

import asyncio
import html
import logging
import re
from typing import Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, Plan, UserService
from app.services.users_ops import (
    UserOpsRow,
    approx_expire_at,
    build_user_ops_row,
    load_services_by_user_ids,
)

logger = logging.getLogger(__name__)

MAX_STAFF_MESSAGE_LEN = 2000
MAX_BULK_RECIPIENTS = 40
_BULK_SEND_DELAY = 0.05

_TAG_RE = re.compile(r"<[^>]+>")


def sanitize_staff_message(raw: str) -> str:
    """Plain staff text → safe Telegram HTML (escaped, newlines preserved)."""
    text = (raw or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise ValueError("متن پیام خالی است")
    if len(text) > MAX_STAFF_MESSAGE_LEN:
        raise ValueError(f"متن پیام حداکثر {MAX_STAFF_MESSAGE_LEN} کاراکتر باشد")
    # Strip any tags the staff pasted, then escape.
    text = _TAG_RE.sub("", text)
    return html.escape(text)


def wrap_staff_dm(body_html: str, *, actor: str | None = None) -> str:
    # Telegram HTML has no <small>; use <i> for the actor line.
    head = "📩 <b>پیام پشتیبانی</b>"
    if actor:
        head += f"\n<i>از: {html.escape(str(actor)[:64])}</i>"
    return f"{head}\n\n{body_html}"


async def send_staff_dm(
    session: AsyncSession,
    user: BotUser,
    text: str,
    *,
    actor: str | None = None,
) -> None:
    """Send a scoped staff DM via the correct shop/main bot."""
    if user.is_blocked:
        raise ValueError("کاربر مسدود است — ابتدا مسدودی را بردارید")
    if not user.telegram_id:
        raise ValueError("شناسه تلگرام کاربر مشخص نیست")
    body = sanitize_staff_message(text)
    payload = wrap_staff_dm(body, actor=actor)

    from app.services.reseller_bots import open_notify_bot_for_user

    bot, should_close = await open_notify_bot_for_user(session, user)
    try:
        await bot.send_message(int(user.telegram_id), payload, parse_mode="HTML")
    finally:
        if should_close:
            await bot.session.close()


def pick_critical_service(services: list[UserService]) -> UserService | None:
    """Soonest approx-expire service; else newest by id."""
    if not services:
        return None
    scored: list[tuple[float, int, UserService]] = []
    for svc in services:
        exp = approx_expire_at(svc, getattr(svc, "plan", None))
        ts = exp.timestamp() if exp is not None else float("inf")
        scored.append((ts, int(svc.id), svc))
    scored.sort(key=lambda t: (t[0], -t[1]))
    return scored[0][2]


async def quick_renew_user(
    session: AsyncSession,
    user: BotUser,
    *,
    service_id: int | None = None,
) -> tuple[UserService, str]:
    """Renew critical (or specified) service with its current plan. Returns (svc, label)."""
    from app.services.bot_user_admin import admin_renew_service, get_owned_service

    by = await load_services_by_user_ids(session, [int(user.id)])
    services = by.get(int(user.id), [])
    if not services:
        raise ValueError("سرویسی برای تمدید نیست")

    target: UserService | None
    if service_id is not None:
        target = next((s for s in services if int(s.id) == int(service_id)), None)
        if target is None:
            raise ValueError("سرویس متعلق به این کاربر نیست")
    else:
        target = pick_critical_service(services)
    if target is None:
        raise ValueError("سرویس یافت نشد")

    # Re-load through ownership guard
    svc = await get_owned_service(
        session, bot_user_id=int(user.id), service_id=int(target.id)
    )
    plan = None
    if svc.plan_id:
        plan = await session.get(Plan, int(svc.plan_id))
    if plan is None:
        raise ValueError("پلن سرویس مشخص نیست — از ویرایش کاربر تمدید کنید")
    if plan.is_trial:
        raise ValueError("پلن تست برای تمدید سریع مجاز نیست")

    await admin_renew_service(session, svc, plan=plan, reset_traffic=True)
    label = (plan.name or svc.pg_username or f"#{svc.id}").strip()
    return svc, label


async def bulk_staff_dm(
    session: AsyncSession,
    users: Iterable[BotUser],
    text: str,
    *,
    actor: str | None = None,
) -> dict[str, int]:
    """Send the same staff DM to many users (hard-capped)."""
    targets = [u for u in users if u and not u.is_blocked and u.telegram_id]
    if not targets:
        raise ValueError("گیرنده‌ای برای ارسال نیست")
    if len(targets) > MAX_BULK_RECIPIENTS:
        raise ValueError(f"حداکثر {MAX_BULK_RECIPIENTS} گیرنده در هر ارسال")

    body = sanitize_staff_message(text)
    payload = wrap_staff_dm(body, actor=actor)
    ok = 0
    fail = 0
    for u in targets:
        try:
            from app.services.reseller_bots import open_notify_bot_for_user

            bot, should_close = await open_notify_bot_for_user(session, u)
            try:
                await bot.send_message(int(u.telegram_id), payload, parse_mode="HTML")
                ok += 1
            finally:
                if should_close:
                    await bot.session.close()
        except Exception:
            logger.debug("bulk dm failed user=%s", getattr(u, "id", None), exc_info=True)
            fail += 1
        await asyncio.sleep(_BULK_SEND_DELAY)
    return {"ok": ok, "fail": fail, "total": len(targets)}


def bulk_targets_from_rows(rows: list[UserOpsRow]) -> list[BotUser]:
    return [r.user for r in rows if r.user and not r.user.is_blocked]
