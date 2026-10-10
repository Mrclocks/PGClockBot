"""Shared admin ops for Telegram bot users (web panel + bot handlers).

Money uses ``credit_wallet`` / ``debit_wallet`` (platform purse by default;
pass shop_id for shop purse). Never write balances outside those helpers.
VPN quota/links are live from PasarGuard through ``UserService.pg_user_id``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import BotUser, Plan, Role, UserService, WalletTransaction
from app.services.formatting import (
    expire_remaining_days,
    format_bytes,
    format_bytes_ratio,
    format_expire_short,
    is_on_hold_status,
    on_hold_expire_duration_seconds,
    status_label_plain,
    time_remaining_label,
)
from app.services.numbers import normalize_number_text
from app.services.pasarguard import (
    absolutize_subscription_url,
    build_user_modify_payload,
    get_pg,
    user_subscription_url,
)
from app.services.wallet import credit_wallet, debit_wallet

logger = logging.getLogger(__name__)

GB = 1024**3
MAX_ADMIN_WALLET_CREDIT = 50_000_000  # 50M toman hard cap per op
MAX_ADMIN_WALLET_DEBIT = MAX_ADMIN_WALLET_CREDIT
MIN_ADMIN_WALLET_ADJUST = 1_000  # absolute toman floor for manual adjust UI/API
MAX_EXTEND_DAYS = 3650
MAX_EXTEND_GB = 10_000
MIN_TELEGRAM_ID = 1
MAX_TELEGRAM_ID = 9_007_199_254_740_991  # signed int64 safe upper bound


def parse_admin_wallet_amount(text: str | None) -> int:
    """Parse a signed integer toman amount (no float / no scientific notation)."""
    raw = normalize_number_text(text).replace("−", "-").replace("–", "-")
    if not raw or raw in {"+", "-"}:
        raise ValueError("مبلغ نامعتبر است")
    if any(ch in raw for ch in ".eE"):
        raise ValueError("مبلغ باید عدد صحیح تومان باشد")
    if raw.startswith("+"):
        raw = raw[1:]
        if not raw or raw.startswith("-"):
            raise ValueError("مبلغ نامعتبر است")
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError("مبلغ نامعتبر است") from exc


def _require_admin_wallet_amount(amount: object) -> int:
    """Reject bool / non-int; return a plain int (bool is a subclass of int)."""
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise ValueError("مبلغ نامعتبر است")
    return int(amount)


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

    @property
    def time_label(self) -> str:
        """Remaining-time label — never «نامحدود» for on_hold + null days."""
        status = (self.pg or {}).get("status") if self.pg else None
        return time_remaining_label(days_left=self.days_left, status=status)


def assert_staff_pg_user_action(staff: dict | None, action: str) -> None:
    """Fail closed when web staff lacks the PasarGuard users.* action.

    Bot/internal callers pass ``staff=None`` and skip this check (they have their
    own auth). Web panel must always pass the live staff session.
    """
    if staff is None:
        return
    from app.services.pg_access import staff_user_actions

    acts = staff_user_actions(staff)
    if acts.get(action):
        return
    labels = {
        "create": "ساخت کاربر پاسارگارد",
        "update": "ویرایش کاربر پاسارگارد",
        "delete": "حذف کاربر پاسارگارد",
        "disable": "غیرفعال‌سازی کاربر پاسارگارد",
        "reset_usage": "ریست مصرف پاسارگارد",
    }
    raise ValueError(
        f"نقش پاسارگارد شما اجازه {labels.get(action, action)} را ندارد"
    )


async def _pg_client_for_bot_service(
    session: AsyncSession, service: UserService
):
    """PG client for a shop service — reseller shop uses that shop's PG admin.

    Never fall through to the Owner env token for tenant services (isolation).
    """
    bot_user = await session.get(BotUser, int(service.bot_user_id))
    rid = int(bot_user.reseller_id) if bot_user and bot_user.reseller_id else None
    if rid:
        from app.services.pasarguard import get_pg_for_reseller

        return await get_pg_for_reseller(session, rid)
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

    url = absolutize_subscription_url(service.subscription_url) or (
        (service.subscription_url or "").strip() or None
    )
    if url and url != (service.subscription_url or "").strip():
        service.subscription_url = url
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
        pg = await _pg_client_for_bot_service(session, service)
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
        status_raw = info.get("status")
        exp_raw = info.get("expire") if "expire" in info else info.get("expire_date")
        hold_dur = on_hold_expire_duration_seconds(info)
        days_left = expire_remaining_days(
            exp_raw, status=status_raw, expire_duration=hold_dur
        )
        expire_text = format_expire_short(
            exp_raw, status=status_raw, expire_duration=hold_dur
        )
        # on_hold without a known duration: never show «نامحدود» for remaining days
        if is_on_hold_status(status_raw) and days_left is None and not exp_raw:
            days_left = None  # snapshot_telegram_lines must not say نامحدود
        return ServiceSnapshot(
            service=service,
            pg=info,
            status_fa=status_label_plain(status_raw),
            used_text=format_bytes(used),
            limit_text=format_bytes(limit_n) if limit_n > 0 else "نامحدود",
            volume_text=format_bytes_ratio(used, lim_for_ratio),
            remain_gb_text=remain_gb,
            days_left=days_left,
            expire_text=expire_text,
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


def _plan_matches_user_shop(plan: Plan, user: BotUser) -> bool:
    """Plan catalog tenant must match the bot user's shop ownership."""
    user_rid = user.reseller_id
    plan_rid = plan.owner_reseller_id
    if user_rid is None:
        return plan_rid is None
    return plan_rid is not None and int(plan_rid) == int(user_rid)


def _normalize_telegram_username(raw: str | None) -> str | None:
    if not raw:
        return None
    name = str(raw).strip().lstrip("@")[:64]
    return name or None


async def admin_create_bot_user(
    session: AsyncSession,
    *,
    telegram_id: int,
    username: str | None = None,
    full_name: str | None = None,
    reseller_id: int | None = None,
    staff: dict | None = None,
    color_tag: str | None = None,
) -> BotUser:
    """Create a shop BotUser manually (web panel). Always role=user.

    When ``staff`` is provided, shop ownership is forced from session scope:
    Owner → platform (``reseller_id=None``); reseller → own shop id only.
    """
    import secrets
    import string

    from app.services.color_tags import normalize_color_tag
    from app.services.platform_identity import (
        deliverable_telegram_id,
        is_explicit_owner_staff,
    )

    if staff is not None:
        from app.services.shop_scope import ShopScopeError, resolve_shop_scope_id

        if is_explicit_owner_staff(staff):
            reseller_id = None
        else:
            try:
                scope = resolve_shop_scope_id(staff)
            except ShopScopeError as exc:
                raise ValueError(exc.message) from exc
            if not scope:
                raise ValueError("محدوده فروشگاه مشخص نیست")
            reseller_id = int(scope)

    try:
        tid = int(telegram_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("شناسه تلگرام نامعتبر است") from exc
    if tid < MIN_TELEGRAM_ID or tid > MAX_TELEGRAM_ID:
        raise ValueError("شناسه تلگرام نامعتبر است")

    if deliverable_telegram_id(tid) is None:
        raise ValueError("شناسه تلگرام باید عدد مثبت واقعی باشد")

    existing = (
        await session.execute(select(BotUser).where(BotUser.telegram_id == tid))
    ).scalar_one_or_none()
    if existing is not None:
        raise ValueError("این شناسه تلگرام قبلاً ثبت شده است")

    if reseller_id is not None:
        try:
            rid = int(reseller_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("فروشگاه نامعتبر است") from exc
        if rid <= 0:
            raise ValueError("فروشگاه نامعتبر است")
        owner = await session.get(BotUser, rid)
        if owner is None or owner.role != Role.RESELLER.value:
            raise ValueError("نماینده فروشگاه یافت نشد")
        assign_reseller = rid
    else:
        assign_reseller = None

    alphabet = string.ascii_uppercase + string.digits
    referral_code = "".join(secrets.choice(alphabet) for _ in range(8))

    user = BotUser(
        telegram_id=tid,
        username=_normalize_telegram_username(username),
        full_name=(str(full_name).strip()[:255] if full_name else None) or None,
        role=Role.USER.value,
        referral_code=referral_code,
        reseller_id=assign_reseller,
        color_tag=normalize_color_tag(color_tag),
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def admin_provision_service(
    session: AsyncSession,
    user: BotUser,
    plan: Plan,
    *,
    staff: dict | None = None,
    actor: str | None = None,
) -> UserService:
    """Assign a new VPN service (PG user + UserService) without an order."""
    import time

    from app.services.orders import _reseller_pg_link, generate_pg_username
    from app.services.pasarguard import (
        build_user_create_payload,
        extract_sub_token,
        parse_group_ids,
    )
    from app.services.plans_catalog import plan_belongs_to_staff
    from app.services.provision_gate import ProvisionError, assert_provision_create

    if not plan.is_active:
        raise ValueError("پلن غیرفعال است")
    if plan.is_trial:
        raise ValueError("پلن تست برای اختصاص دستی مجاز نیست")
    if not _plan_matches_user_shop(plan, user):
        raise ValueError("این پلن متعلق به فروشگاه این کاربر نیست")
    if staff is not None and not plan_belongs_to_staff(plan, staff):
        raise ValueError("دسترسی به این پلن ندارید")
    # Shop isolation ∧ PasarGuard ACL (same axis as /pg/users create).
    assert_staff_pg_user_action(staff, "create")

    shop_rid = int(user.reseller_id) if user.reseller_id else None
    if shop_rid:
        from app.services.pasarguard import get_pg_for_reseller

        pg = await get_pg_for_reseller(session, shop_rid)
    else:
        from app.services.pasarguard import get_pg

        pg = get_pg()

    pg_owner, pg_role_id = await _reseller_pg_link(session, shop_rid)
    data_limit = None
    expire = None
    if plan.data_limit_gb is not None:
        data_limit = int(float(plan.data_limit_gb) * GB)
    if plan.duration_days:
        expire = int(time.time()) + int(plan.duration_days) * 86400

    # Always run provision gate when staff is present (Owner Hybrid limited role too).
    # Reseller shop also passes username/role for quota ownership checks.
    if staff is not None or shop_rid:
        try:
            await assert_provision_create(
                session,
                staff=staff,
                reseller_user_id=shop_rid,
                pg_admin_username=pg_owner,
                pg_role_id=pg_role_id,
                data_limit=data_limit,
                expire_ts=expire,
                from_template=bool(plan.pg_template_id),
                quantity=1,
            )
        except ProvisionError as e:
            raise ValueError(e.message) from e

    group_ids = None
    if not plan.pg_template_id:
        group_ids = parse_group_ids(getattr(plan, "pg_group_ids", None))
        if not group_ids:
            raise ValueError(
                "هیچ گروهی برای ساخت کاربر انتخاب نشده — در وب‌پنل برای پلن، گروه پاسارگارد را انتخاب کنید"
            )

    actor_label = (actor or "admin")[:64]
    note = f"PGClockBot manual assign by {actor_label}"

    username = await generate_pg_username(
        session,
        user_id=int(user.id),
        plan=plan,
        reseller_id=shop_rid,
    )

    pg_uid: int | None = None
    try:
        if plan.pg_template_id:
            pg_user = await pg.create_user_from_template(
                {
                    "username": username,
                    "user_template_id": plan.pg_template_id,
                    "note": note,
                }
            )
        else:
            pg_user = await pg.create_user(
                build_user_create_payload(
                    username=username,
                    group_ids=group_ids or [],
                    data_limit=data_limit,
                    expire_ts=expire,
                    note=note,
                )
            )
        raw_uid = pg_user.get("id") if isinstance(pg_user, dict) else None
        if raw_uid:
            pg_uid = int(raw_uid)

        if shop_rid and getattr(pg, "_login_username", None) is None:
            owner_name = (pg_owner or "").strip()
            if not owner_name or not pg_uid:
                raise ValueError(
                    "کاربر ساخته شد ولی مالکیت قابل تنظیم نیست — اختصاص لغو شد"
                )
            await pg.set_owner_by_id(int(pg_uid), owner_name)
        elif shop_rid and not pg_uid:
            raise ValueError("ساخت کاربر پاسارگارد شناسه برنگرداند — اختصاص لغو شد")

        sub_url = user_subscription_url(pg_user if isinstance(pg_user, dict) else None)
        service = UserService(
            bot_user_id=int(user.id),
            plan_id=int(plan.id),
            pg_user_id=pg_uid,
            pg_username=(pg_user.get("username", username) if isinstance(pg_user, dict) else username),
            subscription_url=sub_url,
            subscription_token=extract_sub_token(sub_url),
            remark=f"manual:{actor_label}",
        )
        plan_days = int(getattr(plan, "duration_days", 0) or 0) or None
        if isinstance(pg_user, dict):
            sync_service_quota_cache(
                service, pg_user, fallback_duration_days=plan_days
            )
        else:
            sync_service_quota_cache(
                service,
                expire_ts=expire,
                data_limit_bytes=data_limit,
                fallback_duration_days=plan_days,
            )
        session.add(service)
        await session.commit()
        await session.refresh(service)
        return service
    except Exception:
        if pg_uid:
            try:
                await pg.delete_user_by_id(int(pg_uid))
            except Exception:
                logger.debug(
                    "rollback PG user failed uid=%s", pg_uid, exc_info=True
                )
        await session.rollback()
        raise


async def admin_credit_user_wallet(
    session: AsyncSession,
    user: BotUser,
    amount: int,
    *,
    actor: str,
    note: str | None = None,
    shop_id: int | None = None,
) -> BotUser:
    """Increase platform or shop wallet (admin). Positive amounts only."""
    amt = _require_admin_wallet_amount(amount)
    if amt <= 0:
        raise ValueError("مبلغ شارژ باید مثبت باشد")
    if amt > MAX_ADMIN_WALLET_CREDIT:
        raise ValueError(f"سقف شارژ دستی {MAX_ADMIN_WALLET_CREDIT:,} تومان است")
    reason = f"شارژ ادمین ({(actor or 'admin')[:64]})"
    if note:
        reason = f"{reason}: {note.strip()[:120]}"
    return await credit_wallet(session, user, amt, reason, shop_id=shop_id)


async def admin_debit_user_wallet(
    session: AsyncSession,
    user: BotUser,
    amount: int,
    *,
    actor: str,
    note: str | None = None,
    shop_id: int | None = None,
) -> BotUser:
    """Decrease platform or shop wallet (admin). Fail-closed on shortfall."""
    amt = _require_admin_wallet_amount(amount)
    if amt <= 0:
        raise ValueError("مبلغ کسر باید مثبت باشد")
    if amt > MAX_ADMIN_WALLET_DEBIT:
        raise ValueError(f"سقف کسر دستی {MAX_ADMIN_WALLET_DEBIT:,} تومان است")
    reason = f"کسر ادمین ({(actor or 'admin')[:64]})"
    if note:
        reason = f"{reason}: {note.strip()[:120]}"
    return await debit_wallet(session, user, amt, reason, shop_id=shop_id)


async def admin_adjust_user_wallet(
    session: AsyncSession,
    user: BotUser,
    amount: int,
    *,
    actor: str,
    note: str | None = None,
    shop_id: int | None = None,
    min_abs: int = MIN_ADMIN_WALLET_ADJUST,
) -> BotUser:
    """Signed admin adjust: positive credits, negative debits (atomic shortfall)."""
    signed = _require_admin_wallet_amount(amount)
    if signed == 0:
        raise ValueError("مبلغ نمی‌تواند صفر باشد")
    abs_amt = abs(signed)
    floor = int(min_abs)
    if floor > 0 and abs_amt < floor:
        raise ValueError(f"حداقل قدرمطلق مبلغ {floor:,} تومان است")
    if signed > 0:
        return await admin_credit_user_wallet(
            session, user, signed, actor=actor, note=note, shop_id=shop_id
        )
    return await admin_debit_user_wallet(
        session, user, abs_amt, actor=actor, note=note, shop_id=shop_id
    )


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
    """Load one owned shop service with plan eagerly (async-safe for Telegram cards)."""
    svc = (
        await session.execute(
            select(UserService)
            .where(UserService.id == int(service_id))
            .options(selectinload(UserService.plan))
        )
    ).scalar_one_or_none()
    if svc is None or int(svc.bot_user_id) != int(bot_user_id):
        raise ValueError("سرویس یافت نشد")
    return svc


def _cache_quota_status(service: UserService, info: dict[str, Any]) -> None:
    """Remember the panel status so audience queries need no live panel call."""
    status = info.get("status")
    if isinstance(status, str):
        service.quota_status = status.strip().lower()[:32]


def sync_service_quota_cache(
    service: UserService,
    info: dict[str, Any] | None = None,
    *,
    expire_ts: int | None = None,
    data_limit_bytes: int | None = None,
    fallback_duration_days: int | None = None,
) -> None:
    """Persist PG expire/data_limit onto UserService for users-list summaries.

    Prefer a live PG ``info`` dict when present. Otherwise apply absolute
    ``expire_ts`` / ``data_limit_bytes`` from the write just issued (partial OK).
    ``quota_expire_at is None`` + synced ⇒ unlimited time;
    ``quota_data_limit_bytes == 0`` + synced ⇒ unlimited volume.

    PasarGuard ``on_hold`` users often have ``expire=0`` until first connect, with
    ``expire_duration`` holding the real TTL — that must NOT sync as unlimited.

    Never lazy-load ``service.plan`` (AsyncSession → MissingGreenlet / xd2s).
    Pass ``fallback_duration_days`` from an already-loaded Plan when available.
    """
    from app.services.formatting import parse_expire

    touched = False
    if isinstance(info, dict):
        _cache_quota_status(service, info)
        has_expire = "expire" in info or "expire_date" in info
        if has_expire:
            exp_raw = info["expire"] if "expire" in info else info.get("expire_date")
            # PG: 0 / null ⇒ unlimited *unless* on_hold with expire_duration
            if exp_raw in (None, "", 0, 0.0) or (
                isinstance(exp_raw, (int, float)) and int(exp_raw) <= 0
            ):
                hold_dur = on_hold_expire_duration_seconds(info)
                if hold_dur is not None:
                    service.quota_expire_at = datetime.now(timezone.utc) + timedelta(
                        seconds=int(hold_dur)
                    )
                elif is_on_hold_status(info.get("status")):
                    # Pending start — approximate from plan so list never shows
                    # «نامحدود» for a timed on_hold service.
                    # CRITICAL: never touch ``service.plan`` relationship here.
                    # Under AsyncSession, lazy-load raises MissingGreenlet /
                    # greenlet_spawn (SQLAlchemy xd2s) and aborts first-time
                    # approve/delivery — second click then resumes successfully.
                    # Only use an already-loaded plan, or the optional override.
                    plan = service.__dict__.get("plan")
                    days = int(getattr(plan, "duration_days", 0) or 0) if plan else 0
                    if days <= 0 and fallback_duration_days is not None:
                        try:
                            days = int(fallback_duration_days or 0)
                        except (TypeError, ValueError):
                            days = 0
                    service.quota_expire_at = datetime.now(timezone.utc) + timedelta(
                        days=max(1, days)
                    )
                else:
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
    staff: dict | None = None,
) -> ServiceSnapshot:
    """Set absolute quota on the linked PG user and refresh local link + list cache."""
    if not service.pg_user_id:
        raise ValueError("سرویس به پاسارگارد وصل نیست")
    assert_staff_pg_user_action(staff, "update")
    pg = await _pg_client_for_bot_service(session, service)
    # Reset is a separate PG ACL (users.reset_usage). Without it, still allow
    # quota update — never use Owner token to bypass a missing reset right.
    do_reset = bool(reset_traffic)
    if do_reset and staff is not None:
        from app.services.pg_access import staff_user_actions

        if not staff_user_actions(staff).get("reset_usage"):
            do_reset = False
    if do_reset:
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
    staff: dict | None = None,
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
        staff=staff,
    )


async def admin_extend_service(
    session: AsyncSession,
    service: UserService,
    *,
    extra_days: int = 0,
    extra_gb: float = 0,
    staff: dict | None = None,
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

    pg = await _pg_client_for_bot_service(session, service)
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
        staff=staff,
    )


async def admin_delete_service(
    session: AsyncSession,
    service: UserService,
    *,
    delete_pg: bool = True,
    staff: dict | None = None,
) -> dict[str, Any]:
    """Hard-delete one shop service and optionally its linked PasarGuard user.

    Caller must verify ownership / shop scope before invoking. Nulls FK refs on
    orders / loyalty / wheel spins, then removes the ``UserService`` row.
    Web staff must have PasarGuard ``users.delete`` (disable fallback needs
    ``users.disable`` / update).
    """
    from sqlalchemy import delete, update

    from app.db.models import LuckyWheelSpin, Order, RewardRedemption

    svc_id = int(service.id)
    bot_user_id = int(service.bot_user_id)
    pg_uid = int(service.pg_user_id) if service.pg_user_id else None
    pg_deleted = False
    pg_disabled = False

    if delete_pg and pg_uid:
        assert_staff_pg_user_action(staff, "delete")
        pg = await _pg_client_for_bot_service(session, service)
        try:
            await pg.delete_user_by_id(pg_uid)
            pg_deleted = True
        except Exception:
            logger.warning(
                "PG delete failed service=%s pg=%s — trying disable",
                svc_id,
                pg_uid,
                exc_info=True,
            )
            try:
                assert_staff_pg_user_action(staff, "disable")
                await pg.set_disabled_by_id(pg_uid, True)
                pg_disabled = True
            except ValueError:
                raise
            except Exception:
                logger.debug("PG disable fallback failed uid=%s", pg_uid, exc_info=True)

        # Fail closed: never wipe the local shop row while the PG user still
        # exists and is enabled — that orphans live quota / billing.
        if not pg_deleted and not pg_disabled:
            raise ValueError(
                "حذف/غیرفعال‌سازی در پاسارگارد ناموفق بود — سرویس محلی حذف نشد"
            )

    await session.execute(
        update(Order).where(Order.service_id == svc_id).values(service_id=None)
    )
    await session.execute(
        update(RewardRedemption)
        .where(RewardRedemption.service_id == svc_id)
        .values(service_id=None)
    )
    await session.execute(
        update(LuckyWheelSpin)
        .where(LuckyWheelSpin.service_id == svc_id)
        .values(service_id=None)
    )
    await session.execute(delete(UserService).where(UserService.id == svc_id))
    await session.commit()
    return {
        "service_id": svc_id,
        "bot_user_id": bot_user_id,
        "pg_user_id": pg_uid,
        "pg_deleted": pg_deleted,
        "pg_disabled": pg_disabled,
    }


async def detach_local_services_for_pg_user(
    session: AsyncSession,
    pg_user_id: int,
    *,
    commit: bool = True,
) -> int:
    """Remove local ``UserService`` rows after a PasarGuard user was deleted.

    Does not call PasarGuard again — used by web/bot PG delete paths (admin +
    reseller) so shop records do not orphan.
    """
    from sqlalchemy import delete, update

    from app.db.models import LuckyWheelSpin, Order, RewardRedemption

    pg_uid = int(pg_user_id)
    rows = list(
        (
            await session.execute(
                select(UserService).where(UserService.pg_user_id == pg_uid)
            )
        ).scalars().all()
    )
    if not rows:
        return 0
    svc_ids = [int(s.id) for s in rows]
    await session.execute(
        update(Order).where(Order.service_id.in_(svc_ids)).values(service_id=None)
    )
    await session.execute(
        update(RewardRedemption)
        .where(RewardRedemption.service_id.in_(svc_ids))
        .values(service_id=None)
    )
    await session.execute(
        update(LuckyWheelSpin)
        .where(LuckyWheelSpin.service_id.in_(svc_ids))
        .values(service_id=None)
    )
    await session.execute(delete(UserService).where(UserService.id.in_(svc_ids)))
    if commit:
        await session.commit()
    return len(svc_ids)


def snapshot_telegram_lines(snap: ServiceSnapshot) -> str:
    svc = snap.service
    plan_name = svc.plan.name if svc.plan else "—"
    lines = [
        f"📦 سرویس #{svc.id} · {plan_name}",
        f"وضعیت: {snap.status_fa}",
        f"حجم: {snap.volume_text} (مانده {snap.remain_gb_text})",
        f"زمان: {snap.expire_text} · مانده {snap.time_label}",
    ]
    if snap.subscription_url:
        lines.append(f"لینک: {snap.subscription_url}")
    if snap.error:
        lines.append(f"⚠️ {snap.error}")
    return "\n".join(lines)
