from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import or_, select

from app.db.models import BotUser, ResellerProfile, UserService
from app.db.session import SessionLocal
from app.services.formatting import format_bytes_ratio, parse_expire
from app.services.pasarguard import get_pg

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()

# Bound concurrent PG subscription lookups during expiry scan
_PG_FETCH_CONCURRENCY = 8


def _as_int(val, default: int) -> int:
    try:
        return int(float(val))
    except Exception:
        return default


def _resolve_send_bot(
    main_bot: Bot,
    reseller_user_id: int | None,
    profile_by_user: dict[int, int],
) -> Bot:
    """Prefer the shop's dedicated bot when alerting that shop's customers."""
    if not reseller_user_id:
        return main_bot
    from app.services.reseller_bots import get_reseller_bot_manager

    mgr = get_reseller_bot_manager()
    if not mgr:
        return main_bot
    pid = profile_by_user.get(int(reseller_user_id))
    if pid is None:
        return main_bot
    shop_bot = mgr.bot_for_profile_id(int(pid))
    return shop_bot or main_bot


async def check_expiring_services(bot: Bot) -> None:
    async with SessionLocal() as session:
        from app.services.users import get_all_settings, on

        settings_cache: dict[int | None, dict] = {}

        async def ui_for(reseller_user_id: int | None) -> dict:
            key = int(reseller_user_id) if reseller_user_id else None
            if key not in settings_cache:
                settings_cache[key] = await get_all_settings(session, reseller_id=key)
            return settings_cache[key]

        global_ui = await ui_for(None)
        # Fast path: if platform alerts are off, only bother when a reseller shop enables them.
        reseller_ids_with_alerts: set[int] = set()
        platform_on = on(global_ui.get("user_alert_low_enabled", "0"))
        if not platform_on:
            from app.db.models import ResellerSetting

            # One query for the alert flag — avoid N× full get_all_settings just to discover shops
            rows = (
                await session.execute(
                    select(ResellerSetting.reseller_user_id, ResellerSetting.value).where(
                        ResellerSetting.key == "user_alert_low_enabled",
                        ResellerSetting.reseller_user_id.in_(
                            select(ResellerProfile.user_id).where(
                                ResellerProfile.is_active.is_(True)
                            )
                        ),
                    )
                )
            ).all()
            for rid, raw in rows:
                if rid is not None and on(raw or "0"):
                    reseller_ids_with_alerts.add(int(rid))
            if not reseller_ids_with_alerts:
                return

        profiles = (
            await session.execute(
                select(ResellerProfile.user_id, ResellerProfile.id).where(
                    ResellerProfile.is_active.is_(True),
                    ResellerProfile.bot_token.is_not(None),
                )
            )
        ).all()
        profile_by_user = {int(uid): int(pid) for uid, pid in profiles if uid is not None}

        pg = get_pg()
        now = datetime.now(timezone.utc)
        batch_size = 150
        last_id = 0

        while True:
            # Keyset batches — avoid loading every pending service in one query.
            svc_q = (
                select(UserService)
                .where(
                    UserService.subscription_token.is_not(None),
                    or_(
                        UserService.notified_expire.is_(False),
                        UserService.notified_traffic.is_(False),
                    ),
                    UserService.id > last_id,
                )
                .order_by(UserService.id)
                .limit(batch_size)
            )
            if not platform_on and reseller_ids_with_alerts:
                # Narrow to shops that actually have alerts on before PG fan-out
                svc_q = svc_q.join(BotUser, BotUser.id == UserService.bot_user_id).where(
                    BotUser.reseller_id.in_(reseller_ids_with_alerts)
                )
            services = list((await session.execute(svc_q)).scalars().all())
            if not services:
                break
            last_id = int(services[-1].id)

            # Prefetch users for alert eligibility before any remote PG call
            user_ids = {svc.bot_user_id for svc in services}
            users_by_id: dict[int, BotUser] = {}
            if user_ids:
                rows = (
                    await session.execute(select(BotUser).where(BotUser.id.in_(user_ids)))
                ).scalars().all()
                users_by_id = {u.id: u for u in rows}

            # Prefer shop of the fulfilling order (remark order:{id}) over sticky
            # BotUser.reseller_id — first-touch attribution must not leak alerts.
            order_ids: set[int] = set()
            for svc in services:
                remark = (getattr(svc, "remark", None) or "").strip()
                if remark.startswith("order:"):
                    try:
                        order_ids.add(int(remark.split(":", 1)[1].split()[0]))
                    except (TypeError, ValueError):
                        pass
            order_shop_by_id: dict[int, int | None] = {}
            if order_ids:
                from app.db.models import Order

                for oid, orid in (
                    await session.execute(
                        select(Order.id, Order.reseller_id).where(Order.id.in_(order_ids))
                    )
                ).all():
                    order_shop_by_id[int(oid)] = int(orid) if orid is not None else None

            def _shop_rid_for(svc: UserService, user: BotUser) -> int | None:
                remark = (getattr(svc, "remark", None) or "").strip()
                if remark.startswith("order:"):
                    try:
                        oid = int(remark.split(":", 1)[1].split()[0])
                    except (TypeError, ValueError):
                        oid = 0
                    if oid in order_shop_by_id:
                        return order_shop_by_id[oid]
                return int(user.reseller_id) if user.reseller_id else None

            eligible: list[tuple[UserService, BotUser, dict, int, int, int | None]] = []
            for svc in services:
                user = users_by_id.get(svc.bot_user_id)
                if not user or user.is_blocked:
                    continue

                rid = _shop_rid_for(svc, user)
                if not platform_on:
                    if rid is None or rid not in reseller_ids_with_alerts:
                        continue

                ui = await ui_for(rid)
                if not on(ui.get("user_alert_low_enabled", "0")):
                    continue

                traffic_pct = max(1, min(99, _as_int(ui.get("user_alert_low_traffic_pct"), 20)))
                time_pct = max(1, min(99, _as_int(ui.get("user_alert_low_time_pct"), 20)))
                eligible.append((svc, user, ui, traffic_pct, time_pct, rid))

            if not eligible:
                if len(services) < batch_size:
                    break
                continue

            sem = asyncio.Semaphore(_PG_FETCH_CONCURRENCY)

            async def _fetch_info(token: str | None, sub_url: str | None):
                async with sem:
                    try:
                        return await asyncio.wait_for(
                            pg.subscription_info(token, subscription_url=sub_url),
                            timeout=12,
                        )
                    except Exception:
                        logger.debug("subscription_info failed for service fetch", exc_info=True)
                        return None

            infos = await asyncio.gather(
                *[
                    _fetch_info(svc.subscription_token, svc.subscription_url)
                    for svc, *_rest in eligible
                ]
            )

            for (svc, user, _ui, traffic_pct, time_pct, shop_rid), info in zip(eligible, infos):
                if not info:
                    continue
                send_bot = _resolve_send_bot(bot, shop_rid, profile_by_user)

                # --- remaining TIME percent ---
                if not svc.notified_expire:
                    expire = parse_expire(info.get("expire"))
                    created = svc.created_at
                    if created and created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    if expire and created and expire > created:
                        total = (expire - created).total_seconds()
                        remaining = (expire - now).total_seconds()
                        if total > 0 and remaining >= 0:
                            rem_pct = (remaining / total) * 100
                            if rem_pct <= time_pct:
                                try:
                                    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

                                    renew_kb = None
                                    if on(_ui.get("one_tap_renew_enabled", "1")):
                                        from app.services.button_styles import style_kwargs

                                        renew_kb = InlineKeyboardMarkup(
                                            inline_keyboard=[
                                                [
                                                    InlineKeyboardButton(
                                                        text="🔄 تمدید یک‌ضربی",
                                                        callback_data=f"svc:renew:{svc.id}",
                                                        **style_kwargs(
                                                            _ui, "one_tap_renew", fallback="primary"
                                                        ),
                                                    )
                                                ]
                                            ]
                                        )
                                    await send_bot.send_message(
                                        user.telegram_id,
                                        f"⏰ زمان سرویس <b>{svc.pg_username}</b> به کمتر از "
                                        f"<b>{time_pct}٪</b> رسیده است.\n"
                                        "از بخش سرویس‌ها تمدید کنید."
                                        + ("\nیا دکمه زیر را بزنید:" if renew_kb else ""),
                                        parse_mode="HTML",
                                        reply_markup=renew_kb,
                                    )
                                    svc.notified_expire = True
                                    if renew_kb is not None:
                                        svc.renew_nudge_sent_at = now
                                except Exception:
                                    logger.debug(
                                        "expire alert send failed tg=%s", user.telegram_id, exc_info=True
                                    )

                # --- remaining TRAFFIC percent ---
                if not svc.notified_traffic:
                    used = float(info.get("used_traffic") or 0)
                    limit = info.get("data_limit")
                    if limit:
                        try:
                            limit_f = float(limit)
                        except Exception:
                            limit_f = 0
                        if limit_f > 0:
                            rem_pct = max(0.0, (1.0 - (used / limit_f)) * 100)
                            if rem_pct <= traffic_pct:
                                try:
                                    await send_bot.send_message(
                                        user.telegram_id,
                                        f"📉 حجم باقی‌مانده سرویس <b>{svc.pg_username}</b> کمتر از "
                                        f"<b>{traffic_pct}٪</b> است "
                                        f"({format_bytes_ratio(used, limit_f, joiner=' از ')}).",
                                        parse_mode="HTML",
                                    )
                                    svc.notified_traffic = True
                                except Exception:
                                    logger.debug(
                                        "traffic alert send failed tg=%s", user.telegram_id, exc_info=True
                                    )

            await session.commit()
            if len(services) < batch_size:
                break



# Track last billing run so tick minutes setting works without reschedule
_last_billing_tick_at: float | None = None


async def run_reseller_billing_tick() -> None:
    """Periodic PAYG usage charge; respects billing_tick_minutes setting."""
    global _last_billing_tick_at
    import time

    async with SessionLocal() as session:
        try:
            from app.services.billing import get_tick_minutes, is_billing_enabled, run_billing_tick

            if not await is_billing_enabled(session):
                return
            mins = await get_tick_minutes(session)
            now = time.time()
            if _last_billing_tick_at is not None and (now - _last_billing_tick_at) < mins * 60:
                return
            stats = await run_billing_tick(session)
            _last_billing_tick_at = now
            if stats.get("checked"):
                logger.info("billing tick %s", stats)
        except Exception:
            logger.exception("billing tick failed")


async def cleanup_stale_pending_orders() -> None:
    """Hourly: cancel unpaid pending orders past TTL (platform + shops)."""
    async with SessionLocal() as session:
        try:
            from app.services.orders import cancel_stale_pending_for_settings

            total = await cancel_stale_pending_for_settings(session, reseller_id=None)
            shop_ids = list(
                (
                    await session.execute(
                        select(ResellerProfile.user_id).where(
                            ResellerProfile.is_active.is_(True)
                        )
                    )
                ).scalars().all()
            )
            for rid in shop_ids:
                try:
                    n = await cancel_stale_pending_for_settings(
                        session, reseller_id=int(rid)
                    )
                    total += n
                except Exception:
                    logger.exception("pending order cleanup failed for shop %s", rid)
            if total:
                logger.info("pending order cleanup cancelled=%s", total)
        except Exception:
            logger.exception("pending order cleanup failed")


async def run_scheduled_backup() -> None:
    """Nightly backup + archive validation when enabled."""
    async with SessionLocal() as session:
        try:
            from datetime import datetime, timezone
            from pathlib import Path

            from app.services.backup import create_backup, validate_backup_archive
            from app.services.users import get_setting, on, set_setting

            if not on(await get_setting(session, "backup_schedule_enabled", "1")):
                return
            try:
                hour = int(await get_setting(session, "backup_schedule_hour", "3") or 3)
            except Exception:
                hour = 3
            now = datetime.now(timezone.utc)
            if now.hour != max(0, min(23, hour)):
                return
            # Dedup same UTC day
            last = (await get_setting(session, "backup_last_ok_at", "")) or ""
            day_key = now.strftime("%Y-%m-%d")
            if last.startswith(day_key):
                return
            include_env = on(await get_setting(session, "backup_include_env_scheduled", "0"))
            meta = create_backup(
                note="scheduled",
                include_env=include_env,
                created_by="scheduler",
            )
            path = Path(str(meta.get("path") or ""))
            ok, err, _ = validate_backup_archive(path)
            await set_setting(session, "backup_last_verify_ok", "1" if ok else "0")
            if not ok:
                logger.warning("scheduled backup verify failed: %s", err)
            await set_setting(session, "backup_last_ok_at", now.isoformat())
            await session.commit()
            logger.info("scheduled backup ok id=%s", meta.get("id"))
        except Exception:
            logger.exception("scheduled backup failed")


async def run_admin_daily_report(bot: Bot) -> None:
    """Send nightly ops summary to platform admins and opted-in shops."""
    async with SessionLocal() as session:
        try:
            from datetime import datetime, timezone

            from sqlalchemy import select

            from app.config import get_settings
            from app.db.models import OrgPrincipal, ResellerProfile
            from app.services.daily_report import (
                ACTOR_OWNER,
                ACTOR_SHOP,
                DEFAULT_REPORT_TEMPLATE,
                build_daily_report,
                shop_daily_report_chat_ids,
            )
            from app.services.org_principals import DEPTH_ONE, STATUS_ACTIVE
            from app.services.reseller_bots import open_notify_bot_for_reseller
            from app.services.subordinate_report import (
                SETTING_ENABLED as SUB_REPORT_ENABLED,
                SETTING_LAST as SUB_REPORT_LAST,
                build_subordinate_report_for_parent,
                load_owner_parent,
            )
            from app.services.users import get_all_settings, get_setting, on, set_setting

            now = datetime.now(timezone.utc)
            day_key = now.strftime("%Y-%m-%d")

            async def _traffic_map_for_shops(shops) -> dict[int, str]:
                """Best-effort PG traffic labels; never fails the digest."""
                out: dict[int, str] = {}
                if not shops:
                    return out
                try:
                    from app.services.pasarguard import get_pg
                    from app.services.pg_overview import admin_usage_snapshot

                    pg = get_pg()
                    admins = await pg.get_admins()
                    roles = await pg.get_admin_roles()
                    role_by_id = {
                        int(r["id"]): r
                        for r in (roles or [])
                        if isinstance(r, dict) and r.get("id") is not None
                    }
                    by_username = {
                        str(a.get("username") or "").strip().lower(): a
                        for a in (admins or [])
                        if isinstance(a, dict) and a.get("username")
                    }
                    profile_ids = [int(s.reseller_profile_id) for s in shops]
                    profiles = (
                        await session.execute(
                            select(ResellerProfile).where(
                                ResellerProfile.id.in_(profile_ids or [0])
                            )
                        )
                    ).scalars().all()
                    for profile in profiles:
                        uname = (profile.pg_admin_username or "").strip().lower()
                        if not uname:
                            continue
                        admin = by_username.get(uname)
                        if not admin:
                            continue
                        role = None
                        rid = admin.get("role_id")
                        if rid is not None:
                            try:
                                role = role_by_id.get(int(rid))
                            except (TypeError, ValueError):
                                role = None
                        snap = admin_usage_snapshot(admin, role)
                        out[int(profile.user_id)] = str(
                            snap.get("traffic_text") or "—"
                        )
                except Exception:
                    logger.debug(
                        "subordinate report traffic map skipped", exc_info=True
                    )
                return out

            # —— Platform Owner report ——
            try:
                hour = int(await get_setting(session, "admin_daily_report_hour", "0") or 0)
            except Exception:
                hour = 0
            owner_hour_ok = now.hour == max(0, min(23, hour))

            if owner_hour_ok and on(
                await get_setting(session, "admin_daily_report_enabled", "1")
            ):
                last = (await get_setting(session, "admin_daily_report_last", "")) or ""
                if last != day_key:
                    from app.services.daily_report import build_daily_report_outbound

                    text, report_kw = await build_daily_report_outbound(
                        session,
                        reseller_id=None,
                        actor=ACTOR_OWNER,
                        admin_name="مالک سیستم",
                        template=(
                            await get_setting(session, "admin_daily_report_template", "")
                        )
                        or DEFAULT_REPORT_TEMPLATE,
                        metrics_raw=await get_setting(
                            session, "admin_daily_report_metrics", ""
                        ),
                    )
                    for aid in get_settings().admin_ids or []:
                        try:
                            await bot.send_message(
                                int(aid),
                                text,
                                **{"parse_mode": "HTML", **report_kw},
                            )
                        except Exception:
                            logger.debug(
                                "daily report send failed admin=%s", aid, exc_info=True
                            )
                    await set_setting(session, "admin_daily_report_last", day_key)
                    await session.commit()

            # —— Platform Owner: direct L1 subordinate digest ——
            if owner_hour_ok and on(
                await get_setting(session, SUB_REPORT_ENABLED, "1")
            ):
                last_sub = (await get_setting(session, SUB_REPORT_LAST, "")) or ""
                if last_sub != day_key:
                    owner = await load_owner_parent(session)
                    if owner is not None:
                        from app.services.subordinate_report import (
                            list_direct_subordinate_shops,
                        )

                        shops = await list_direct_subordinate_shops(
                            session, parent=owner
                        )
                        if shops:
                            traffic = await _traffic_map_for_shops(shops)
                            text = await build_subordinate_report_for_parent(
                                session, parent=owner, traffic_by_user_id=traffic
                            )
                            if text:
                                for aid in get_settings().admin_ids or []:
                                    try:
                                        await bot.send_message(
                                            int(aid), text, parse_mode="HTML"
                                        )
                                    except Exception:
                                        logger.debug(
                                            "subordinate report send failed admin=%s",
                                            aid,
                                            exc_info=True,
                                        )
                        await set_setting(session, SUB_REPORT_LAST, day_key)
                        await session.commit()

            # —— Shop reports (explicit reseller_id scope) ——
            profiles = (
                await session.execute(
                    select(ResellerProfile).where(
                        ResellerProfile.is_active.is_(True),
                        ResellerProfile.bot_token.is_not(None),
                    )
                )
            ).scalars().all()
            for profile in profiles:
                rid = int(profile.user_id)
                ui = await get_all_settings(session, reseller_id=rid)
                try:
                    hour = int(ui.get("admin_daily_report_hour") or 0)
                except Exception:
                    hour = 0
                hour_ok = now.hour == max(0, min(23, hour))
                if not hour_ok:
                    continue

                if on(ui.get("admin_daily_report_enabled")) and (
                    ui.get("admin_daily_report_last") or ""
                ).strip() != day_key:
                    from app.services.daily_report import build_daily_report_outbound
                    from app.services.rich_text import rich_plain_text

                    admin_name = (
                        rich_plain_text(ui.get("shop_title"))
                        or profile.bot_username
                        or "فروشگاه"
                    ).strip()
                    text, report_kw = await build_daily_report_outbound(
                        session,
                        reseller_id=rid,
                        actor=ACTOR_SHOP,
                        admin_name=admin_name,
                        template=ui.get("admin_daily_report_template")
                        or DEFAULT_REPORT_TEMPLATE,
                        metrics_raw=ui.get("admin_daily_report_metrics"),
                    )
                    shop_bot, should_close = await open_notify_bot_for_reseller(
                        session, rid
                    )
                    if shop_bot is not None:
                        try:
                            for chat_id in await shop_daily_report_chat_ids(
                                session, rid
                            ):
                                try:
                                    await shop_bot.send_message(
                                        int(chat_id),
                                        text,
                                        **{"parse_mode": "HTML", **report_kw},
                                    )
                                except Exception:
                                    logger.debug(
                                        "daily report shop send failed rid=%s chat=%s",
                                        rid,
                                        chat_id,
                                        exc_info=True,
                                    )
                            await set_setting(
                                session,
                                "admin_daily_report_last",
                                day_key,
                                reseller_id=rid,
                            )
                            await session.commit()
                        finally:
                            if should_close:
                                try:
                                    await shop_bot.session.close()
                                except Exception:
                                    logger.debug(
                                        "daily report shop bot session close failed rid=%s",
                                        rid,
                                        exc_info=True,
                                    )

                # L1 shop → L2 subordinate digest (same hour, opt-in)
                if on(ui.get(SUB_REPORT_ENABLED)) and (
                    ui.get(SUB_REPORT_LAST) or ""
                ).strip() != day_key:
                    parent = (
                        await session.execute(
                            select(OrgPrincipal).where(
                                OrgPrincipal.reseller_profile_id == int(profile.id),
                                OrgPrincipal.depth == DEPTH_ONE,
                                OrgPrincipal.status == STATUS_ACTIVE,
                            )
                        )
                    ).scalar_one_or_none()
                    if parent is None:
                        await set_setting(
                            session, SUB_REPORT_LAST, day_key, reseller_id=rid
                        )
                        await session.commit()
                        continue
                    from app.services.subordinate_report import (
                        list_direct_subordinate_shops,
                    )

                    shops = await list_direct_subordinate_shops(
                        session, parent=parent
                    )
                    if shops:
                        traffic = await _traffic_map_for_shops(shops)
                        text = await build_subordinate_report_for_parent(
                            session, parent=parent, traffic_by_user_id=traffic
                        )
                        if text:
                            shop_bot, should_close = await open_notify_bot_for_reseller(
                                session, rid
                            )
                            if shop_bot is not None:
                                try:
                                    for chat_id in await shop_daily_report_chat_ids(
                                        session, rid
                                    ):
                                        try:
                                            await shop_bot.send_message(
                                                int(chat_id), text, parse_mode="HTML"
                                            )
                                        except Exception:
                                            logger.debug(
                                                "subordinate report shop send failed rid=%s chat=%s",
                                                rid,
                                                chat_id,
                                                exc_info=True,
                                            )
                                finally:
                                    if should_close:
                                        try:
                                            await shop_bot.session.close()
                                        except Exception:
                                            logger.debug(
                                                "subordinate report shop bot close failed rid=%s",
                                                rid,
                                                exc_info=True,
                                            )
                    await set_setting(
                        session, SUB_REPORT_LAST, day_key, reseller_id=rid
                    )
                    await session.commit()
        except Exception:
            logger.exception("admin daily report failed")


async def run_pg_admin_subscription_tick() -> None:
    """Expire due PG-admin subscriptions (reseller + pg_staff shared clock)."""
    try:
        async with SessionLocal() as session:
            from app.services.pg_admin_subscription import run_subscription_expiry_tick

            stats = await run_subscription_expiry_tick(session)
            if stats.get("expired") or stats.get("warned") or stats.get("errors"):
                logger.info("pg admin subscription tick: %s", stats)
    except Exception:
        logger.exception("pg admin subscription tick failed")


def start_scheduler(bot: Bot) -> None:
    if scheduler.running:
        return
    scheduler.add_job(
        check_expiring_services,
        "interval",
        hours=6,
        args=[bot],
        id="expiry",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    # Job fires every minute; no-ops until billing_tick_minutes elapses.
    scheduler.add_job(
        run_reseller_billing_tick,
        "interval",
        minutes=1,
        id="billing_tick",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
    )
    scheduler.add_job(
        run_pg_admin_subscription_tick,
        "interval",
        minutes=5,
        id="pg_admin_subscription",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    scheduler.add_job(
        cleanup_stale_pending_orders,
        "interval",
        hours=1,
        id="pending_order_cleanup",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        run_scheduled_backup,
        "interval",
        minutes=30,
        id="scheduled_backup",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        run_admin_daily_report,
        "interval",
        minutes=30,
        args=[bot],
        id="admin_daily_report",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.start()
    logger.info("Scheduler started")
