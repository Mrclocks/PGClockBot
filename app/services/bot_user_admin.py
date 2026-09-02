"""Shared admin ops for Telegram bot users (web panel + bot handlers).

Money uses shop ``BotUser.wallet_balance`` via ``credit_wallet``.
VPN quota/links are live from PasarGuard through ``UserService.pg_user_id``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import BotUser, Plan, UserService, WalletTransaction
from app.services.formatting import (
    expire_remaining_days,
    format_bytes,
    format_bytes_ratio,
    format_expire_short,
    status_label_plain,
)
from app.services.pasarguard import (
    build_user_modify_payload,
    get_pg,
    user_subscription_url,
)
from app.services.wallet import credit_wallet

logger = logging.getLogger(__name__)

GB = 1024**3
MAX_ADMIN_WALLET_CREDIT = 50_000_000  # 50M toman hard cap per op
MAX_EXTEND_DAYS = 3650
MAX_EXTEND_GB = 10_000


@dataclass(frozen=True)
class ServiceSnapshot:
    service: UserService
    pg: dict[str, Any] | None
    status_fa: str
    used_text: str
    limit_text: str
    volume_text: str
    remain_gb_text: str
    days_left: int | None
    expire_text: str
    subscription_url: str | None
    error: str | None = None


def _pg_client_for_service(service: UserService):
    """Platform owner PG client — bot-shop services are provisioned under owner/reseller link.

    Admin panel ops always use the platform client; reseller-scoped bot customers
    still have pg_user_id on the same PG instance.
    """
    return get_pg()


async def load_user_services(
    session: AsyncSession, bot_user_id: int
) -> list[UserService]:
    rows = (
        await session.execute(
            select(UserService)
            .where(UserService.bot_user_id == int(bot_user_id))
            .options(selectinload(UserService.plan))
            .order_by(UserService.id.desc())
        )
    ).scalars().all()
    return list(rows)


async def service_snapshot(session: AsyncSession, service: UserService) -> ServiceSnapshot:
    """Live PG status for one shop service."""
    import asyncio

    url = (service.subscription_url or "").strip() or None
    if not service.pg_user_id:
        return ServiceSnapshot(
            service=service,
            pg=None,
            status_fa="بدون پنل",
            used_text="—",
            limit_text="—",
            volume_text="—",
            remain_gb_text="—",
            days_left=None,
            expire_text="—",
            subscription_url=url,
            error="سرویس به پاسارگارد وصل نیست",
        )
    try:
        pg = _pg_client_for_service(service)
        # Cap wait so edit-modal fragment never hangs on slow PG (client timeout is 30s).
        info = await asyncio.wait_for(pg.get_user_by_id(int(service.pg_user_id)), timeout=3.0)
        if not isinstance(info, dict):
            raise ValueError("پاسخ نامعتبر پاسارگارد")
        used = int(info.get("used_traffic") or 0)
        limit = info.get("data_limit")
        try:
            limit_n = int(limit) if limit not in (None, "") else 0
        except (TypeError, ValueError):
            limit_n = 0
        remain_gb = "نامحدود"
        if limit_n > 0:
            left = max(0, limit_n - used)
            remain_gb = format_bytes(left)
        live_url = user_subscription_url(info) or url
        if live_url and live_url != service.subscription_url:
            service.subscription_url = live_url
            # token refresh best-effort
            from app.services.pasarguard import extract_sub_token

            tok = extract_sub_token(live_url)
            if tok:
                service.subscription_token = tok
        lim_for_ratio = limit_n if limit_n > 0 else None
        # Keep users-list cache warm whenever we successfully read live PG.
        sync_service_quota_cache(service, info)
        return ServiceSnapshot(
            service=service,
            pg=info,
            status_fa=status_label_plain(info.get("status")),
            used_text=format_bytes(used),
            limit_text=format_bytes(limit_n) if limit_n > 0 else "نامحدود",
            volume_text=format_bytes_ratio(used, lim_for_ratio),
            remain_gb_text=remain_gb,
            days_left=expire_remaining_days(info.get("expire") or info.get("expire_date")),
            expire_text=format_expire_short(info.get("expire") or info.get("expire_date")),
            subscription_url=live_url or url,
            error=None,
        )
    except Exception as e:
        logger.debug("service snapshot failed id=%s: %s", service.id, e, exc_info=True)
        err = "پاسارگارد پاسخ نداد" if isinstance(e, asyncio.TimeoutError) else str(e)[:160]
        return ServiceSnapshot(
            service=service,
            pg=None,
            status_fa="خطا",
            used_text="—",
            limit_text="—",
            volume_text="—",
            remain_gb_text="—",
            days_left=None,
            expire_text="—",
            subscription_url=url,
            error=err,
        )


async def list_service_snapshots(
    session: AsyncSession, bot_user_id: int
) -> list[ServiceSnapshot]:
    out: list[ServiceSnapshot] = []
    for svc in await load_user_services(session, bot_user_id):
        out.append(await service_snapshot(session, svc))
    await session.commit()  # persist refreshed subscription urls
    return out


async def admin_credit_user_wallet(
    session: AsyncSession,
    user: BotUser,
    amount: int,
    *,
    actor: str,
    note: str | None = None,
) -> BotUser:
    """Increase shop wallet (admin). Positive amounts only."""
    amt = int(amount)
    if amt <= 0:
        raise ValueError("مبلغ شارژ باید مثبت باشد")
    if amt > MAX_ADMIN_WALLET_CREDIT:
        raise ValueError(f"سقف شارژ دستی {MAX_ADMIN_WALLET_CREDIT:,} تومان است")
    reason = f"شارژ ادمین ({(actor or 'admin')[:64]})"
    if note:
        reason = f"{reason}: {note.strip()[:120]}"
    return await credit_wallet(session, user, amt, reason)


async def list_wallet_txs(
    session: AsyncSession, user_id: int, *, limit: int = 20
) -> list[WalletTransaction]:
    rows = (
        await session.execute(
            select(WalletTransaction)
            .where(WalletTransaction.user_id == int(user_id))
            .order_by(WalletTransaction.id.desc())
            .limit(int(limit))
        )
    ).scalars().all()
    return list(rows)


async def get_owned_service(
    session: AsyncSession, *, bot_user_id: int, service_id: int
) -> UserService:
    svc = await session.get(UserService, int(service_id))
    if svc is None or int(svc.bot_user_id) != int(bot_user_id):
        raise ValueError("سرویس یافت نشد")
    return svc


def sync_service_quota_cache(
    service: UserService,
    info: dict[str, Any] | None = None,
    *,
    expire_ts: int | None = None,
    data_limit_bytes: int | None = None,
) -> None:
    """Persist PG expire/data_limit onto UserService for users-list summaries.

    Prefer a live PG ``info`` dict when present. Otherwise apply absolute
    ``expire_ts`` / ``data_limit_bytes`` from the write just issued (partial OK).
    ``quota_expire_at is None`` + synced ⇒ unlimited time;
    ``quota_data_limit_bytes == 0`` + synced ⇒ unlimited volume.
    """
    from app.services.formatting import parse_expire

    touched = False
    if isinstance(info, dict):
        has_expire = "expire" in info or "expire_date" in info
        if has_expire:
            exp_raw = info["expire"] if "expire" in info else info.get("expire_date")
            # PG: 0 / null ⇒ unlimited (parse_expire(0) would be epoch — wrong)
            if exp_raw in (None, "", 0, 0.0) or (
                isinstance(exp_raw, (int, float)) and int(exp_raw) <= 0
            ):
                service.quota_expire_at = None
            else:
                exp_dt = parse_expire(exp_raw)
                if exp_dt is not None and exp_dt.tzinfo is None:
                    exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                service.quota_expire_at = exp_dt
            touched = True
        if "data_limit" in info:
            try:
                lim = info.get("data_limit")
                service.quota_data_limit_bytes = (
                    0 if lim in (None, "") else max(0, int(lim))
                )
            except (TypeError, ValueError):
                service.quota_data_limit_bytes = 0
            touched = True
    else:
        if expire_ts is not None:
            if int(expire_ts) <= 0:
                service.quota_expire_at = None
            else:
                service.quota_expire_at = datetime.fromtimestamp(
                    int(expire_ts), tz=timezone.utc
                )
            touched = True
        if data_limit_bytes is not None:
            service.quota_data_limit_bytes = max(0, int(data_limit_bytes))
            touched = True
    if touched:
        service.quota_synced_at = datetime.now(timezone.utc)


async def admin_set_service_quota(
    session: AsyncSession,
    service: UserService,
    *,
    data_limit_bytes: int | None = None,
    expire_ts: int | None = None,
    reset_traffic: bool = False,
    activate: bool = True,
) -> ServiceSnapshot:
    """Set absolute quota on the linked PG user and refresh local link + list cache."""
    if not service.pg_user_id:
        raise ValueError("سرویس به پاسارگارد وصل نیست")
    pg = _pg_client_for_service(service)
    if reset_traffic:
        try:
            await pg.reset_user_by_id(int(service.pg_user_id))
        except Exception:
            logger.debug("reset traffic failed uid=%s", service.pg_user_id, exc_info=True)
    payload = build_user_modify_payload(
        data_limit=data_limit_bytes,
        expire_ts=expire_ts,
        status="active" if activate else None,
    )
    if not payload and not reset_traffic:
        raise ValueError("تغییری برای اعمال نیست")
    info: dict | None = None
    if payload:
        info = await pg.modify_user_by_id(int(service.pg_user_id), payload)
        if isinstance(info, dict):
            live = user_subscription_url(info)
            if live:
                service.subscription_url = live
                from app.services.pasarguard import extract_sub_token

                tok = extract_sub_token(live)
                if tok:
                    service.subscription_token = tok
            sync_service_quota_cache(service, info)
        # Absolute values we just wrote win when PG GET/modify is laggy or partial
        if expire_ts is not None or data_limit_bytes is not None:
            sync_service_quota_cache(
                service,
                expire_ts=expire_ts,
                data_limit_bytes=data_limit_bytes,
            )
    else:
        # Traffic reset only — refresh cache from live PG when possible
        try:
            live_info = await pg.get_user_by_id(int(service.pg_user_id))
            if isinstance(live_info, dict):
                sync_service_quota_cache(service, live_info)
        except Exception:
            logger.debug("quota cache refresh failed uid=%s", service.pg_user_id, exc_info=True)
    service.notified_expire = False
    service.notified_traffic = False
    await session.commit()
    snap = await service_snapshot(session, service)
    # Persist any cache refresh from the live snapshot read
    await session.commit()
    return snap


async def admin_renew_service(
    session: AsyncSession,
    service: UserService,
    *,
    days: int | None = None,
    data_limit_gb: float | None = None,
    plan: Plan | None = None,
    reset_traffic: bool = True,
) -> ServiceSnapshot:
    """Admin renew: set expire from now + days and optional data limit (from plan or args)."""
    if plan is not None:
        if plan.is_trial:
            raise ValueError("پلن تست برای تمدید مجاز نیست")
        days = int(plan.duration_days or 0) or days
        if plan.data_limit_gb is not None:
            data_limit_gb = float(plan.data_limit_gb)
        service.plan_id = int(plan.id)

    if days is not None:
        days_n = int(days)
        if days_n < 0 or days_n > MAX_EXTEND_DAYS:
            raise ValueError("تعداد روز نامعتبر است")
        expire_ts = int(time.time()) + days_n * 86400 if days_n > 0 else 0
    else:
        expire_ts = None

    if data_limit_gb is not None:
        gb = float(data_limit_gb)
        if gb < 0 or gb > MAX_EXTEND_GB:
            raise ValueError("حجم نامعتبر است")
        data_limit_bytes = int(gb * GB) if gb > 0 else 0
    else:
        data_limit_bytes = None

    return await admin_set_service_quota(
        session,
        service,
        data_limit_bytes=data_limit_bytes,
        expire_ts=expire_ts,
        reset_traffic=reset_traffic,
        activate=True,
    )


async def admin_extend_service(
    session: AsyncSession,
    service: UserService,
    *,
    extra_days: int = 0,
    extra_gb: float = 0,
) -> ServiceSnapshot:
    """Adjust days/GB on current PG remaining (admin). Signed deltas allowed (±MAX)."""
    days_n = int(extra_days or 0)
    gb_n = float(extra_gb or 0)
    if days_n == 0 and gb_n == 0:
        raise ValueError("حداقل یک مقدار غیرصفر وارد کنید")
    if abs(days_n) > MAX_EXTEND_DAYS:
        raise ValueError("تعداد روز نامعتبر است")
    if abs(gb_n) > MAX_EXTEND_GB:
        raise ValueError("حجم نامعتبر است")
    if not service.pg_user_id:
        raise ValueError("سرویس به پاسارگارد وصل نیست")

    pg = _pg_client_for_service(service)
    info = await pg.get_user_by_id(int(service.pg_user_id))
    if not isinstance(info, dict):
        raise ValueError("کاربر پاسارگارد یافت نشد")

    expire_ts = None
    if days_n != 0:
        from app.services.formatting import parse_expire

        cur = parse_expire(info.get("expire") or info.get("expire_date"))
        now = datetime.now(timezone.utc)
        if days_n > 0:
            # Extend from remaining future expire, else from now
            base = cur if cur and cur > now else now
        else:
            # Reduce from current expire (or now if already expired / missing)
            base = cur if cur else now
        expire_ts = int(base.timestamp()) + days_n * 86400
        # Guard absurd negative unix stamps; past expire is allowed (shows expired)
        if expire_ts < 0:
            expire_ts = 0

    data_limit_bytes = None
    if gb_n != 0:
        try:
            cur_lim = int(info.get("data_limit") or 0)
        except (TypeError, ValueError):
            cur_lim = 0
        if cur_lim <= 0:
            if gb_n < 0:
                raise ValueError("حجم نامحدود است")
            # Unlimited (0) → start from purchased extra only
            data_limit_bytes = int(gb_n * GB)
        else:
            data_limit_bytes = max(0, cur_lim + int(gb_n * GB))

    return await admin_set_service_quota(
        session,
        service,
        data_limit_bytes=data_limit_bytes,
        expire_ts=expire_ts,
        reset_traffic=False,
        activate=True,
    )


def snapshot_telegram_lines(snap: ServiceSnapshot) -> str:
    svc = snap.service
    plan_name = svc.plan.name if svc.plan else "—"
    days = "نامحدود" if snap.days_left is None else f"{snap.days_left} روز"
    lines = [
        f"📦 سرویس #{svc.id} · {plan_name}",
        f"وضعیت: {snap.status_fa}",
        f"حجم: {snap.volume_text} (مانده {snap.remain_gb_text})",
        f"زمان: {snap.expire_text} · مانده {days}",
    ]
    if snap.subscription_url:
        lines.append(f"لینک: {snap.subscription_url}")
    if snap.error:
        lines.append(f"⚠️ {snap.error}")
    return "\n".join(lines)
