from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import or_, select

from app.db.models import BotUser, ResellerProfile, UserService
from app.db.session import SessionLocal
from app.jobs.bot_profile_photo import run_bot_profile_photo_sync
from app.jobs.cancellation_notifications import run_cancellation_notifications
from app.jobs.chat_message_purge import run_chat_message_purge
from app.jobs.connection_status import run_connection_status
from app.jobs.customer_notices import run_customer_notices
from app.jobs.panel_alert_notifications import run_panel_alert_notifications
from app.jobs.pg_link_watchdog import run_pg_link_watchdog
from app.jobs.service_cleanup import run_service_cleanup
from app.jobs.user_photo_cache import run_user_photo_prune
from app.jobs.volume_external_sync import run_volume_external_sync
from app.services.alert_rearm import rearm_flagged_alerts
from app.services.formatting import format_bytes_ratio, parse_expire
from app.services.pasarguard import get_pg
from app.services.replaceable_notices import DAILY_REPORT_KEY, SUBORDINATE_REPORT_KEY, send_replacing_report
from app.services.staff_notice_buttons import platform_report_markup, shop_report_markup, subordinate_report_markup
from app.services.usage_alerts import (
    ALERT_TIME,
    ALERT_TRAFFIC,
    SentAlert,
    alert_extras,
    period_seconds,
    plan_durations,
    replace_previous_alerts,
    time_alert_markup,
    time_remaining_pct,
    traffic_alert_markup,
    traffic_remaining_pct,
    usage_alert_key,
)

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()

# Bound concurrent PG subscription lookups during expiry scan
_PG_FETCH_CONCURRENCY = 8
# Where the next alert re-arm sweep continues (in memory: a restart simply starts from the beginning).
_rearm_after_id = 0


def _as_int(val, default: int) -> int:
    try:
        return int(float(val))
    except Exception:
        return default


def _resolve_send_bot(
    main_bot: Bot,
    reseller_user_id: int | None,
    profile_by_user: dict[int, int],
) -> Bot | None:
    """The bot that may alert this shop's customers: the platform bot, or the shop's own (None if it has none)."""
    if not reseller_user_id:
        return main_bot
    from app.services.reseller_bots import get_reseller_bot_manager

    mgr = get_reseller_bot_manager()
    pid = profile_by_user.get(int(reseller_user_id)) if mgr else None
    if mgr is None or pid is None:
        return None
    return mgr.bot_for_profile_id(int(pid))


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
                svc_q = svc_q.where(UserService.shop_id.in_(reseller_ids_with_alerts))
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

            eligible: list[tuple[UserService, BotUser, dict, int, int, int | None]] = []
            for svc in services:
                user = users_by_id.get(svc.bot_user_id)
                if not user or user.is_blocked:
                    continue

                if svc.shop_needs_review:
                    continue
                rid = int(svc.shop_id) if svc.shop_id else None
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

            extras_by_shop = {
                shop: await alert_extras(session, [item[0] for item in eligible if item[5] == shop], shop_id=shop)
                for shop in {item[5] for item in eligible}
            }
            plan_days = await plan_durations(session, [item[0] for item in eligible])
            sem = asyncio.Semaphore(_PG_FETCH_CONCURRENCY)
            sent_alerts: list[SentAlert] = []

            async def _fetch_info(token: str):
                async with sem:
                    try:
                        return await asyncio.wait_for(pg.subscription_info(token), timeout=12)
                    except Exception:
                        logger.debug("subscription_info failed for service fetch", exc_info=True)
                        return None

            infos = await asyncio.gather(
                *[_fetch_info(svc.subscription_token) for svc, *_rest in eligible]
            )

            for (svc, user, _ui, traffic_pct, time_pct, shop_rid), info in zip(eligible, infos):
                if not info:
                    continue
                from app.services.bot_user_admin import sync_service_quota_cache
                from app.services.service_live_info import validate_service_info

                cached_info = validate_service_info(info)
                if cached_info.get("status") and "expire" in cached_info:
                    sync_service_quota_cache(svc, info=cached_info)
                send_bot = _resolve_send_bot(bot, shop_rid, profile_by_user)
                if send_bot is None:
                    continue  # the shop's own bot is not running: never alert its customers from the platform bot
                extras = extras_by_shop[shop_rid]

                # --- remaining TIME percent ---
                if not svc.notified_expire:
                    expire = parse_expire(info.get("expire"))
                    created = svc.created_at
                    if created and created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    if expire:
                        period = period_seconds(
                            plan_days=plan_days.get(int(svc.plan_id)) if svc.plan_id else None,
                            created_at=created,
                            expire=expire,
                        )
                        rem_pct = time_remaining_pct(expire, now=now, period=period)
                        if rem_pct is not None:
                            if rem_pct <= time_pct:
                                try:
                                    one_tap = on(_ui.get("one_tap_renew_enabled", "1"))
                                    markup = time_alert_markup(_ui, svc.id, extras)
                                    alert = await send_bot.send_message(
                                        user.telegram_id,
                                        f"⏰ زمان سرویس <b>{svc.pg_username}</b> به کمتر از "
                                        f"<b>{time_pct}٪</b> رسیده است.\n"
                                        "از بخش سرویس‌ها تمدید کنید."
                                        + ("\nیا دکمه زیر را بزنید:" if one_tap else "")
                                        + extras.note_for(svc.id),
                                        parse_mode="HTML",
                                        reply_markup=markup,
                                    )
                                    svc.notified_expire = True
                                    sent_alerts.append((send_bot, usage_alert_key(ALERT_TIME, svc.id), alert))
                                    if one_tap:
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
                        except (TypeError, ValueError):
                            limit_f = 0
                        rem_pct = traffic_remaining_pct(used, limit_f)
                        if rem_pct is not None:
                            if rem_pct <= traffic_pct:
                                try:
                                    alert = await send_bot.send_message(
                                        user.telegram_id,
                                        f"📉 حجم باقی‌مانده سرویس <b>{svc.pg_username}</b> کمتر از "
                                        f"<b>{traffic_pct}٪</b> است "
                                        f"({format_bytes_ratio(used, limit_f, joiner=' از ')})."
                                        + extras.note_for(svc.id),
                                        parse_mode="HTML",
                                        reply_markup=traffic_alert_markup(_ui, svc.id, extras),
                                    )
                                    svc.notified_traffic = True
                                    sent_alerts.append((send_bot, usage_alert_key(ALERT_TRAFFIC, svc.id), alert))
                                except Exception:
                                    logger.debug(
                                        "traffic alert send failed tg=%s", user.telegram_id, exc_info=True
                                    )

            await session.commit()
            await replace_previous_alerts(session, sent_alerts)
            if len(services) < batch_size:
                break

        async def thresholds_for(shop_id: int | None) -> tuple[bool, int, int]:
            shop_ui = await ui_for(shop_id)
            return (
                on(shop_ui.get("user_alert_low_enabled", "0")),
                max(1, min(99, _as_int(shop_ui.get("user_alert_low_traffic_pct"), 20))),
                max(1, min(99, _as_int(shop_ui.get("user_alert_low_time_pct"), 20))),
            )

        try:
            global _rearm_after_id
            result = await rearm_flagged_alerts(session, pg, thresholds_for, now=now, after_id=_rearm_after_id)
            _rearm_after_id = result.next_after_id
        except Exception:  # noqa: BLE001 - the sweep is best effort; the next run tries again
            logger.warning("alert re-arm sweep failed", exc_info=True)
            await session.rollback()



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
            return
        await _run_payg_reconcile(session)


async def run_cashback_mature_job() -> None:
    """Every 5 minutes: credit cashback whose hold has passed, once each."""
    async with SessionLocal() as session:
        try:
            from app.services.cashback import mature_grants

            credited = await mature_grants(session)
            if credited:
                logger.info("cashback credited %s", credited)
        except Exception:
            logger.exception("cashback maturity failed")


async def run_credit_cycle_job() -> None:
    """Hourly: close last month's credit statements, settle paid debt, apply the overdue ladder."""
    async with SessionLocal() as session:
        try:
            from app.services.credit_cycle import run_credit_cycle

            stats = await run_credit_cycle(session)
            if stats.get("shops"):
                logger.info("credit cycle %s", stats)
        except Exception:
            logger.exception("credit cycle failed")


async def _run_payg_reconcile(session) -> None:
    """Charge PAYG shops for user changes made in the panel outside the bot."""
    try:
        from app.services.pasarguard import get_pg
        from app.services.payg_reconcile import run_payg_reconcile

        stats = await run_payg_reconcile(session, get_pg())
        if stats.shops or stats.swept:
            logger.info("payg reconcile %s", stats)
    except Exception:
        logger.exception("payg reconcile failed")


async def run_auto_order_purge() -> None:
    """Hourly: delete the order and payment rows each shop chose for automatic deletion."""
    async with SessionLocal() as session:
        try:
            from app.services.order_purge import auto_purge_all_shops

            total = await auto_purge_all_shops(session)
            if total:
                logger.info("automatic order purge deleted=%s", total)
        except Exception:
            logger.exception("automatic order purge failed")


async def run_scheduled_receipt_approvals() -> None:
    """Every minute: approve card receipts nobody reviewed within their shop's chosen time."""
    async with SessionLocal() as session:
        try:
            from app.bot import create_bot
            from app.services.reseller_bots import open_notify_bot_for_reseller
            from app.services.scheduled_approval import approve_due_receipts
            from app.services.users import get_all_settings

            async def settings_for(shop_id: int | None) -> dict[str, str]:
                return await get_all_settings(session, reseller_id=shop_id)

            async def open_bot(shop_id: int | None):
                if shop_id is None:
                    return create_bot(), True
                return await open_notify_bot_for_reseller(session, shop_id)

            approved = await approve_due_receipts(session, settings_for=settings_for, open_bot=open_bot)
            if approved:
                logger.info("scheduled receipt approval approved=%s", approved)
        except Exception:
            logger.exception("scheduled receipt approval failed")


async def run_crypto_rate_refresh() -> None:
    """Every minute: read the API rate of each shop that converts crypto amounts with one."""
    async with SessionLocal() as session:
        try:
            from app.services.crypto_rates import refresh_api_rates
            from app.services.users import get_all_settings

            shop_ids = list(
                (
                    await session.execute(
                        select(ResellerProfile.user_id).where(ResellerProfile.is_active.is_(True))
                    )
                ).scalars().all()
            )

            async def settings_for(shop_id: int | None) -> dict[str, str]:
                return await get_all_settings(session, reseller_id=shop_id)

            await refresh_api_rates(settings_for, [None, *[int(rid) for rid in shop_ids]])
        except Exception:
            logger.exception("crypto rate refresh failed")


async def run_scheduled_backup() -> None:
    """Nightly backup + archive validation when enabled."""
    from app.services.backup_schedule import run_nightly_backup

    async with SessionLocal() as session:
        try:
            await run_nightly_backup(session, datetime.now(timezone.utc))
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
                is_report_due,
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
            owner_hour_ok = is_report_due(now, await get_setting(session, "admin_daily_report_hour", "0"))

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
                    await send_replacing_report(
                        session, bot, list(get_settings().admin_ids or []), text,
                        replace_key=DAILY_REPORT_KEY, reply_markup=platform_report_markup(),
                        **{"parse_mode": "HTML", **report_kw},
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
                                await send_replacing_report(
                                    session, bot, list(get_settings().admin_ids or []), text,
                                    replace_key=SUBORDINATE_REPORT_KEY, parse_mode="HTML",
                                    reply_markup=subordinate_report_markup(shop=False),
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
                if not is_report_due(now, ui.get("admin_daily_report_hour")):
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
                            await send_replacing_report(
                                session, shop_bot, await shop_daily_report_chat_ids(session, rid), text,
                                replace_key=DAILY_REPORT_KEY, reply_markup=shop_report_markup(),
                                **{"parse_mode": "HTML", **report_kw},
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
                                    await send_replacing_report(
                                        session, shop_bot, await shop_daily_report_chat_ids(session, rid), text,
                                        replace_key=SUBORDINATE_REPORT_KEY, parse_mode="HTML",
                                        reply_markup=subordinate_report_markup(shop=True),
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


async def run_service_automation_tick(bot: Bot) -> None:
    from app.db.models import ServiceAutomation
    from app.services.reseller_bots import open_notify_bot_for_reseller
    from app.services.service_automation import process_service_automation

    last_id = 0
    unreachable_shops: set[int] = set()
    while True:
        async with SessionLocal() as session:
            rows = (await session.execute(
                select(ServiceAutomation.service_id, ServiceAutomation.shop_id)
                .where(
                    ServiceAutomation.service_id > last_id,
                    or_(
                        ServiceAutomation.renew_enabled.is_(True),
                        ServiceAutomation.duration_enabled.is_(True),
                        ServiceAutomation.volume_enabled.is_(True),
                        ServiceAutomation.pending_order_id.is_not(None),
                    ),
                ).order_by(ServiceAutomation.service_id).limit(100)
            )).all()
        if not rows:
            return
        for service_id, shop_id in rows:
            last_id = service_id
            async with SessionLocal() as session:
                send_bot, should_close = bot, False
                try:
                    if shop_id:
                        send_bot, should_close = await open_notify_bot_for_reseller(session, shop_id)
                        if send_bot is None:
                            if shop_id not in unreachable_shops:
                                unreachable_shops.add(shop_id)
                                logger.warning("service automation skipped: shop %s has no bot token", shop_id)
                            continue
                    await process_service_automation(session, service_id, send_bot)
                except Exception:
                    logger.exception("service automation tick failed service=%s", service_id)
                finally:
                    if should_close and send_bot is not None:
                        await send_bot.session.close()


async def run_targeted_campaigns_tick(bot: Bot) -> None:
    from app.db.models import TargetedCampaign
    from app.services.campaigns import process_campaign
    from app.services.catalog_discounts import pause_expired_announcements
    from app.services.reseller_bots import open_notify_bot_for_reseller

    async with SessionLocal() as session:
        await pause_expired_announcements(session)
    last_id = 0
    while True:
        async with SessionLocal() as session:
            rows = (await session.execute(select(TargetedCampaign.id, TargetedCampaign.reseller_id).where(
                TargetedCampaign.id > last_id, TargetedCampaign.status == "running",
            ).order_by(TargetedCampaign.id).limit(50))).all()
        if not rows:
            return
        for campaign_id, shop_id in rows:
            last_id = campaign_id
            async with SessionLocal() as session:
                send_bot, should_close = bot, False
                try:
                    if shop_id:
                        send_bot, should_close = await open_notify_bot_for_reseller(session, shop_id)
                        if send_bot is None:
                            continue
                    await process_campaign(session, campaign_id, send_bot)
                except Exception:
                    logger.exception("campaign batch failed campaign=%s", campaign_id)
                finally:
                    if should_close and send_bot is not None:
                        await send_bot.session.close()


async def run_broadcast_tick(bot: Bot) -> None:
    """Send owner broadcasts a step at a time; the platform bot only reaches platform customers."""
    from app.services.broadcast_queue import run_broadcast_worker

    await run_broadcast_worker(SessionLocal, bot)
async def run_first_use_nudge_tick(bot: Bot) -> None:
    """Message customers whose new service has not carried any traffic after a day."""
    from app.services.first_use_nudge import run_first_use_check

    async with SessionLocal() as session:
        profiles = (
            await session.execute(
                select(ResellerProfile.user_id, ResellerProfile.id).where(
                    ResellerProfile.is_active.is_(True), ResellerProfile.bot_token.is_not(None)
                )
            )
        ).all()
        profile_by_user = {int(user_id): int(profile_id) for user_id, profile_id in profiles if user_id is not None}
        try:
            await run_first_use_check(
                session, get_pg(), lambda shop_id: _resolve_send_bot(bot, shop_id, profile_by_user)
            )
        except Exception:
            logger.exception("first-use nudge tick failed")


async def run_bbc_settlement_poll() -> None:
    """Re-read open «بزن به کارت» orders: the callback's backup and the late-payment check."""
    from app.services.bbc_settlement import poll_bbc_settlements

    async with SessionLocal() as session:
        try:
            n = await poll_bbc_settlements(session)
            if n:
                logger.info("bbc settlement poll settled=%s", n)
        except Exception:
            logger.exception("bbc settlement poll failed")


async def run_psp_settlement_poll() -> None:
    """Re-verify open SunPay/Zarinpal settlements when the browser never returns,
    and finish settlements a crash left half-settled."""
    from app.services.payment_settlement import (
        poll_psp_settlements,
        recover_stuck_psp_settlements,
    )

    async with SessionLocal() as session:
        try:
            n = await poll_psp_settlements(session)
            if n:
                logger.info("psp settlement poll settled=%s", n)
        except Exception:
            logger.exception("psp settlement poll failed")
    async with SessionLocal() as session:
        try:
            n = await recover_stuck_psp_settlements(session)
            if n:
                logger.info("psp stuck settlements recovered=%s", n)
        except Exception:
            logger.exception("psp stuck settlement recovery failed")


BOT_PHOTO_FIRST_RUN_MINUTES = 2


def start_background_scheduler() -> None:
    """Run financial and maintenance jobs as soon as the database is ready."""
    jobs = (
        ("billing_tick", run_reseller_billing_tick, {"minutes": 1}, 120),
        ("credit_cycle", run_credit_cycle_job, {"hours": 1}, 3600),
        ("cashback_mature", run_cashback_mature_job, {"minutes": 5}, 300),
        ("pg_admin_subscription", run_pg_admin_subscription_tick, {"minutes": 5}, 300),
        ("order_purge", run_auto_order_purge, {"hours": 1}, 3600),
        ("scheduled_receipt_approval", run_scheduled_receipt_approvals, {"seconds": 60}, 90),
        ("crypto_rate_refresh", run_crypto_rate_refresh, {"seconds": 60}, 90),
        ("scheduled_backup", run_scheduled_backup, {"minutes": 30}, 3600),
        ("connection_status", run_connection_status, {"seconds": 60}, 60),
        ("pg_link_watchdog", run_pg_link_watchdog, {"seconds": 60}, 60),
        ("bbc_settlement_poll", run_bbc_settlement_poll, {"seconds": 45}, 90),
        ("psp_settlement_poll", run_psp_settlement_poll, {"seconds": 45}, 90),
        # Ticks often; each shop is recounted on the period set in volume_external_sync_minutes.
        ("volume_external_sync", run_volume_external_sync, {"minutes": 1}, 120),
        ("chat_message_purge", run_chat_message_purge, {"hours": 1}, 3600),
        ("service_cleanup", run_service_cleanup, {"minutes": 30}, 1800),
    )
    for job_id, func, interval, grace in jobs:
        if scheduler.get_job(job_id) is None:
            scheduler.add_job(
                func,
                "interval",
                id=job_id,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=grace,
                **interval,
            )
    if scheduler.get_job("bot_profile_photo") is None:
        # Every 6 hours, but the first copy soon after start so a new logo shows the same day.
        scheduler.add_job(
            run_bot_profile_photo_sync, "interval", hours=6, id="bot_profile_photo",
            next_run_time=datetime.now(timezone.utc) + timedelta(minutes=BOT_PHOTO_FIRST_RUN_MINUTES),
            max_instances=1, coalesce=True, misfire_grace_time=3600,
        )
    if scheduler.get_job("user_photo_prune") is None:
        scheduler.add_job(
            run_user_photo_prune, "interval", hours=24, id="user_photo_prune",
            max_instances=1, coalesce=True, misfire_grace_time=3600,
        )
    if not scheduler.running:
        scheduler.start()
        logger.info("Background scheduler started")


def start_scheduler(bot: Bot) -> None:
    """Attach notification jobs after the bot connects to Telegram."""
    start_background_scheduler()
    scheduler.add_job(
        run_cancellation_notifications, "interval", seconds=10, args=[bot], id="cancellation_notifications",
        max_instances=1, coalesce=True, misfire_grace_time=60, replace_existing=True,
    )
    scheduler.add_job(
        run_customer_notices, "interval", seconds=10, args=[bot], id="customer_notices",
        max_instances=1, coalesce=True, misfire_grace_time=60, replace_existing=True,
    )
    scheduler.add_job(
        run_panel_alert_notifications, "interval", minutes=5, args=[bot], id="panel_alert_notifications",
        max_instances=1, coalesce=True, misfire_grace_time=300, replace_existing=True,
    )
    scheduler.add_job(
        run_targeted_campaigns_tick, "interval", minutes=1, args=[bot], id="targeted_campaigns",
        max_instances=1, coalesce=True, misfire_grace_time=120, replace_existing=True,
    )
    scheduler.add_job(
        run_broadcast_tick, "interval", minutes=1, args=[bot], id="broadcast",
        max_instances=1, coalesce=True, misfire_grace_time=120, replace_existing=True,
    )
    scheduler.add_job(
        run_first_use_nudge_tick, "interval", minutes=30, args=[bot], id="first_use_nudge",
        max_instances=1, coalesce=True, misfire_grace_time=1800, replace_existing=True,
    )
    scheduler.add_job(
        run_service_automation_tick,
        "interval", minutes=1, args=[bot], id="service_automation",
        max_instances=1, coalesce=True, misfire_grace_time=120,
        replace_existing=True,
    )
    scheduler.add_job(
        check_expiring_services,
        "interval",
        hours=6,
        args=[bot],
        id="expiry",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
        replace_existing=True,
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
        replace_existing=True,
    )
    logger.info("Telegram scheduler jobs attached")


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
