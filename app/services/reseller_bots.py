"""Runtime manager for reseller-owned Telegram bots (multi-token polling)."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from sqlalchemy import select

from app.db.models import ResellerProfile
from app.services.secret_box import hash_bot_token, reveal_bot_token
from app.db.session import SessionLocal

log = logging.getLogger(__name__)

_manager: "ResellerBotManager | None" = None
_TOKEN_LOOKUP_CACHE: dict[str, tuple[float, dict[str, Any] | None]] = {}
_TOKEN_LOOKUP_TTL = 60.0


class ResellerBotManager:
    """Polls each reseller bot and feeds updates into the shared dispatcher."""

    def __init__(self, dispatcher: Dispatcher):
        self.dispatcher = dispatcher
        self._tasks: dict[int, asyncio.Task] = {}
        self._bots: dict[int, Bot] = {}
        self._lock = asyncio.Lock()

    async def start_all(self) -> None:
        async with SessionLocal() as session:
            result = await session.execute(
                select(ResellerProfile).where(
                    ResellerProfile.is_active.is_(True),
                    ResellerProfile.setup_completed_at.is_not(None),
                    ResellerProfile.bot_token.is_not(None),
                )
            )
            rows = list(result.scalars().all())
            items = [(r.id, reveal_bot_token(r.bot_token), r.bot_telegram_id) for r in rows if r.bot_token]
        for rid, token, tg_id in items:
            try:
                await self.start_reseller(rid, token, bot_telegram_id=tg_id)
            except Exception:
                log.exception("Failed to start reseller bot profile_id=%s", rid)

    async def start_reseller(
        self,
        reseller_profile_id: int,
        token: str,
        bot_telegram_id: int | None = None,
    ) -> bool:
        token = (token or "").strip()
        if not token:
            return False
        async with self._lock:
            await self._stop_locked(reseller_profile_id)
            bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
            registered = False
            try:
                me = await bot.get_me()
                tg_id = int(me.id)
                uname = me.username or ""
                from app.bot.chat_menu import sync_telegram_menu_button

                # Shop bots never host the platform Mini App (HMAC / token mismatch)
                await sync_telegram_menu_button(bot, allow_miniapp=False)

                async with SessionLocal() as session:
                    row = await session.get(ResellerProfile, reseller_profile_id)
                    if row:
                        row.bot_telegram_id = tg_id
                        row.bot_username = uname
                        await session.commit()
                        try:
                            setattr(
                                bot,
                                "_pgclock_reseller",
                                {
                                    "profile_id": int(reseller_profile_id),
                                    "user_id": int(row.user_id),
                                    "bot_telegram_id": tg_id,
                                    "bot_username": uname,
                                },
                            )
                        except Exception:
                            pass

                self._bots[reseller_profile_id] = bot
                registered = True
                task = asyncio.create_task(
                    self._poll_loop(reseller_profile_id, bot),
                    name=f"reseller-bot-{reseller_profile_id}",
                )
                self._tasks[reseller_profile_id] = task
                log.info("Started reseller bot profile=%s (@%s)", reseller_profile_id, uname)
                return True
            except Exception as e:
                log.warning("reseller bot %s start failed: %s", reseller_profile_id, e)
                return False
            finally:
                if not registered:
                    try:
                        await bot.session.close()
                    except Exception:
                        pass

    async def stop_reseller(self, reseller_profile_id: int) -> None:
        async with self._lock:
            await self._stop_locked(reseller_profile_id)

    async def stop_by_user_id(self, reseller_user_id: int) -> None:
        async with SessionLocal() as session:
            result = await session.execute(
                select(ResellerProfile.id).where(ResellerProfile.user_id == reseller_user_id)
            )
            pid = result.scalar_one_or_none()
        if pid is not None:
            await self.stop_reseller(int(pid))

    async def restart_reseller_profile(self, profile_id: int) -> bool:
        async with SessionLocal() as session:
            row = await session.get(ResellerProfile, profile_id)
            if not row or not row.bot_token or not row.is_active:
                return False
            token = reveal_bot_token(row.bot_token)
            tg_id = row.bot_telegram_id
        return await self.start_reseller(profile_id, token, bot_telegram_id=tg_id)

    async def _stop_locked(self, reseller_profile_id: int) -> None:
        task = self._tasks.pop(reseller_profile_id, None)
        bot = self._bots.pop(reseller_profile_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        if bot:
            try:
                await bot.session.close()
            except Exception:
                pass

    async def stop_all(self) -> None:
        async with self._lock:
            ids = list(self._tasks.keys())
            for rid in ids:
                await self._stop_locked(rid)

    async def _poll_loop(self, reseller_profile_id: int, bot: Bot) -> None:
        offset: int | None = None
        try:
            try:
                await bot.delete_webhook(drop_pending_updates=True)
            except Exception:
                pass
            while True:
                try:
                    updates = await bot.get_updates(
                        offset=offset,
                        timeout=25,
                        allowed_updates=None,
                    )
                    for update in updates:
                        offset = update.update_id + 1
                        try:
                            await self.dispatcher.feed_update(bot, update)
                        except Exception:
                            log.exception(
                                "reseller bot %s failed to handle update %s",
                                reseller_profile_id,
                                getattr(update, "update_id", "?"),
                            )
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning(
                        "reseller bot %s get_updates error: %s",
                        reseller_profile_id,
                        e,
                    )
                    await asyncio.sleep(3)
        finally:
            try:
                await bot.session.close()
            except Exception:
                pass

    def bot_for_token(self, token: str) -> Bot | None:
        token = (token or "").strip()
        for b in self._bots.values():
            if getattr(b, "token", None) == token:
                return b
        return None

    def bot_for_profile_id(self, reseller_profile_id: int) -> Bot | None:
        return self._bots.get(int(reseller_profile_id))

    def profile_id_for_bot(self, bot: Bot) -> int | None:
        for rid, b in self._bots.items():
            if b is bot or b.token == bot.token:
                return rid
        return None


def get_reseller_bot_manager() -> ResellerBotManager | None:
    return _manager


# Default for ``shop_id``: fall back to the shop the customer is attributed to.
ATTRIBUTED_SHOP = object()


class ShopBotUnavailable(ValueError):
    """The shop a message is about has no running or configured bot of its own."""


async def open_notify_bot_for_user(session, user, *, shop_id=ATTRIBUTED_SHOP) -> tuple[Bot, bool]:
    """Return (bot, should_close) for notifying a customer.

    ``shop_id`` is the shop the message is about (an order's or the acting staff's shop;
    None = platform). Without it the customer's attributed shop is used, which is wrong
    for a person who buys from several shops, so callers that know the shop pass it.
    A shop's message goes through that shop's bot only: when it has none, ``ShopBotUnavailable``
    is raised instead of sending shop data through the platform token. Platform messages
    use a short-lived main-bot instance that the caller must close.

    Always returns a bot with HTML parse_mode so format_message / <b> tags render.
    """
    from app.bot import create_bot

    reseller_id = getattr(user, "reseller_id", None) if shop_id is ATTRIBUTED_SHOP else shop_id
    if reseller_id:
        bot, should_close = await open_notify_bot_for_reseller(session, int(reseller_id))
        if bot is None:
            raise ShopBotUnavailable("ربات این فروشگاه برای ارسال پیام در دسترس نیست")
        return bot, should_close
    return create_bot(), True


async def open_notify_bot_for_reseller(session, reseller_user_id: int) -> tuple[Bot | None, bool]:
    """Return (shop_bot, should_close) for notifying shop staff.

    Prefers the running dedicated bot; falls back to an ephemeral bot from stored token.
    Returns (None, False) when the shop has no bot token.
    """
    from app.bot import create_bot
    from app.services.resellers import get_reseller_profile

    profile = await get_reseller_profile(session, int(reseller_user_id))
    if not profile or not profile.is_active:
        return None, False
    mgr = get_reseller_bot_manager()
    if mgr:
        shop = mgr.bot_for_profile_id(int(profile.id))
        if shop is not None:
            return shop, False
    token = (reveal_bot_token(profile.bot_token) or "").strip()
    if token:
        return create_bot(token), True
    # No dedicated bot yet — do not fall back to main bot (ACL would break on platform bot)
    return None, False


def init_reseller_bot_manager(dispatcher: Dispatcher) -> ResellerBotManager:
    global _manager
    _manager = ResellerBotManager(dispatcher)
    return _manager


async def lookup_reseller_by_bot_token(session, token: str) -> dict[str, Any] | None:
    """Resolve active reseller profile that owns this bot token (cached briefly)."""
    import time

    token = (token or "").strip()
    if not token:
        return None
    now = time.monotonic()
    cached = _TOKEN_LOOKUP_CACHE.get(token)
    if cached is not None:
        at, payload = cached
        if now - at < _TOKEN_LOOKUP_TTL:
            return dict(payload) if payload else None
    token_hash = hash_bot_token(token)
    row = None
    if token_hash:
        result = await session.execute(
            select(ResellerProfile).where(
                ResellerProfile.bot_token_hash == token_hash,
                ResellerProfile.is_active.is_(True),
            )
        )
        row = result.scalar_one_or_none()
    if row is None:
        # Legacy plaintext rows (pre-encryption) — match then lazy-upgrade hash/seal
        result = await session.execute(
            select(ResellerProfile).where(
                ResellerProfile.bot_token == token,
                ResellerProfile.is_active.is_(True),
            )
        )
        row = result.scalar_one_or_none()
        if row is not None and token_hash:
            try:
                from app.services.secret_box import seal_bot_token

                sealed = seal_bot_token(token)
                if sealed:
                    row.bot_token = sealed
                    row.bot_token_hash = token_hash
                    await session.commit()
            except Exception:
                pass
    if not row:
        _TOKEN_LOOKUP_CACHE[token] = (now, None)
        return None
    payload = {
        "profile_id": row.id,
        "user_id": row.user_id,
        "bot_username": row.bot_username,
        "bot_telegram_id": row.bot_telegram_id,
    }
    _TOKEN_LOOKUP_CACHE[token] = (now, payload)
    return dict(payload)


def clear_reseller_token_cache() -> None:
    _TOKEN_LOOKUP_CACHE.clear()



async def start_reseller_bot_for_profile(profile_id: int) -> bool:
    mgr = get_reseller_bot_manager()
    if not mgr:
        return False
    return await mgr.restart_reseller_profile(profile_id)
