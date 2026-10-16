"""Lucky Wheel (چرخ شانس) — shared bot + web engine.

Security invariants:
- Server-side weighted RNG only (never trust client prize ids for wins)
- Tenant isolation by reseller_id (platform NULL vs shop)
- Idempotent spins via unique idempotency_key
- Gated by loyalty_enabled AND lucky_wheel_enabled (fail closed)
- Empty prize pool fails closed
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    BotUser,
    LuckyWheelPrize,
    LuckyWheelSpin,
    LuckyWheelUserState,
    UserService,
)
from app.services.loyalty import (
    apply_service_reward,
    credit_points,
    debit_points,
    issue_discount_entitlement,
    loyalty_enabled,
    points_balance,
)
from app.services.service_scope import customer_may_use_service, shop_services_clause
from app.services.users import current_shop_reseller_id

logger = logging.getLogger(__name__)

SETTING_WHEEL_ENABLED = "lucky_wheel_enabled"
SETTING_SPIN_COST = "lucky_wheel_spin_cost_points"
SETTING_DAILY_LIMIT = "lucky_wheel_daily_limit"
SETTING_COOLDOWN = "lucky_wheel_cooldown_seconds"
SETTING_FREE_SPINS_DAILY = "lucky_wheel_free_spins_daily"
SETTING_SUBMENU_ORDER = "loyalty_submenu_order"

PRIZE_TYPE_LABELS: dict[str, str] = {
    "none": "بدون جایزه",
    "points": "امتیاز",
    "traffic_gb": "ترافیک (گیگ)",
    "time_days": "زمان (روز)",
    "wallet_credit": "اعتبار کیف پول",
    "discount_percent": "تخفیف درصدی",
    "free_spin": "چرخش رایگان",
}

DEFAULT_SUBMENU_ORDER = [
    "loy_referral",
    "loy_points",
    "loy_rewards",
    "loy_wheel",
    "loy_history",
]

MAX_WEIGHT = 10_000
MAX_PRIZE_VALUE = 1_000_000
MAX_SPIN_COST = 1_000_000
MAX_DAILY_LIMIT = 100
MAX_COOLDOWN = 86_400
MAX_FREE_SPINS_DAILY = 50
MAX_LABEL_LEN = 128

RandomFn = Callable[[int], int]


@dataclass(frozen=True)
class SpinResult:
    spin: LuckyWheelSpin
    prize_type: str
    prize_value: int
    prize_label: str
    cost_points: int
    used_free_spin: bool
    replayed: bool = False
    discount_code: str | None = None


@dataclass(frozen=True)
class WheelStatus:
    enabled: bool
    spin_cost: int
    daily_limit: int
    cooldown_seconds: int
    free_spins_daily: int
    free_spins_left: int
    spins_today: int
    cooldown_remaining: int
    points_balance: int
    can_spin: bool
    block_reason: str | None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_day(now: datetime | None = None) -> str:
    return (now or _utc_now()).strftime("%Y-%m-%d")


def clamp_int(value: Any, *, lo: int, hi: int, default: int) -> int:
    try:
        n = int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def parse_submenu_order(raw: str | None, *, fill_missing: bool = True) -> list[str]:
    """Parse loyalty submenu CSV.

    ``fill_missing=True`` (default): append any missing catalog keys — used for
    legacy callers. ``fill_missing=False``: keep only listed keys so the panel
    can hide items (empty/invalid CSV falls back to the full default order).
    """
    allowed = set(DEFAULT_SUBMENU_ORDER)
    parts = [p.strip() for p in str(raw or "").split(",") if p.strip()]
    out: list[str] = []
    for p in parts:
        if p in allowed and p not in out:
            out.append(p)
    if not out:
        return list(DEFAULT_SUBMENU_ORDER)
    if fill_missing:
        for p in DEFAULT_SUBMENU_ORDER:
            if p not in out:
                out.append(p)
    return out


async def get_wheel_settings(
    session: AsyncSession, *, reseller_id: int | None
) -> dict[str, Any]:
    from app.services.users import get_setting, on

    enabled_raw = await get_setting(
        session, SETTING_WHEEL_ENABLED, "0", reseller_id=reseller_id
    )
    return {
        "enabled": on(enabled_raw),
        "spin_cost": clamp_int(
            await get_setting(session, SETTING_SPIN_COST, "10", reseller_id=reseller_id),
            lo=0,
            hi=MAX_SPIN_COST,
            default=10,
        ),
        "daily_limit": clamp_int(
            await get_setting(session, SETTING_DAILY_LIMIT, "3", reseller_id=reseller_id),
            lo=0,
            hi=MAX_DAILY_LIMIT,
            default=3,
        ),
        "cooldown_seconds": clamp_int(
            await get_setting(session, SETTING_COOLDOWN, "0", reseller_id=reseller_id),
            lo=0,
            hi=MAX_COOLDOWN,
            default=0,
        ),
        "free_spins_daily": clamp_int(
            await get_setting(
                session, SETTING_FREE_SPINS_DAILY, "0", reseller_id=reseller_id
            ),
            lo=0,
            hi=MAX_FREE_SPINS_DAILY,
            default=0,
        ),
        "submenu_order": parse_submenu_order(
            await get_setting(
                session,
                SETTING_SUBMENU_ORDER,
                ",".join(DEFAULT_SUBMENU_ORDER),
                reseller_id=reseller_id,
            ),
            fill_missing=False,
        ),
    }


async def wheel_feature_enabled(
    session: AsyncSession, *, reseller_id: int | None
) -> bool:
    if not await loyalty_enabled(session, reseller_id=reseller_id):
        return False
    cfg = await get_wheel_settings(session, reseller_id=reseller_id)
    return bool(cfg["enabled"])


def prize_in_scope(prize: LuckyWheelPrize, scope: int | None) -> bool:
    if scope is None:
        return prize.reseller_id is None
    return prize.reseller_id is not None and int(prize.reseller_id) == int(scope)


def shop_prize_scope(_user: BotUser | None = None) -> int | None:
    """Prizes, settings and spins belong to the shop of the bot in use, not the customer's attribution."""
    rid = current_shop_reseller_id()
    return int(rid) if rid else None


async def list_prizes(
    session: AsyncSession,
    *,
    reseller_id: int | None,
    include_archived: bool = False,
) -> list[LuckyWheelPrize]:
    q: Select = select(LuckyWheelPrize)
    if reseller_id is None:
        q = q.where(LuckyWheelPrize.reseller_id.is_(None))
    else:
        q = q.where(LuckyWheelPrize.reseller_id == int(reseller_id))
    if not include_archived:
        q = q.where(LuckyWheelPrize.archived.is_(False))
    q = q.order_by(LuckyWheelPrize.sort_order.asc(), LuckyWheelPrize.id.asc())
    return list((await session.execute(q)).scalars().all())


async def list_active_pool(
    session: AsyncSession, *, reseller_id: int | None
) -> list[LuckyWheelPrize]:
    rows = await list_prizes(session, reseller_id=reseller_id, include_archived=False)
    pool: list[LuckyWheelPrize] = []
    for p in rows:
        if not p.enabled or int(p.weight or 0) <= 0:
            continue
        if p.max_wins_global is not None and int(p.win_count or 0) >= int(p.max_wins_global):
            continue
        pool.append(p)
    return pool


def validate_prize_fields(
    *,
    label: str,
    prize_type: str,
    prize_value: int,
    weight: int,
) -> tuple[str, str, int, int]:
    if prize_type not in PRIZE_TYPE_LABELS:
        raise ValueError("نوع جایزه نامعتبر است")
    clean_label = (label or "").strip()[:MAX_LABEL_LEN] or "جایزه"
    w = clamp_int(weight, lo=1, hi=MAX_WEIGHT, default=1)
    val = clamp_int(prize_value, lo=0, hi=MAX_PRIZE_VALUE, default=0)
    if prize_type == "none":
        val = 0
    elif prize_type == "discount_percent":
        if val <= 0 or val > 100:
            raise ValueError("درصد تخفیف باید بین ۱ تا ۱۰۰ باشد")
    elif prize_type == "free_spin":
        if val <= 0:
            val = 1
        val = min(val, MAX_FREE_SPINS_DAILY)
    elif prize_type in {"points", "traffic_gb", "time_days", "wallet_credit"}:
        if val <= 0:
            raise ValueError("مقدار جایزه باید مثبت باشد")
    return clean_label, prize_type, val, w


async def upsert_prize(
    session: AsyncSession,
    *,
    reseller_id: int | None,
    prize_id: int | None = None,
    label: str,
    prize_type: str,
    prize_value: int,
    weight: int = 1,
    sort_order: int = 0,
    enabled: bool = True,
    max_wins_global: int | None = None,
    max_wins_per_user: int | None = None,
    min_purchase_toman: int = 0,
    max_discount_toman: int | None = None,
    expires_days: int | None = None,
    commit: bool = True,
) -> LuckyWheelPrize:
    clean_label, ptype, val, w = validate_prize_fields(
        label=label, prize_type=prize_type, prize_value=prize_value, weight=weight
    )
    prize: LuckyWheelPrize | None = None
    if prize_id is not None:
        prize = await session.get(LuckyWheelPrize, int(prize_id))
        if not prize or not prize_in_scope(prize, reseller_id):
            raise ValueError("جایزه پیدا نشد")
        if prize.prize_type != ptype:
            raise ValueError("تغییر نوع جایزه مجاز نیست")
    else:
        prize = LuckyWheelPrize(reseller_id=reseller_id, prize_type=ptype)
        session.add(prize)

    prize.label = clean_label
    prize.prize_value = val
    prize.weight = w
    prize.sort_order = int(sort_order)
    prize.enabled = bool(enabled)
    prize.max_wins_global = max_wins_global
    prize.max_wins_per_user = max_wins_per_user
    if ptype == "discount_percent":
        prize.min_purchase_toman = max(0, int(min_purchase_toman or 0))
        prize.max_discount_toman = max_discount_toman
        prize.expires_days = expires_days
    else:
        prize.min_purchase_toman = 0
        prize.max_discount_toman = None
        prize.expires_days = None
    if commit:
        await session.commit()
        await session.refresh(prize)
    else:
        await session.flush()
    return prize


async def archive_prize(
    session: AsyncSession,
    prize_id: int,
    *,
    reseller_id: int | None,
) -> bool:
    prize = await session.get(LuckyWheelPrize, int(prize_id))
    if not prize or not prize_in_scope(prize, reseller_id):
        return False
    prize.archived = True
    prize.enabled = False
    await session.commit()
    return True


def weighted_draw(
    prizes: list[LuckyWheelPrize], *, random_fn: RandomFn | None = None
) -> LuckyWheelPrize:
    """Server-side weighted choice. random_fn(n) → [0, n)."""
    eligible = [p for p in prizes if int(p.weight or 0) > 0]
    if not eligible:
        raise ValueError("استخر جوایز خالی است")
    total = sum(int(p.weight) for p in eligible)
    if total <= 0:
        raise ValueError("استخر جوایز خالی است")
    pick = int(random_fn(total) if random_fn else secrets.randbelow(total))
    if pick < 0 or pick >= total:
        raise ValueError("نتیجه تصادفی نامعتبر")
    cursor = 0
    for p in eligible:
        cursor += int(p.weight)
        if pick < cursor:
            return p
    return eligible[-1]


async def _get_or_create_state(
    session: AsyncSession, user: BotUser, *, for_update: bool = False
) -> LuckyWheelUserState:
    scope = shop_prize_scope()
    scope_clause = (
        LuckyWheelUserState.reseller_id == scope if scope else LuckyWheelUserState.reseller_id.is_(None)
    )
    q = select(LuckyWheelUserState).where(LuckyWheelUserState.user_id == int(user.id), scope_clause)
    if for_update:
        try:
            q = q.with_for_update()
        except Exception:
            pass
    state = (await session.execute(q)).scalar_one_or_none()
    if state:
        return state
    state = LuckyWheelUserState(
        user_id=int(user.id),
        reseller_id=scope,
        free_spins_balance=0,
        spins_today=0,
        spins_day=_utc_day(),
    )
    try:
        async with session.begin_nested():
            session.add(state)
            await session.flush()
    except IntegrityError:
        state = (
            await session.execute(
                select(LuckyWheelUserState).where(
                    LuckyWheelUserState.user_id == int(user.id), scope_clause
                )
            )
        ).scalar_one()
        if for_update:
            try:
                state = (
                    await session.execute(
                        select(LuckyWheelUserState)
                        .where(LuckyWheelUserState.user_id == int(user.id), scope_clause)
                        .with_for_update()
                    )
                ).scalar_one()
            except Exception:
                pass
    return state


def _refresh_day_counters(state: LuckyWheelUserState, *, free_spins_daily: int) -> None:
    today = _utc_day()
    if state.spins_day != today:
        state.spins_day = today
        state.spins_today = 0
        if free_spins_daily > 0:
            state.free_spins_balance = max(int(state.free_spins_balance or 0), free_spins_daily)


async def _user_prize_wins(session: AsyncSession, user_id: int, prize_id: int) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(LuckyWheelSpin)
                .where(
                    LuckyWheelSpin.user_id == int(user_id),
                    LuckyWheelSpin.prize_id == int(prize_id),
                    LuckyWheelSpin.status == "completed",
                )
            )
        ).scalar_one()
        or 0
    )


async def _resolve_service(
    session: AsyncSession, user: BotUser, service_id: int | None
) -> UserService | None:
    scope = shop_prize_scope()
    if service_id is not None:
        svc = await session.get(UserService, int(service_id))
        if not await customer_may_use_service(session, svc, int(user.id), scope):
            raise ValueError("سرویس معتبر نیست")
        if not svc.pg_user_id:
            raise ValueError("سرویس به پنل متصل نیست")
        return svc
    rows = list(
        (
            await session.execute(
                select(UserService)
                .where(
                    UserService.bot_user_id == int(user.id),
                    UserService.pg_user_id.is_not(None),
                    shop_services_clause(scope),
                )
                .order_by(UserService.id.desc())
                .limit(1)
            )
        ).scalars().all()
    )
    return rows[0] if rows else None


async def get_user_wheel_status(session: AsyncSession, user: BotUser) -> WheelStatus:
    scope = shop_prize_scope(user)
    if not await wheel_feature_enabled(session, reseller_id=scope):
        return WheelStatus(
            enabled=False,
            spin_cost=0,
            daily_limit=0,
            cooldown_seconds=0,
            free_spins_daily=0,
            free_spins_left=0,
            spins_today=0,
            cooldown_remaining=0,
            points_balance=await points_balance(session, user, scope),
            can_spin=False,
            block_reason="چرخ شانس غیرفعال است",
        )
    cfg = await get_wheel_settings(session, reseller_id=scope)
    state = await _get_or_create_state(session, user)
    _refresh_day_counters(state, free_spins_daily=int(cfg["free_spins_daily"]))
    await session.flush()

    cooldown_remaining = 0
    if cfg["cooldown_seconds"] > 0 and state.last_spin_at:
        last = state.last_spin_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        elapsed = int((_utc_now() - last).total_seconds())
        cooldown_remaining = max(0, int(cfg["cooldown_seconds"]) - elapsed)

    free_left = int(state.free_spins_balance or 0)
    spins_today = int(state.spins_today or 0)
    daily_limit = int(cfg["daily_limit"])
    cost = int(cfg["spin_cost"])
    pts = await points_balance(session, user, scope)

    reason: str | None = None
    can = True
    pool = await list_active_pool(session, reseller_id=scope)
    if not pool:
        can = False
        reason = "استخر جوایز خالی است"
    elif daily_limit > 0 and spins_today >= daily_limit:
        can = False
        reason = "سقف چرخش روزانه پر شده"
    elif cooldown_remaining > 0:
        can = False
        reason = f"کمی صبر کنید ({cooldown_remaining} ثانیه)"
    elif free_left <= 0 and cost > 0 and pts < cost:
        can = False
        reason = "امتیاز کافی نیست"

    return WheelStatus(
        enabled=True,
        spin_cost=cost,
        daily_limit=daily_limit,
        cooldown_seconds=int(cfg["cooldown_seconds"]),
        free_spins_daily=int(cfg["free_spins_daily"]),
        free_spins_left=free_left,
        spins_today=spins_today,
        cooldown_remaining=cooldown_remaining,
        points_balance=pts,
        can_spin=can,
        block_reason=reason,
    )


async def spin(
    session: AsyncSession,
    user: BotUser,
    *,
    idempotency_key: str,
    service_id: int | None = None,
    random_fn: RandomFn | None = None,
) -> SpinResult:
    """Execute one spin. Idempotent on idempotency_key. Fail closed."""
    key = (idempotency_key or "").strip()[:128]
    if not key:
        raise ValueError("کلید یکتایی نامعتبر است")

    existing = (
        await session.execute(
            select(LuckyWheelSpin).where(LuckyWheelSpin.idempotency_key == key)
        )
    ).scalar_one_or_none()
    if existing:
        return SpinResult(
            spin=existing,
            prize_type=existing.prize_type,
            prize_value=int(existing.prize_value or 0),
            prize_label=existing.prize_label_snapshot or "",
            cost_points=int(existing.cost_points or 0),
            used_free_spin=bool(existing.used_free_spin),
            replayed=True,
            discount_code=existing.discount_code,
        )

    scope = shop_prize_scope(user)
    if not await wheel_feature_enabled(session, reseller_id=scope):
        raise ValueError("چرخ شانس غیرفعال است")

    cfg = await get_wheel_settings(session, reseller_id=scope)
    state = await _get_or_create_state(session, user, for_update=True)
    _refresh_day_counters(state, free_spins_daily=int(cfg["free_spins_daily"]))

    if cfg["cooldown_seconds"] > 0 and state.last_spin_at:
        last = state.last_spin_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        elapsed = int((_utc_now() - last).total_seconds())
        if elapsed < int(cfg["cooldown_seconds"]):
            raise ValueError(
                f"کمی صبر کنید ({int(cfg['cooldown_seconds']) - elapsed} ثانیه)"
            )

    daily_limit = int(cfg["daily_limit"])
    if daily_limit > 0 and int(state.spins_today or 0) >= daily_limit:
        raise ValueError("سقف چرخش روزانه پر شده")

    pool = await list_active_pool(session, reseller_id=scope)
    if not pool:
        raise ValueError("استخر جوایز خالی است")

    eligible: list[LuckyWheelPrize] = []
    for p in pool:
        if p.max_wins_per_user is not None:
            wins = await _user_prize_wins(session, user.id, p.id)
            if wins >= int(p.max_wins_per_user):
                continue
        eligible.append(p)
    if not eligible:
        raise ValueError("استخر جوایز خالی است")

    used_free = False
    cost = 0
    pts_tx_id: int | None = None
    if int(state.free_spins_balance or 0) > 0:
        used_free = True
        state.free_spins_balance = int(state.free_spins_balance) - 1
    else:
        cost = int(cfg["spin_cost"])
        if cost > 0:
            pts_tx = await debit_points(
                session,
                user,
                cost,
                tx_type="spend",
                source="lucky_wheel",
                reference=key[:64],
                description="چرخش چرخ شانس",
                idempotency_key=f"wheel_pts:{key}",
                meta={"idempotency_key": key},
                commit=False,
                shop_id=scope,
            )
            pts_tx_id = pts_tx.id if pts_tx else None

    prize = weighted_draw(eligible, random_fn=random_fn)

    if prize.max_wins_global is not None and int(prize.win_count or 0) >= int(
        prize.max_wins_global
    ):
        raise ValueError("سقف این جایزه پر شده؛ دوباره تلاش کنید")

    service: UserService | None = None
    if prize.prize_type in ("traffic_gb", "time_days"):
        service = await _resolve_service(session, user, service_id)
        if not service:
            raise ValueError("برای این جایزه به یک سرویس فعال نیاز دارید")

    spin_row = LuckyWheelSpin(
        user_id=int(user.id),
        reseller_id=scope,
        prize_id=int(prize.id),
        prize_type=prize.prize_type,
        prize_value=int(prize.prize_value or 0),
        prize_label_snapshot=(prize.label or "")[:MAX_LABEL_LEN],
        cost_points=cost,
        used_free_spin=used_free,
        status="completed",
        points_tx_id=pts_tx_id,
        service_id=int(service.id) if service else None,
        idempotency_key=key,
    )
    session.add(spin_row)
    await session.flush()

    discount_code: str | None = None
    meta: dict[str, Any] = {"prize_id": prize.id}

    try:
        ptype = prize.prize_type
        pval = int(prize.prize_value or 0)
        if ptype == "points" and pval > 0:
            await credit_points(
                session,
                user,
                pval,
                tx_type="earn",
                source="lucky_wheel",
                reference=str(spin_row.id),
                description=f"جایزه چرخ شانس: {prize.label}",
                idempotency_key=f"wheel_win_pts:{key}",
                meta={"spin_id": spin_row.id},
                commit=False,
                shop_id=scope,
            )
        elif ptype == "wallet_credit" and pval > 0:
            from app.services.wallet import credit_wallet

            await credit_wallet(
                session,
                user,
                pval,
                f"lucky_wheel:{spin_row.id}:{key}",
                shop_id=int(scope) if scope is not None else None,
                commit=False,
            )
        elif ptype in ("traffic_gb", "time_days") and service is not None:
            await apply_service_reward(session, user, service, ptype, pval)
        elif ptype == "discount_percent" and pval > 0:
            ent = await issue_discount_entitlement(
                session,
                user,
                percent=pval,
                min_purchase_toman=int(prize.min_purchase_toman or 0),
                max_discount_toman=int(prize.max_discount_toman)
                if prize.max_discount_toman is not None
                else None,
                expires_days=int(prize.expires_days)
                if prize.expires_days is not None
                else None,
                wheel_spin_id=int(spin_row.id),
                reseller_id=int(scope) if scope is not None else None,
            )
            discount_code = ent.code
            spin_row.discount_code = ent.code
            meta["discount_code"] = ent.code
        elif ptype == "free_spin":
            grant = max(1, pval)
            state.free_spins_balance = int(state.free_spins_balance or 0) + grant
            meta["free_spins_granted"] = grant

        prize.win_count = int(prize.win_count or 0) + 1
        state.spins_today = int(state.spins_today or 0) + 1
        state.last_spin_at = _utc_now()
        spin_row.meta_json = json.dumps(meta, ensure_ascii=False)

        await session.commit()
        await session.refresh(spin_row)
        await session.refresh(user)
    except IntegrityError:
        await session.rollback()
        again = (
            await session.execute(
                select(LuckyWheelSpin).where(LuckyWheelSpin.idempotency_key == key)
            )
        ).scalar_one_or_none()
        if again:
            return SpinResult(
                spin=again,
                prize_type=again.prize_type,
                prize_value=int(again.prize_value or 0),
                prize_label=again.prize_label_snapshot or "",
                cost_points=int(again.cost_points or 0),
                used_free_spin=bool(again.used_free_spin),
                replayed=True,
                discount_code=again.discount_code,
            )
        raise
    except Exception:
        await session.rollback()
        raise

    return SpinResult(
        spin=spin_row,
        prize_type=spin_row.prize_type,
        prize_value=int(spin_row.prize_value or 0),
        prize_label=spin_row.prize_label_snapshot or "",
        cost_points=int(spin_row.cost_points or 0),
        used_free_spin=bool(spin_row.used_free_spin),
        replayed=False,
        discount_code=discount_code,
    )


async def list_recent_spins(
    session: AsyncSession,
    *,
    reseller_id: int | None,
    limit: int = 50,
) -> list[LuckyWheelSpin]:
    q = (
        select(LuckyWheelSpin)
        .order_by(LuckyWheelSpin.id.desc())
        .limit(max(1, min(200, limit)))
    )
    if reseller_id is None:
        q = q.where(LuckyWheelSpin.reseller_id.is_(None))
    else:
        q = q.where(LuckyWheelSpin.reseller_id == int(reseller_id))
    return list((await session.execute(q)).scalars().all())


async def overview_wheel_metrics(
    session: AsyncSession, *, reseller_id: int | None
) -> dict[str, Any]:
    prizes = await list_prizes(session, reseller_id=reseller_id, include_archived=False)
    active = [p for p in prizes if p.enabled]
    spins_q = select(func.count()).select_from(LuckyWheelSpin).where(
        LuckyWheelSpin.status == "completed"
    )
    if reseller_id is None:
        spins_q = spins_q.where(LuckyWheelSpin.reseller_id.is_(None))
    else:
        spins_q = spins_q.where(LuckyWheelSpin.reseller_id == int(reseller_id))
    total_spins = int((await session.execute(spins_q)).scalar_one() or 0)
    cfg = await get_wheel_settings(session, reseller_id=reseller_id)
    return {
        "wheel_enabled": bool(cfg["enabled"]),
        "active_prizes": len(active),
        "total_spins": total_spins,
        "spin_cost": int(cfg["spin_cost"]),
    }
