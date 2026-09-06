from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware, Bot
from aiogram.types import CallbackQuery, Message, TelegramObject, Update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.session import SessionLocal
from app.services.reseller_bots import lookup_reseller_by_bot_token
from app.services.users import (
    get_or_create_user,
    reset_shop_reseller_id,
    set_shop_reseller_id,
)

logger = logging.getLogger("pgclock.bot")


def _extract_from_user(event: TelegramObject):
    if isinstance(event, Message) and event.from_user:
        return event.from_user
    if isinstance(event, CallbackQuery) and event.from_user:
        return event.from_user
    try:
        from aiogram.types import PreCheckoutQuery

        if isinstance(event, PreCheckoutQuery) and event.from_user:
            return event.from_user
    except Exception:
        pass
    if isinstance(event, Update):
        if event.message and event.message.from_user:
            return event.message.from_user
        if event.callback_query and event.callback_query.from_user:
            return event.callback_query.from_user
        if event.edited_message and event.edited_message.from_user:
            return event.edited_message.from_user
        if event.pre_checkout_query and event.pre_checkout_query.from_user:
            return event.pre_checkout_query.from_user
    return None


def _reply_message(event: TelegramObject) -> Message | None:
    if isinstance(event, Message):
        return event
    if isinstance(event, CallbackQuery) and event.message and isinstance(event.message, Message):
        return event.message
    if isinstance(event, Update):
        if event.message:
            return event.message
        if event.edited_message:
            return event.edited_message
        cq = event.callback_query
        if cq and cq.message and isinstance(cq.message, Message):
            return cq.message
    return None


def _is_benign_telegram_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return (
        "message is not modified" in text
        or "query is too old" in text
        or "query id is invalid" in text
        or "message to delete not found" in text
        or "message can't be deleted" in text
    )


def _extract_start_payload(event: TelegramObject) -> str | None:
    text = None
    if isinstance(event, Message) and event.text:
        text = event.text
    elif isinstance(event, Update) and event.message and event.message.text:
        text = event.message.text
    if not text:
        return None
    parts = text.strip().split(maxsplit=1)
    if len(parts) == 2 and parts[0].startswith("/start"):
        return parts[1].strip()
    return None


def _force_join_chat_id(channel: str):
    """Chat id for get_chat_member (@username str or numeric int).

    Invite links return None here — use :func:`resolve_force_join_chat_id` instead.
    """
    from app.services.users import normalize_force_join_channel_id

    ch = normalize_force_join_channel_id(channel)
    if not ch:
        return None
    if ch.startswith("@"):
        return ch
    if ch.lstrip("-").isdigit():
        try:
            return int(ch)
        except ValueError:
            return ch
    return ch


def clear_force_join_member_cache(telegram_id: int | None = None) -> None:
    """Drop cached membership results (all, or one user).

    Also clears bot-admin / chat-resolve caches so a freshly promoted bot or
    corrected channel id is picked up on the next «عضو شدم» /start.
    """
    _FORCE_JOIN_BOT_ADMIN_CACHE.clear()
    _FORCE_JOIN_RESOLVE_CACHE.clear()
    if telegram_id is None:
        _FORCE_JOIN_MEMBER_CACHE.clear()
        return
    tid = int(telegram_id)
    for key in [k for k in _FORCE_JOIN_MEMBER_CACHE if k[0] == tid]:
        _FORCE_JOIN_MEMBER_CACHE.pop(key, None)


_JOINED_STATUSES = frozenset({"member", "administrator", "creator"})
_LEFT_STATUSES = frozenset({"left", "kicked"})

_FORCE_JOIN_MEMBER_CACHE: dict[tuple[int, str], tuple[float, bool | None]] = {}
_FORCE_JOIN_MEMBER_TTL = 45.0
_FORCE_JOIN_RESOLVE_CACHE: dict[str, tuple[float, int | str]] = {}
_FORCE_JOIN_RESOLVE_TTL = 300.0
_FORCE_JOIN_BOT_ADMIN_CACHE: dict[str, tuple[float, bool]] = {}
_FORCE_JOIN_BOT_ADMIN_TTL = 120.0


def _status_value(member: Any) -> str:
    status = getattr(member, "status", None)
    if status is None:
        return ""
    val = getattr(status, "value", None)
    if val is not None:
        return str(val).lower()
    text = str(status).lower()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text


def _member_is_joined(member: Any) -> bool | None:
    """Map Telegram ChatMember → True / False / None (unknown)."""
    from aiogram.types import (
        ChatMemberAdministrator,
        ChatMemberBanned,
        ChatMemberLeft,
        ChatMemberMember,
        ChatMemberOwner,
        ChatMemberRestricted,
    )

    if isinstance(member, (ChatMemberMember, ChatMemberAdministrator, ChatMemberOwner)):
        return True
    if isinstance(member, ChatMemberRestricted):
        return bool(getattr(member, "is_member", False))
    if isinstance(member, (ChatMemberLeft, ChatMemberBanned)):
        return False
    status_val = _status_value(member)
    if status_val in _LEFT_STATUSES:
        return False
    if status_val == "restricted":
        return bool(getattr(member, "is_member", False))
    if status_val in _JOINED_STATUSES:
        return True
    logger.warning(
        "force-join unknown chat_member %r status=%r",
        type(member).__name__,
        status_val,
    )
    return None


def _bot_can_inspect_members(member: Any) -> bool:
    """Channels require the bot to be admin/owner to call getChatMember on others."""
    from aiogram.types import ChatMemberAdministrator, ChatMemberOwner

    if isinstance(member, (ChatMemberAdministrator, ChatMemberOwner)):
        return True
    return _status_value(member) in {"administrator", "creator"}


async def resolve_force_join_chat_id(
    bot: Bot,
    channel: str,
    *,
    invite_link: str | None = None,
) -> int | str | None:
    """Resolve @username / -100… / invite link to a chat id Telegram accepts."""
    import time

    from app.services.users import (
        is_force_join_invite_ref,
        normalize_force_join_invite_link,
    )

    direct = _force_join_chat_id(channel)
    invite = normalize_force_join_invite_link(invite_link) or (
        normalize_force_join_invite_link(channel) if is_force_join_invite_ref(channel) else ""
    )
    cache_key = f"{direct!s}|{invite}"
    now = time.monotonic()
    hit = _FORCE_JOIN_RESOLVE_CACHE.get(cache_key)
    if hit and (now - hit[0]) < _FORCE_JOIN_RESOLVE_TTL:
        return hit[1]

    candidates: list[Any] = []
    if direct is not None:
        candidates.append(direct)
    if invite:
        candidates.append(invite)

    resolved: int | str | None = None
    last_exc: BaseException | None = None
    for cand in candidates:
        try:
            chat = await bot.get_chat(cand)
            cid = getattr(chat, "id", None)
            # Require a real int (AsyncMock children int()→1 and must not be trusted)
            if type(cid) is int:
                resolved = cid
                break
            if isinstance(cid, str) and cid.lstrip("-").isdigit():
                resolved = int(cid)
                break
            resolved = cand
            break
        except Exception as exc:
            last_exc = exc
            continue

    if resolved is None:
        # Fall back to direct id even if get_chat failed (public @username often works)
        if direct is not None:
            resolved = direct
        else:
            logger.warning(
                "force-join chat resolve failed for %r invite=%r: %s",
                channel,
                invite,
                last_exc,
            )
            return None

    _FORCE_JOIN_RESOLVE_CACHE[cache_key] = (now, resolved)
    if len(_FORCE_JOIN_RESOLVE_CACHE) > 2000:
        oldest = sorted(_FORCE_JOIN_RESOLVE_CACHE.items(), key=lambda kv: kv[1][0])[:500]
        for k, _ in oldest:
            _FORCE_JOIN_RESOLVE_CACHE.pop(k, None)
    return resolved


async def _ensure_bot_can_check(bot: Bot, chat_id: int | str) -> bool:
    """Return True when this bot may call getChatMember for other users in chat_id."""
    import time

    token = getattr(bot, "token", "") or ""
    cache_key = f"{token[-12:]}:{chat_id}"
    now = time.monotonic()
    hit = _FORCE_JOIN_BOT_ADMIN_CACHE.get(cache_key)
    if hit and (now - hit[0]) < _FORCE_JOIN_BOT_ADMIN_TTL:
        return hit[1]

    try:
        me = await bot.get_me()
        bot_id = getattr(me, "id", None)
        if type(bot_id) is not int:
            # Cannot resolve bot id (tests / transient) — still attempt user check
            return True
    except Exception:
        return True

    ok = False
    try:
        bot_member = await bot.get_chat_member(chat_id, bot_id)
        ok = _bot_can_inspect_members(bot_member)
        if not ok:
            logger.warning(
                "force-join: bot is not admin/owner in chat %s (status=%s) — "
                "cannot verify user membership",
                chat_id,
                _status_value(bot_member),
            )
    except Exception as exc:
        logger.warning("force-join: bot admin probe failed for %s: %s", chat_id, exc)
        ok = False
    _FORCE_JOIN_BOT_ADMIN_CACHE[cache_key] = (now, ok)
    return ok


async def check_force_join_member(
    bot: Bot,
    telegram_id: int,
    channel: str,
    *,
    invite_link: str | None = None,
) -> bool | None:
    """Return True if member, False if left/kicked, None if membership cannot be verified.

    Joined statuses: member, administrator, creator, and restricted with is_member=True.
    Resolves invite links / usernames via getChat first. Requires the bot to be an
    admin of the channel (Telegram API rule for channels). Only positive membership
    is cached briefly so a user who just joined can pass on the next check.
    """
    import time

    chat_id = await resolve_force_join_chat_id(bot, channel, invite_link=invite_link)
    if chat_id is None:
        logger.warning(
            "force-join channel id unusable: %r "
            "(need @username, -100…, t.me/c/…, or invite link while bot is a member)",
            channel,
        )
        return None

    cache_key = (int(telegram_id), str(chat_id))
    now = time.monotonic()
    hit = _FORCE_JOIN_MEMBER_CACHE.get(cache_key)
    if hit and hit[1] is True and (now - hit[0]) < _FORCE_JOIN_MEMBER_TTL:
        return True

    if not await _ensure_bot_can_check(bot, chat_id):
        return None

    try:
        member = await bot.get_chat_member(chat_id, int(telegram_id))
        result = _member_is_joined(member)
    except Exception as exc:
        err = str(exc).lower()
        # Explicit non-membership for this user (chat itself is reachable)
        if any(
            token in err
            for token in (
                "user not found",
                "member not found",
                "participant_id_invalid",
            )
        ):
            result = False
        else:
            logger.warning("force-join membership check failed for %s: %s", channel, exc)
            result = None

    if result is True:
        _FORCE_JOIN_MEMBER_CACHE[cache_key] = (now, True)
        if len(_FORCE_JOIN_MEMBER_CACHE) > 4000:
            oldest = sorted(_FORCE_JOIN_MEMBER_CACHE.items(), key=lambda kv: kv[1][0])[:1000]
            for k, _ in oldest:
                _FORCE_JOIN_MEMBER_CACHE.pop(k, None)
    return result

# Simple per-user flood guard (process-local)
_RATE_BUCKETS: dict[int, list[float]] = {}
_RATE_WINDOW_SEC = 10.0
_RATE_MAX_EVENTS = 25


def _rate_limited(telegram_id: int) -> bool:
    """Return True when the user exceeds the soft flood limit."""
    import time

    now = time.monotonic()
    bucket = _RATE_BUCKETS.get(telegram_id)
    if bucket is None:
        _RATE_BUCKETS[telegram_id] = [now]
        return False
    # Drop timestamps outside the window
    cutoff = now - _RATE_WINDOW_SEC
    bucket[:] = [t for t in bucket if t >= cutoff]
    if len(bucket) >= _RATE_MAX_EVENTS:
        return True
    bucket.append(now)
    if len(_RATE_BUCKETS) > 8000:
        # Opportunistic prune of idle keys
        stale = [k for k, v in _RATE_BUCKETS.items() if not v or v[-1] < cutoff]
        for k in stale[:2000]:
            _RATE_BUCKETS.pop(k, None)
    return False


class RateLimitMiddleware(BaseMiddleware):
    """Soft flood protection for message/callback spam."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        tg_user = _extract_from_user(event)
        if tg_user and _rate_limited(int(tg_user.id)):
            if isinstance(event, CallbackQuery):
                try:
                    await event.answer("لطفاً کمی صبر کنید", show_alert=False)
                except Exception:
                    pass
            else:
                msg = _reply_message(event)
                if msg:
                    try:
                        await msg.answer("تعداد درخواست‌ها زیاد است — چند ثانیه صبر کنید.")
                    except Exception:
                        pass
            return None
        return await handler(event, data)


async def check_force_join_all(
    bot: Bot,
    telegram_id: int,
    channels: list[str],
    *,
    entries: list[dict[str, Any]] | None = None,
) -> tuple[list[str], list[str]]:
    """Check membership for every required channel.

    Returns (missing, unverified). missing = left/kicked; unverified = API errors.
    Callers must block when either list is non-empty — membership must be confirmed.
    """
    if not channels:
        return [], []
    link_by_id: dict[str, str] = {}
    for e in entries or []:
        cid = str((e or {}).get("id") or "").lower()
        link = str((e or {}).get("link") or "")
        if cid and link:
            link_by_id[cid] = link
    results = await asyncio.gather(
        *[
            check_force_join_member(
                bot, telegram_id, ch, invite_link=link_by_id.get(ch.lower())
            )
            for ch in channels
        ]
    )
    missing: list[str] = []
    unverified: list[str] = []
    for ch, joined in zip(channels, results):
        if joined is False:
            missing.append(ch)
        elif joined is None:
            unverified.append(ch)
    return missing, unverified


def force_join_block_message(
    missing: list[str],
    unverified: list[str] | None = None,
    *,
    custom: str | None = None,
) -> str:
    """User-facing Persian copy when required membership is not confirmed.

    Channels belong on inline buttons — default copy does **not** dump a bullet
    list. If *custom* (``force_join_msg``) contains ``{channels}``, that placeholder
    is replaced with the bullet list; otherwise the custom text is shown as-is.
    """
    blocked = list(missing or [])
    for ch in unverified or []:
        if ch not in blocked:
            blocked.append(ch)
    listed = "\n".join(f"• {c}" for c in blocked)
    custom_text = (custom or "").strip()
    if custom_text:
        from app.services.message_variables import DOMAIN_FORCE_JOIN, render_message_template

        return render_message_template(
            custom_text,
            domain=DOMAIN_FORCE_JOIN,
            channels=listed or "—",
            html=True,
        )

    only_unverified = bool(unverified) and not missing
    if only_unverified:
        return (
            "الان نمی‌توان عضویت شما را تأیید کرد.\n"
            "ربات باید ادمین کانال باشد و شناسه/لینک کانال درست ذخیره شده باشد.\n"
            "بعد از رفع، از دکمه «عضو شدم» استفاده کنید."
        )
    return (
        "برای ورود به ربات ابتدا از دکمه‌های زیر عضو کانال شوید، "
        "سپس «عضو شدم» را بزنید."
    )


def _callback_data(event: TelegramObject) -> str | None:
    if isinstance(event, CallbackQuery) and event.data:
        return event.data
    if isinstance(event, Update) and event.callback_query and event.callback_query.data:
        return event.callback_query.data
    return None


class DbSessionMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        async with SessionLocal() as session:
            data["session"] = session
            return await handler(event, data)


class UserMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        session: AsyncSession = data["session"]
        bot: Bot | None = data.get("bot")
        reseller_owner_id: int | None = None
        reseller_profile_id: int | None = None
        is_reseller_bot = False

        token = getattr(bot, "token", None) if bot else None
        main_token = (get_settings().bot_token or "").strip()
        # Fail closed: a non-main token must NEVER use platform settings/ACL.
        if token and main_token and token != main_token:
            info = getattr(bot, "_pgclock_reseller", None)
            if not isinstance(info, dict):
                info = await lookup_reseller_by_bot_token(session, token)
            if info:
                reseller_owner_id = int(info["user_id"])
                reseller_profile_id = int(info["profile_id"])
                is_reseller_bot = True
                if bot is not None and not getattr(bot, "_pgclock_reseller", None):
                    try:
                        setattr(bot, "_pgclock_reseller", {
                            "profile_id": reseller_profile_id,
                            "user_id": reseller_owner_id,
                        })
                    except Exception:
                        pass
            else:
                logger.error(
                    "Rejecting update: bot token is not main and not a known reseller shop"
                )
                msg = _reply_message(event)
                if msg:
                    try:
                        await msg.answer(
                            "ربات فروشگاه شناسایی نشد. با پشتیبانی تماس بگیرید."
                        )
                    except Exception:
                        pass
                return None

        data["reseller_owner_id"] = reseller_owner_id
        data["reseller_profile_id"] = reseller_profile_id
        data["is_reseller_bot"] = is_reseller_bot

        ctx_token = set_shop_reseller_id(reseller_owner_id)
        try:
            tg_user = _extract_from_user(event)
            if tg_user:
                referred_by_code = None
                payload = _extract_start_payload(event)
                if payload and payload.startswith("ref_"):
                    referred_by_code = payload[4:].strip()
                try:
                    user = await get_or_create_user(
                        session,
                        tg_user.id,
                        username=tg_user.username,
                        full_name=tg_user.full_name,
                        referred_by_code=referred_by_code,
                        reseller_owner_id=reseller_owner_id,
                    )
                except Exception:
                    logger.exception("Failed to load/create bot user tg_id=%s", tg_user.id)
                    msg = _reply_message(event)
                    if msg:
                        try:
                            await msg.answer("خطای موقت. چند ثانیه بعد دوباره /start بزنید.")
                        except Exception:
                            pass
                    return None
                data["db_user"] = user
                if user.is_blocked:
                    msg = _reply_message(event)
                    if msg:
                        try:
                            await msg.answer("دسترسی شما مسدود شده است.")
                        except Exception:
                            pass
                    cq = event.callback_query if isinstance(event, Update) else (
                        event if isinstance(event, CallbackQuery) else None
                    )
                    if cq is not None:
                        try:
                            await cq.answer("دسترسی شما مسدود شده است.", show_alert=True)
                        except Exception:
                            pass
                    return None
            return await handler(event, data)
        finally:
            reset_shop_reseller_id(ctx_token)


class ForceJoinMiddleware(BaseMiddleware):
    """Block shop/pay actions until channel membership is verified (real API check)."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        session: AsyncSession | None = data.get("session")
        bot: Bot | None = data.get("bot")
        db_user = data.get("db_user")
        if not session or not bot or not db_user:
            return await handler(event, data)

        # Always allow /start so the join prompt can be shown
        if _extract_start_payload(event) is not None or _is_bare_start(event):
            return await handler(event, data)
        # Allow re-check callback (must not be blocked before the handler runs)
        cb = _callback_data(event)
        if cb and (cb.startswith("forcejoin:") or cb.startswith("terms:")):
            return await handler(event, data)

        from app.services.users import (
            get_all_settings,
            on,
            parse_force_join_channels,
            parse_force_join_entries,
        )
        from app.services.reseller_access import effective_menu_role
        from app.bot import keyboards as kb

        ui = await get_all_settings(session)
        enabled = ui.get("force_join_enabled")
        raw_channels = ui.get("force_join_channel")
        entries = parse_force_join_entries(raw_channels)
        channels = parse_force_join_channels(raw_channels)
        if not on(enabled) or not channels:
            return await handler(event, data)

        role = await effective_menu_role(
            session,
            db_user,
            is_reseller_bot=bool(data.get("is_reseller_bot")),
            reseller_owner_id=data.get("reseller_owner_id"),
        )
        data["menu_role"] = role
        if role != "user":
            return await handler(event, data)

        missing, unverified = await check_force_join_all(
            bot, int(db_user.telegram_id), channels, entries=entries
        )
        if missing or unverified:
            msg = _reply_message(event)
            text = force_join_block_message(
                missing, unverified, custom=ui.get("force_join_msg")
            )
            markup = kb.force_join_inline_keyboard(
                raw_channels, ui=ui, channels=channels
            )
            if msg:
                try:
                    await msg.answer(text, reply_markup=markup)
                except Exception:
                    pass
            cq = event.callback_query if isinstance(event, Update) else (
                event if isinstance(event, CallbackQuery) else None
            )
            if cq is not None:
                alert = (
                    "عضویت تأیید نشد — ربات باید ادمین کانال باشد"
                    if unverified and not missing
                    else "هنوز عضو کانال‌ها نشده‌اید"
                )
                try:
                    await cq.answer(alert, show_alert=True)
                except Exception:
                    pass
            return None

        return await handler(event, data)


def _is_bare_start(event: TelegramObject) -> bool:
    text = None
    if isinstance(event, Message) and event.text:
        text = event.text.strip()
    elif isinstance(event, Update) and event.message and event.message.text:
        text = event.message.text.strip()
    if not text:
        return False
    return text.split()[0].startswith("/start")



class TermsEntryMiddleware(BaseMiddleware):
    """Block end-user actions until entry terms are accepted (after force-join)."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        session: AsyncSession | None = data.get("session")
        db_user = data.get("db_user")
        if not session or not db_user:
            return await handler(event, data)

        if _extract_start_payload(event) is not None or _is_bare_start(event):
            return await handler(event, data)
        cb = _callback_data(event)
        if cb and cb.startswith("terms:"):
            return await handler(event, data)
        # Force-join re-check must still run before terms
        if cb and cb.startswith("forcejoin:"):
            return await handler(event, data)

        from app.services.users import get_all_settings
        from app.services.reseller_access import effective_menu_role
        from app.services.terms import needs_entry_gate
        from app.bot import keyboards as kb

        ui = await get_all_settings(session)
        role = data.get("menu_role")
        if role is None:
            role = await effective_menu_role(
                session,
                db_user,
                is_reseller_bot=bool(data.get("is_reseller_bot")),
                reseller_owner_id=data.get("reseller_owner_id"),
            )
            data["menu_role"] = role
        if role != "user":
            return await handler(event, data)

        prompt = await needs_entry_gate(
            session,
            db_user,
            ui,
            menu_role=role,
            reseller_owner_id=data.get("reseller_owner_id"),
        )
        if prompt is None:
            return await handler(event, data)

        msg = _reply_message(event)
        kwargs = {}
        if prompt.entities:
            kwargs["entities"] = prompt.entities
            kwargs["parse_mode"] = None
        markup = kb.terms_inline_keyboard(prompt, ui)
        if msg:
            try:
                await msg.answer(prompt.text, reply_markup=markup, **kwargs)
            except Exception:
                pass
        cq = event.callback_query if isinstance(event, Update) else (
            event if isinstance(event, CallbackQuery) else None
        )
        if cq is not None:
            try:
                await cq.answer("ابتدا قوانین را بپذیرید", show_alert=True)
            except Exception:
                pass
        return None


class ErrorLogMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        try:
            return await handler(event, data)
        except Exception as e:
            # Propagate control-flow exceptions so later handlers / filters can run
            from aiogram.dispatcher.event.bases import CancelHandler, SkipHandler

            if isinstance(e, (SkipHandler, CancelHandler)):
                raise
            if _is_benign_telegram_error(e):
                logger.debug("Ignored benign Telegram error: %s", e)
                return None
            logger.exception("Unhandled bot error")
            msg = _reply_message(event)
            if msg:
                try:
                    await msg.answer("خطایی رخ داد. لطفاً دوباره /start را بزنید.")
                except Exception:
                    pass
            # do not re-raise so polling keeps running
            return None
