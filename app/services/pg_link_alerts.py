"""Owner Telegram alerts when the single PasarGuard link goes down or recovers.

Detects healthy→down (circuit pause or watchdog failure) and down→healthy.
A short blip must not page the owner: the link has to stay down (and later
stay up) for a confirmation window before either message is claimed. Throttle
and outage flags live in settings so a restart does not re-spam. Sends only
after the claim is committed; notify failures never raise.
"""

from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Setting
from app.services import pg_link_health
from app.services.formatting import format_message, info_block, kv_line
from app.services.pg_link_watchdog import WATCHDOG_FAILURES_FOR_ALERT, consecutive_failures as watchdog_failure_streak
from app.services.pg_read_resilience import circuit_status
from app.services.staff_notice_buttons import link_alert_markup
from app.services.users import clear_settings_cache, get_setting, set_setting

logger = logging.getLogger(__name__)

# Keep strong refs so pause-alert tasks are not GC'd before they run.
_pause_alert_tasks: set[asyncio.Task[None]] = set()

SETTING_KEY = "pg_link_alerts_enabled"
SETTING_DEFAULT = "1"
# Keep-alive drops and a single slow /api/system answer are common; wait for a
# sustained outage (and a sustained recovery) before Telegram messages.
# With a 60s watchdog and a streak of two failures, 60s here keeps a real
# outage around two to three minutes before the owner is paged — not ~four.
DOWN_CONFIRM_SEC = 60
UP_CONFIRM_SEC = 90
THROTTLE_SEC = 30 * 60

OUTAGE_ACTIVE_KEY = "pg_link_alert_outage_active"
DOWN_NOTIFIED_KEY = "pg_link_alert_down_notified"
LAST_DOWN_SENT_KEY = "pg_link_alert_last_down_sent"
LAST_UP_SENT_KEY = "pg_link_alert_last_up_sent"
DOWN_SINCE_KEY = "pg_link_alert_down_since"
UP_SINCE_KEY = "pg_link_alert_up_since"

ALERT_DOWN = "down"
ALERT_UP = "up"


@dataclass(slots=True)
class LinkAlertDecision:
    sent_type: str | None
    text: str | None


def _truthy(raw: str | None) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _parse_iso(raw: str | None) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _tehran_stamp(moment: datetime) -> str:
    return moment.astimezone(pg_link_health.DISPLAY_ZONE).strftime("%Y-%m-%d %H:%M:%S")


def compose_down_text(*, failure_kind: str | None, when: datetime | None = None) -> str:
    """Safe HTML: failure kind label + Tehran time only."""
    kind = failure_kind if failure_kind in pg_link_health.KIND_LABELS else "dropped"
    label = pg_link_health.KIND_LABELS[kind]
    stamp = _tehran_stamp(when or datetime.now(timezone.utc))
    return format_message(
        "⚠️ قطع ارتباط با پاسارگارد",
        info_block(
            [
                kv_line("📛", "نوع خطا", html.escape(label)),
                kv_line("🕒", "زمان (تهران)", f"<code>{html.escape(stamp)}</code>"),
            ]
        ),
    )


def compose_up_text(*, when: datetime | None = None) -> str:
    stamp = _tehran_stamp(when or datetime.now(timezone.utc))
    return format_message(
        "✅ ارتباط با پاسارگارد برقرار شد",
        info_block(
            [
                kv_line("🕒", "زمان (تهران)", f"<code>{html.escape(stamp)}</code>"),
            ]
        ),
    )


def link_is_down(*, watchdog_failed: bool = False, watchdog_streak: int = 0) -> bool:
    """Down when the read circuit is paused or the watchdog has failed repeatedly.

    A single failed probe is not enough: PasarGuard often drops idle keep-alive
    sockets, and one slow ``/api/system`` must not open the outage clock.
    """
    try:
        paused = bool(circuit_status().get("paused"))
    except Exception:
        logger.debug("pg_link_alerts: circuit_status failed", exc_info=True)
        paused = False
    if paused:
        return True
    if watchdog_streak > 0:
        return int(watchdog_streak) >= WATCHDOG_FAILURES_FOR_ALERT
    return bool(watchdog_failed)


async def alerts_enabled(session: AsyncSession) -> bool:
    return _truthy(await get_setting(session, SETTING_KEY, SETTING_DEFAULT))


async def _read_since(session: AsyncSession, key: str) -> datetime | None:
    return _parse_iso(await get_setting(session, key, ""))


async def _write_since(session: AsyncSession, key: str, moment: datetime) -> None:
    await set_setting(session, key, _iso(moment))


async def _clear_since(session: AsyncSession, key: str) -> None:
    current = (await get_setting(session, key, "")).strip()
    if current:
        await set_setting(session, key, "")


async def _ensure_setting_row(session: AsyncSession, key: str, default: str = "0") -> None:
    """Insert the settings row if missing so atomic UPDATEs have a target."""
    exists = await session.scalar(select(Setting.id).where(Setting.key == key))
    if exists is not None:
        return
    session.add(Setting(key=key, value=default))
    try:
        await session.commit()
    except IntegrityError:
        # Concurrent insert won the unique key.
        await session.rollback()
    clear_settings_cache(0)


async def _begin_claim_transaction(session: AsyncSession, keys: tuple[str, ...]) -> None:
    """Serialize claim writers with row locks on the claim settings."""
    if session.in_transaction():
        await session.rollback()
    await session.execute(
        select(Setting.id).where(Setting.key.in_(keys)).with_for_update()
    )


async def _setting_value(session: AsyncSession, key: str) -> str:
    return str(await session.scalar(select(Setting.value).where(Setting.key == key)) or "")


async def _claim_down(session: AsyncSession, *, now: datetime) -> bool:
    """Atomically claim the down notification. True when a down message may be sent.

    One exclusive transaction owns the outage/notified transition so the circuit
    pause-alert task and the watchdog cannot both send «قطع».
    """
    keys = (OUTAGE_ACTIVE_KEY, DOWN_NOTIFIED_KEY, LAST_DOWN_SENT_KEY)
    for key in keys:
        await _ensure_setting_row(session, key, "0" if key != LAST_DOWN_SENT_KEY else "")
    await _begin_claim_transaction(session, keys)
    try:
        was_outage = _truthy(await _setting_value(session, OUTAGE_ACTIVE_KEY))
        await session.execute(
            update(Setting).where(Setting.key == OUTAGE_ACTIVE_KEY).values(value="1")
        )
        if not was_outage:
            await session.execute(
                update(Setting).where(Setting.key == DOWN_NOTIFIED_KEY).values(value="0")
            )

        last = _parse_iso(await _setting_value(session, LAST_DOWN_SENT_KEY))
        if last is not None and (now - last).total_seconds() < THROTTLE_SEC:
            await session.commit()
            clear_settings_cache(0)
            return False

        claimed = await session.execute(
            update(Setting)
            .where(Setting.key == DOWN_NOTIFIED_KEY, Setting.value != "1")
            .values(value="1")
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            await session.commit()
            clear_settings_cache(0)
            return False
        await session.execute(
            update(Setting).where(Setting.key == LAST_DOWN_SENT_KEY).values(value=_iso(now))
        )
        await session.commit()
        clear_settings_cache(0)
        return True
    except Exception:
        await session.rollback()
        raise


async def _claim_up(session: AsyncSession, *, now: datetime) -> bool:
    """Atomically clear outage; True when a recovery message must be sent once."""
    keys = (OUTAGE_ACTIVE_KEY, DOWN_NOTIFIED_KEY, LAST_UP_SENT_KEY)
    for key in keys:
        await _ensure_setting_row(session, key, "0" if key != LAST_UP_SENT_KEY else "")
    await _begin_claim_transaction(session, keys)
    try:
        notified = _truthy(await _setting_value(session, DOWN_NOTIFIED_KEY))
        cleared = await session.execute(
            update(Setting)
            .where(Setting.key == OUTAGE_ACTIVE_KEY, Setting.value == "1")
            .values(value="0")
            .execution_options(synchronize_session=False)
        )
        if cleared.rowcount != 1:
            await session.commit()
            clear_settings_cache(0)
            return False
        await session.execute(
            update(Setting).where(Setting.key == DOWN_NOTIFIED_KEY).values(value="0")
        )
        if not notified:
            await session.commit()
            clear_settings_cache(0)
            return False
        await session.execute(
            update(Setting).where(Setting.key == LAST_UP_SENT_KEY).values(value=_iso(now))
        )
        await session.commit()
        clear_settings_cache(0)
        return True
    except Exception:
        await session.rollback()
        raise


async def decide_link_alert(
    session: AsyncSession,
    *,
    is_down: bool,
    failure_kind: str | None = None,
    now: datetime | None = None,
) -> LinkAlertDecision:
    """Claim a sustained transition. Caller sends only when text is set.

    The first unhealthy observation only starts ``DOWN_SINCE``; a healthy tick
    during that window clears it with no Telegram message. Recovery after a
    notified outage is confirmed the same way with ``UP_SINCE``.
    """
    moment = now or datetime.now(timezone.utc)
    if not await alerts_enabled(session):
        return LinkAlertDecision(None, None)
    if is_down:
        await _clear_since(session, UP_SINCE_KEY)
        since = await _read_since(session, DOWN_SINCE_KEY)
        if since is None:
            await _write_since(session, DOWN_SINCE_KEY, moment)
            return LinkAlertDecision(None, None)
        if (moment - since).total_seconds() < DOWN_CONFIRM_SEC:
            return LinkAlertDecision(None, None)
        if not await _claim_down(session, now=moment):
            return LinkAlertDecision(None, None)
        kind = failure_kind or pg_link_health.latest_failure_kind()
        return LinkAlertDecision(ALERT_DOWN, compose_down_text(failure_kind=kind, when=moment))

    await _clear_since(session, DOWN_SINCE_KEY)
    if not _truthy(await get_setting(session, OUTAGE_ACTIVE_KEY, "0")):
        await _clear_since(session, UP_SINCE_KEY)
        return LinkAlertDecision(None, None)
    since = await _read_since(session, UP_SINCE_KEY)
    if since is None:
        await _write_since(session, UP_SINCE_KEY, moment)
        return LinkAlertDecision(None, None)
    if (moment - since).total_seconds() < UP_CONFIRM_SEC:
        return LinkAlertDecision(None, None)
    if not await _claim_up(session, now=moment):
        return LinkAlertDecision(None, None)
    await _clear_since(session, UP_SINCE_KEY)
    return LinkAlertDecision(ALERT_UP, compose_up_text(when=moment))


async def _send_to_owners(text: str, *, is_down: bool) -> int:
    """Best-effort HTML message to ADMIN_IDS; honours TelegramRetryAfter."""
    from app.bot import create_bot

    chat_ids = list(dict.fromkeys(int(i) for i in (get_settings().admin_ids or ()) if int(i) > 0))
    if not chat_ids:
        return 0
    bot = create_bot()
    delivered = 0
    try:
        for chat_id in chat_ids:
            for attempt in range(2):
                try:
                    await asyncio.wait_for(
                        bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=link_alert_markup(is_down=is_down)),
                        timeout=20,
                    )
                    delivered += 1
                    break
                except TelegramRetryAfter as exc:
                    wait = float(getattr(exc, "retry_after", 0) or 0)
                    if attempt == 0 and 0 < wait <= 60:
                        await asyncio.sleep(wait)
                        continue
                    logger.warning(
                        "PasarGuard link alert rate limited chat=%s",
                        chat_id,
                        exc_info=True,
                    )
                    break
                except (TelegramAPIError, OSError, TimeoutError, asyncio.TimeoutError):
                    logger.warning(
                        "PasarGuard link alert delivery failed chat=%s",
                        chat_id,
                        exc_info=True,
                    )
                    break
            await asyncio.sleep(0.05)
    finally:
        try:
            await bot.session.close()
        except Exception:
            logger.debug("pg_link_alerts: bot close failed", exc_info=True)
    return delivered


async def notify_link_health_change(
    session: AsyncSession,
    *,
    is_down: bool,
    failure_kind: str | None = None,
    now: datetime | None = None,
) -> str | None:
    """Evaluate, commit claim, then send. Never raises. Returns sent type or None."""
    try:
        decision = await decide_link_alert(
            session, is_down=is_down, failure_kind=failure_kind, now=now,
        )
        if decision.text is None:
            return None
        # Claim already committed inside set_setting; send outside any open unit of work.
        await _send_to_owners(decision.text, is_down=decision.sent_type == ALERT_DOWN)
        return decision.sent_type
    except Exception:
        logger.warning("PasarGuard link alert notify failed", exc_info=True)
        return None


async def evaluate_after_watchdog(
    session: AsyncSession,
    *,
    watchdog_ok: bool | None,
    failure_kind: str | None = None,
    consecutive_failures: int | None = None,
) -> str | None:
    """Hook for the watchdog job: skip when the tick did not observe the link."""
    streak = (
        int(consecutive_failures)
        if consecutive_failures is not None
        else watchdog_failure_streak()
    )
    if watchdog_ok is None:
        # Tick skipped (disabled / backoff / lock); still surface an active pause.
        if not link_is_down(watchdog_failed=False, watchdog_streak=streak):
            return None
        return await notify_link_health_change(session, is_down=True, failure_kind=failure_kind)
    is_down = link_is_down(watchdog_failed=not watchdog_ok, watchdog_streak=streak)
    return await notify_link_health_change(
        session,
        is_down=is_down,
        failure_kind=failure_kind if is_down else None,
    )


def schedule_circuit_pause_alert(failure_kind: str | None = None) -> None:
    """Fire-and-forget after the read circuit pauses; never blocks the pause path."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def _run() -> None:
        from app.db.session import SessionLocal

        try:
            async with SessionLocal() as session:
                await notify_link_health_change(
                    session,
                    is_down=True,
                    failure_kind=failure_kind or pg_link_health.latest_failure_kind(),
                )
        except Exception:
            logger.warning("PasarGuard link alert after circuit pause failed", exc_info=True)

    task = loop.create_task(_run(), name="pg-link-pause-alert")
    _pause_alert_tasks.add(task)
    task.add_done_callback(_pause_alert_tasks.discard)
