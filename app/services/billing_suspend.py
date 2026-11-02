"""PAYG empty-balance suspend / restore (cut PG admin + owned users)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ResellerProfile
from app.services.billing import (
    get_low_balance_threshold,
    is_payg,
)

logger = logging.getLogger(__name__)


def _parse_id_list(raw: str | None) -> list[int]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    out: list[int] = []
    if isinstance(data, list):
        for x in data:
            try:
                out.append(int(x))
            except (TypeError, ValueError):
                continue
    return out


def _dump_id_list(ids: list[int]) -> str:
    return json.dumps([int(x) for x in ids], separators=(",", ":"))


async def list_owned_user_ids(pg, admin_username: str, *, page_size: int = 200) -> list[int]:
    """Page through PG users owned by this admin."""
    ids: list[int] = []
    offset = 0
    uname = (admin_username or "").strip()
    if not uname:
        return ids
    while True:
        try:
            data = await pg.get_users(admin=uname, limit=page_size, offset=offset)
        except Exception:
            logger.exception("list users for suspend failed admin=%s", uname)
            break
        users = []
        if isinstance(data, dict):
            users = data.get("users") or data.get("items") or []
        elif isinstance(data, list):
            users = data
        if not isinstance(users, list) or not users:
            break
        for u in users:
            if not isinstance(u, dict):
                continue
            uid = u.get("id")
            try:
                ids.append(int(uid))
            except (TypeError, ValueError):
                continue
        if len(users) < page_size:
            break
        offset += page_size
        if offset > 50_000:
            break
    return ids


async def _list_owned_user_ids(pg, admin_username: str, *, page_size: int = 200) -> list[int]:
    return await list_owned_user_ids(pg, admin_username, page_size=page_size)


async def _set_admin_enabled(pg, username: str, *, enabled: bool) -> None:
    """Best-effort disable/enable PG admin across API field variants."""
    uname = (username or "").strip()
    if not uname:
        return
    # PasarGuard's admin update has only ``status``; unknown keys are accepted and ignored, so
    # ``status`` must come first or the admin is never actually disabled or enabled.
    payloads: list[dict[str, Any]] = (
        [{"status": "active"}, {"enabled": True}, {"is_disabled": False}]
        if enabled
        else [{"status": "disabled"}, {"enabled": False}, {"is_disabled": True}]
    )
    last_err: Exception | None = None
    for payload in payloads:
        try:
            await pg.modify_admin(uname, payload)
            return
        except Exception as e:
            last_err = e
            continue
    if last_err:
        logger.warning("set admin enabled=%s failed admin=%s: %s", enabled, uname, last_err)


async def suspend_payg_reseller(
    session: AsyncSession,
    profile: ResellerProfile,
    *,
    commit: bool = True,
) -> dict[str, int]:
    """Disable PG admin + cut all owned users when shop-wallet PAYG balance <= 0.

    Idempotent when already suspended. Returns counters.
    """
    stats = {"users_cut": 0, "already": 0, "errors": 0}
    if not is_payg(profile):
        return stats
    from app.services.billing import payg_available_balance

    bal = await payg_available_balance(session, profile)
    if bal > 0:
        return stats
    if profile.billing_suspended_at is not None:
        stats["already"] = 1
        return stats

    uname = (profile.pg_admin_username or "").strip()
    if not uname:
        profile.billing_suspended_at = datetime.now(timezone.utc)
        profile.billing_suspended_user_ids = "[]"
        if commit:
            await session.commit()
        await _notify_suspended(session, profile)
        return stats

    from app.services.pasarguard import get_pg

    pg = get_pg()
    await _set_admin_enabled(pg, uname, enabled=False)

    disabled_ids: list[int] = []
    for uid in await _list_owned_user_ids(pg, uname):
        try:
            await pg.set_disabled_by_id(uid, True)
            disabled_ids.append(uid)
            stats["users_cut"] += 1
        except Exception:
            stats["errors"] += 1
            logger.debug("disable user %s failed", uid, exc_info=True)

    profile.billing_suspended_at = datetime.now(timezone.utc)
    profile.billing_suspended_user_ids = _dump_id_list(disabled_ids)
    if commit:
        await session.commit()

    try:
        await _notify_suspended(session, profile)
    except Exception:
        logger.debug("suspend notify failed", exc_info=True)
    return stats


async def restore_payg_reseller(
    session: AsyncSession,
    profile: ResellerProfile,
    *,
    commit: bool = True,
    force: bool = False,
) -> dict[str, int]:
    """Re-enable PG admin + users previously cut by suspend.

    ``force`` lifts the suspension without a positive wallet balance, for a reseller
    leaving PAYG billing. It does not send the "top-up confirmed" notice.
    """
    stats = {"users_restored": 0, "skipped": 0, "errors": 0}
    if profile.billing_suspended_at is None:
        stats["skipped"] = 1
        return stats
    from app.services.billing import payg_available_balance

    if not force and await payg_available_balance(session, profile) <= 0:
        stats["skipped"] = 1
        return stats

    uname = (profile.pg_admin_username or "").strip()
    from app.services.pasarguard import get_pg

    pg = get_pg()
    if uname:
        await _set_admin_enabled(pg, uname, enabled=True)
        for uid in _parse_id_list(profile.billing_suspended_user_ids):
            try:
                await pg.set_disabled_by_id(uid, False)
                stats["users_restored"] += 1
            except Exception:
                stats["errors"] += 1
                logger.debug("enable user %s failed", uid, exc_info=True)

    profile.billing_suspended_at = None
    profile.billing_suspended_user_ids = None
    if profile.billing_low_warned_at is not None:
        profile.billing_low_warned_at = None
    if commit:
        await session.commit()

    if not force:
        try:
            await _notify_restored(session, profile)
        except Exception:
            logger.debug("restore notify failed", exc_info=True)
    return stats


async def min_topup_to_unsuspend(session: AsyncSession) -> int:
    """Minimum wallet credit required to clear PAYG suspension (= 2× warn threshold)."""
    thr = await get_low_balance_threshold(session)
    # Exactly 2× threshold (user asked); still ensure at least 1 toman when thr=0
    return max(1, 2 * max(0, int(thr)))


async def assert_topup_clears_suspend(
    session: AsyncSession,
    profile: ResellerProfile,
    amount: int,
) -> None:
    """When suspended, topup amount must be at least 2× warning threshold."""
    if profile.billing_suspended_at is None:
        return
    need = await min_topup_to_unsuspend(session)
    if int(amount) < need:
        from app.services.billing import BillingError
        from app.services.formatting import format_toman

        raise BillingError(
            f"برای رفع مسدودی، حداقل {format_toman(need)} شارژ لازم است "
            f"(دو برابر آستانه هشدار)."
        )


async def maybe_restore_after_wallet_credit(
    session: AsyncSession,
    user_id: int,
    credit_amount: int,
    *,
    commit: bool = True,
) -> dict[str, int]:
    """Auto-unsuspend PAYG after a wallet credit iff amount ≥ 2× warn threshold.

    No-op when not PAYG, not suspended, or credit is below the minimum.
    """
    stats = {"restored": 0, "skipped": 0, "need": 0, "got": int(credit_amount or 0)}
    if int(credit_amount or 0) <= 0:
        stats["skipped"] = 1
        return stats

    from sqlalchemy import select

    from app.services.billing import ensure_payg_shop_wallet

    profile = (
        await session.execute(
            select(ResellerProfile).where(ResellerProfile.user_id == int(user_id))
        )
    ).scalar_one_or_none()
    if profile is None or not is_payg(profile):
        stats["skipped"] = 1
        return stats

    # Keep billing_balance mirrored to shop wallet after any credit
    try:
        _user, bal = await ensure_payg_shop_wallet(session, profile)
        profile.billing_balance = int(bal)
    except Exception:
        logger.debug("mirror billing after credit failed", exc_info=True)

    if profile.billing_suspended_at is None:
        if commit:
            await session.commit()
        stats["skipped"] = 1
        return stats

    need = await min_topup_to_unsuspend(session)
    stats["need"] = int(need)
    if int(credit_amount) < need:
        if commit:
            await session.commit()
        logger.info(
            "PAYG stay suspended reseller=%s credit=%s need=%s",
            user_id,
            credit_amount,
            need,
        )
        stats["skipped"] = 1
        return stats

    result = await restore_payg_reseller(session, profile, commit=commit)
    if result.get("skipped"):
        stats["skipped"] = 1
    else:
        stats["restored"] = 1
    return stats


async def _notify_suspended(session: AsyncSession, profile: ResellerProfile) -> None:
    from app.db.models import BotUser
    from app.services.formatting import format_toman

    user = await session.get(BotUser, int(profile.user_id))
    if not user or not user.telegram_id:
        return
    need = await min_topup_to_unsuspend(session)
    from app.services.billing import payg_available_balance

    try:
        bal_now = await payg_available_balance(session, profile)
    except Exception:
        bal_now = int(user.wallet_balance or 0)
    text = (
        "🚫 <b>حساب نمایندگی شما مسدود شد</b>\n\n"
        "موجودی کیف پول شما برای PAYG صفر شده است.\n"
        "ادمین پاسارگارد و تمام سرویس‌های شما قطع شدند.\n\n"
        f"موجودی فعلی کیف پول: <b>{format_toman(bal_now)}</b>\n"
        f"برای رفع مسدودی، حداقل <b>{format_toman(need)}</b> شارژ کنید "
        "(دو برابر آستانه هشدار).\n"
        "پس از تأیید شارژ، حساب و سرویس‌ها بلافاصله فعال می‌شوند."
    )
    await _send_reseller_dm(session, int(profile.user_id), int(user.telegram_id), text)


async def _notify_restored(session: AsyncSession, profile: ResellerProfile) -> None:
    from app.db.models import BotUser
    from app.services.formatting import format_toman

    user = await session.get(BotUser, int(profile.user_id))
    if not user or not user.telegram_id:
        return
    bal = format_toman(int(user.wallet_balance or 0))
    text = (
        "✅ <b>مسدودی برداشته شد</b>\n\n"
        "شارژ تأیید شد — حساب و سرویس‌های شما دوباره فعال شدند.\n"
        f"موجودی کیف پول: <b>{bal}</b>"
    )
    await _send_reseller_dm(session, int(profile.user_id), int(user.telegram_id), text)


async def _send_reseller_dm(
    session: AsyncSession, reseller_user_id: int, telegram_id: int, text: str
) -> None:
    try:
        from app.bot import create_bot
        from app.services.reseller_bots import open_notify_bot_for_reseller

        shop_bot, owned = await open_notify_bot_for_reseller(session, int(reseller_user_id))
        bot = shop_bot
        close = owned
        if bot is None:
            bot = create_bot()
            close = True
        try:
            await bot.send_message(int(telegram_id), text, parse_mode="HTML")
        finally:
            if close and bot is not None:
                await bot.session.close()
    except Exception:
        logger.debug("reseller DM failed", exc_info=True)


async def send_reseller_notice(session: AsyncSession, reseller_user_id: int, text: str) -> None:
    """Telegram notice to a reseller through their shop bot, else the main bot; best-effort."""
    from app.db.models import BotUser
    from app.services.platform_identity import deliverable_telegram_id

    user = await session.get(BotUser, int(reseller_user_id))
    telegram_id = deliverable_telegram_id(getattr(user, "telegram_id", None))
    if telegram_id is not None:
        await _send_reseller_dm(session, int(reseller_user_id), int(telegram_id), text)
