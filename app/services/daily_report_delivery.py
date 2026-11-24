"""Send the nightly reports: the platform report, the representatives digest and each shop's reports.

Every recipient group is sent and marked on its own, so one shop whose report fails (a broken
template, an unreachable bot, a database error) is logged and skipped while the other shops still
get theirs. A report is marked sent for the Tehran day of the run (``report_day_key``), which is
also the day after the one it describes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import OrgPrincipal, ResellerProfile
from app.services.daily_report import (
    ACTOR_OWNER,
    ACTOR_SHOP,
    DEFAULT_REPORT_HOUR,
    DEFAULT_REPORT_TEMPLATE,
    build_daily_report_outbound,
    is_report_due,
    report_day_key,
    shop_daily_report_chat_ids,
)
from app.services.db_safe import rollback_quiet
from app.services.org_principals import DEPTH_ONE, STATUS_ACTIVE
from app.services.replaceable_notices import DAILY_REPORT_KEY, SUBORDINATE_REPORT_KEY, send_replacing_report
from app.services.reseller_bots import open_notify_bot_for_reseller
from app.services.rich_text import rich_plain_text
from app.services.staff_notice_buttons import platform_report_markup, shop_panel_markup, subordinate_report_markup
from app.services.subordinate_report import (
    SETTING_ENABLED as SUB_REPORT_ENABLED,
    SETTING_LAST as SUB_REPORT_LAST,
    build_subordinate_report_for_parent,
    list_direct_subordinate_shops,
    load_owner_parent,
    subordinate_traffic_map,
)
from app.services.users import get_all_settings, get_setting, on, set_setting

logger = logging.getLogger(__name__)

OWNER_ADMIN_NAME = "مالک سیستم"
DAILY_LAST_KEY = "admin_daily_report_last"
# Shops are read in pages so a large platform never loads every profile at once.
SHOP_PAGE_SIZE = 200


@dataclass(frozen=True, slots=True)
class ReportRun:
    now: datetime
    day_key: str


async def deliver_daily_reports(session: AsyncSession, bot: Bot, *, now: datetime) -> None:
    """One scheduler run: the platform's reports, then every active shop's, each isolated."""
    run = ReportRun(now=now, day_key=report_day_key(now))
    platform_hour = await get_setting(session, "admin_daily_report_hour", str(DEFAULT_REPORT_HOUR))
    if is_report_due(now, platform_hour):
        for step in (_send_owner_report, _send_owner_subordinate_report):
            try:
                await step(session, bot, run)
            except Exception:  # noqa: BLE001 - one report failing must not stop the others
                logger.exception("platform report step %s failed", step.__name__)
                await rollback_quiet(session)
    last_id = 0
    while True:
        # Plain ids: a rollback after a failed shop expires loaded rows, ids stay usable.
        profile_ids = list(
            await session.scalars(
                select(ResellerProfile.id)
                .where(
                    ResellerProfile.is_active.is_(True),
                    ResellerProfile.bot_token.is_not(None),
                    ResellerProfile.id > last_id,
                )
                .order_by(ResellerProfile.id)
                .limit(SHOP_PAGE_SIZE)
            )
        )
        if not profile_ids:
            return
        last_id = int(profile_ids[-1])
        for profile_id in profile_ids:
            try:
                profile = await session.get(ResellerProfile, int(profile_id))
                if profile is not None:
                    await _send_shop_reports(session, profile, run)
            except Exception:  # noqa: BLE001 - one shop's failure must not stop the other shops
                logger.exception("daily report failed for shop profile=%s", profile_id)
                await rollback_quiet(session)


async def _send_owner_report(session: AsyncSession, bot: Bot, run: ReportRun) -> None:
    if not on(await get_setting(session, "admin_daily_report_enabled", "1")):
        return
    if (await get_setting(session, DAILY_LAST_KEY, "") or "") == run.day_key:
        return
    text, report_kw = await build_daily_report_outbound(
        session,
        reseller_id=None,
        actor=ACTOR_OWNER,
        admin_name=OWNER_ADMIN_NAME,
        template=(await get_setting(session, "admin_daily_report_template", "")) or DEFAULT_REPORT_TEMPLATE,
        metrics_raw=await get_setting(session, "admin_daily_report_metrics", ""),
        now=run.now,
    )
    await send_replacing_report(
        session, bot, list(get_settings().admin_ids or []), text,
        replace_key=DAILY_REPORT_KEY, reply_markup=platform_report_markup(),
        **{"parse_mode": "HTML", **report_kw},
    )
    await set_setting(session, DAILY_LAST_KEY, run.day_key)
    await session.commit()


async def _send_owner_subordinate_report(session: AsyncSession, bot: Bot, run: ReportRun) -> None:
    if not on(await get_setting(session, SUB_REPORT_ENABLED, "1")):
        return
    if (await get_setting(session, SUB_REPORT_LAST, "") or "") == run.day_key:
        return
    owner = await load_owner_parent(session)
    if owner is None:
        return
    text = await _subordinate_text(session, owner, run)
    if text:
        await send_replacing_report(
            session, bot, list(get_settings().admin_ids or []), text,
            replace_key=SUBORDINATE_REPORT_KEY, parse_mode="HTML",
            reply_markup=subordinate_report_markup(shop=False),
        )
    await set_setting(session, SUB_REPORT_LAST, run.day_key)
    await session.commit()


async def _subordinate_text(session: AsyncSession, parent: OrgPrincipal, run: ReportRun) -> str | None:
    shops = await list_direct_subordinate_shops(session, parent=parent)
    if not shops:
        return None
    traffic = await subordinate_traffic_map(session, shops)
    return await build_subordinate_report_for_parent(session, parent=parent, traffic_by_user_id=traffic, now=run.now)


async def _send_shop_reports(session: AsyncSession, profile: ResellerProfile, run: ReportRun) -> None:
    rid = int(profile.user_id)
    ui = await get_all_settings(session, reseller_id=rid)
    if not is_report_due(run.now, ui.get("admin_daily_report_hour")):
        return
    wants_daily = on(ui.get("admin_daily_report_enabled")) and (ui.get(DAILY_LAST_KEY) or "").strip() != run.day_key
    wants_digest = on(ui.get(SUB_REPORT_ENABLED)) and (ui.get(SUB_REPORT_LAST) or "").strip() != run.day_key
    if not (wants_daily or wants_digest):
        return
    shop_bot, should_close = await open_notify_bot_for_reseller(session, rid)
    if shop_bot is None:
        # Nothing is marked: the report goes out on a later run once the shop's bot is back.
        logger.warning("daily report skipped: no bot for shop=%s", rid)
        return
    try:
        if wants_daily:
            await _send_shop_daily(session, shop_bot, profile, ui, run)
        if wants_digest:
            await _send_shop_digest(session, shop_bot, profile, run)
    finally:
        if should_close:
            await _close_bot(shop_bot, rid)


async def _send_shop_daily(session: AsyncSession, bot: Bot, profile: ResellerProfile, ui: dict, run: ReportRun) -> None:
    rid = int(profile.user_id)
    admin_name = (rich_plain_text(ui.get("shop_title")) or profile.bot_username or "فروشگاه").strip()
    text, report_kw = await build_daily_report_outbound(
        session,
        reseller_id=rid,
        actor=ACTOR_SHOP,
        admin_name=admin_name,
        template=ui.get("admin_daily_report_template") or DEFAULT_REPORT_TEMPLATE,
        metrics_raw=ui.get("admin_daily_report_metrics"),
        now=run.now,
    )
    await send_replacing_report(
        session, bot, await shop_daily_report_chat_ids(session, rid), text,
        replace_key=DAILY_REPORT_KEY, reply_markup=shop_panel_markup(),
        **{"parse_mode": "HTML", **report_kw},
    )
    await set_setting(session, DAILY_LAST_KEY, run.day_key, reseller_id=rid)
    await session.commit()


async def _send_shop_digest(session: AsyncSession, bot: Bot, profile: ResellerProfile, run: ReportRun) -> None:
    """A Level-1 shop's digest of its own direct sub-shops."""
    rid = int(profile.user_id)
    parent = await session.scalar(
        select(OrgPrincipal).where(
            OrgPrincipal.reseller_profile_id == int(profile.id),
            OrgPrincipal.depth == DEPTH_ONE,
            OrgPrincipal.status == STATUS_ACTIVE,
        )
    )
    text = await _subordinate_text(session, parent, run) if parent is not None else None
    if text:
        await send_replacing_report(
            session, bot, await shop_daily_report_chat_ids(session, rid), text,
            replace_key=SUBORDINATE_REPORT_KEY, parse_mode="HTML",
            reply_markup=subordinate_report_markup(shop=True),
        )
    await set_setting(session, SUB_REPORT_LAST, run.day_key, reseller_id=rid)
    await session.commit()


async def _close_bot(bot: Bot, shop_id: int) -> None:
    try:
        await bot.session.close()
    except (TelegramAPIError, OSError, RuntimeError):
        logger.debug("daily report shop bot session close failed shop=%s", shop_id, exc_info=True)
