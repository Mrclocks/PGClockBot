"""Mini App onboarding: the referral code, required channels and entry terms, finished in the Mini App.

The bot asks for these in its chat before anything else. Someone who opens the Mini App from
Telegram search has never seen that chat, so the Mini App shows the same steps itself: the
same settings, the same membership check through the bot, and the same acceptance record.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser
from app.services.miniapp_auth import ForceJoinCheck, is_gated_customer, mini_force_join_check
from app.services.miniapp_shop import MiniScope
from app.services.referral_registration import ReferralRequired
from app.services.rich_text import visible_text
from app.services.terms import build_prompt, needs_entry_gate, record_acceptance, shop_scope_id
from app.services.users import DEFAULT_SETTINGS, force_join_channel_url, get_all_settings

STEP_REFERRAL = "referral"
STEP_FORCE_JOIN = "force_join"
STEP_TERMS = "terms"
STEP_READY = "ready"

CHANNEL_JOINED = "joined"
CHANNEL_MISSING = "missing"
CHANNEL_UNVERIFIED = "unverified"

TELEGRAM_LINK_PREFIX = "https://t.me/"
LABEL_MAX_CHARS = 64
TEXT_MAX_CHARS = 4096

FORCE_JOIN_TEXT = "برای استفاده از مینی‌اپ، ابتدا در کانال‌های زیر عضو شوید و بعد «عضو شدم» را بزنید."
FORCE_JOIN_UNVERIFIED_TEXT = (
    "الان نمی‌توان عضویت شما را تأیید کرد. چند لحظه بعد دوباره «عضو شدم» را بزنید؛ "
    "اگر درست نشد به پشتیبانی خبر دهید."
)
REFERRAL_INVALID_TEXT = "کد معرف معتبر نیست. کد معتبر همین فروشگاه را وارد کنید."
DEFAULT_JOIN_LABEL = "عضویت در کانال"
DEFAULT_CHECK_LABEL = "عضو شدم"
DEFAULT_TERMS_BUTTON = "موافقم"


class OnboardingThrottled(ValueError):
    """The same person repeated an onboarding step faster than allowed."""


class _Throttle:
    """At most ``limit`` calls per key in ``window`` seconds, in this process; memory is bounded."""

    def __init__(self, *, limit: int, window: float, max_keys: int = 5000) -> None:
        self._limit = limit
        self._window = window
        self._max_keys = max_keys
        self._hits: dict[int, deque[float]] = {}

    def check(self, key: int) -> None:
        now = time.monotonic()
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] >= self._window:
            hits.popleft()
        if len(hits) >= self._limit:
            raise OnboardingThrottled("چند ثانیه صبر کنید و دوباره تلاش کنید")
        hits.append(now)
        if len(self._hits) > self._max_keys:
            self._prune(now)

    def _prune(self, now: float) -> None:
        idle = [k for k, v in self._hits.items() if not v or now - v[-1] >= self._window]
        for key in idle:
            del self._hits[key]
        while len(self._hits) > self._max_keys:
            del self._hits[next(iter(self._hits))]


# «عضو شدم» asks Telegram once per channel; a referral code is a guess against other people's codes.
_JOIN_CHECKS = _Throttle(limit=1, window=3.0)
_REFERRAL_TRIES = _Throttle(limit=5, window=60.0)


def throttle_join_check(telegram_id: int) -> None:
    _JOIN_CHECKS.check(int(telegram_id))


def throttle_referral_try(telegram_id: int) -> None:
    _REFERRAL_TRIES.check(int(telegram_id))


def _plain(raw: str | None, *, limit: int, default: str = "") -> str:
    return (visible_text(raw).strip() or default)[:limit]


def referral_step(exc: ReferralRequired) -> dict[str, Any]:
    """The referral step: the admin's text (plain, for a web page) and a code field."""
    default = DEFAULT_SETTINGS["referral_required_text"]
    return {
        "step": STEP_REFERRAL,
        "text": _plain(exc.ui.get("referral_required_text"), limit=TEXT_MAX_CHARS, default=default),
        "invalid": bool(exc.invalid),
    }


def _channel_status(channel: str, check: ForceJoinCheck) -> str:
    if channel in check.missing:
        return CHANNEL_MISSING
    if channel in check.unverified:
        return CHANNEL_UNVERIFIED
    return CHANNEL_JOINED


def _channel_row(channel: str, entry: dict[str, Any], check: ForceJoinCheck, join_label: str) -> dict[str, str]:
    """One required channel as the page shows it; private chat ids never leave the server."""
    url = force_join_channel_url(channel, entry.get("link")) or ""
    title = str(entry.get("title") or "").strip()
    if title:
        label = title
    elif channel.startswith("@"):
        label = f"{join_label} · {channel}"
    else:
        label = join_label
    return {
        "label": label[:LABEL_MAX_CHARS],
        "url": url if url.startswith(TELEGRAM_LINK_PREFIX) else "",
        "status": _channel_status(channel, check),
    }


def force_join_step(check: ForceJoinCheck, ui: dict[str, str]) -> dict[str, Any]:
    by_id = {str(e.get("id") or "").lower(): e for e in check.entries if e.get("id")}
    join_label = _plain(ui.get("btn_force_join"), limit=LABEL_MAX_CHARS, default=DEFAULT_JOIN_LABEL)
    only_unverified = bool(check.unverified) and not check.missing
    return {
        "step": STEP_FORCE_JOIN,
        "text": FORCE_JOIN_UNVERIFIED_TEXT if only_unverified else FORCE_JOIN_TEXT,
        "check_label": _plain(ui.get("btn_force_join_check"), limit=LABEL_MAX_CHARS, default=DEFAULT_CHECK_LABEL),
        "channels": [_channel_row(ch, by_id.get(ch.lower()) or {}, check, join_label) for ch in check.channels],
    }


async def onboarding_state(
    session: AsyncSession, user: BotUser, *, scope: MiniScope, fresh: bool = False
) -> dict[str, Any]:
    """The first step this person still has to finish here, in the bot's order: channels, then terms."""
    check = await mini_force_join_check(session, user, scope=scope, fresh=fresh)
    ui = await get_all_settings(session, reseller_id=scope.shop_id)
    if check is not None and not check.passed:
        return force_join_step(check, ui)
    if is_gated_customer(user, scope):
        prompt = await needs_entry_gate(session, user, ui, menu_role="user", reseller_owner_id=scope.shop_id)
        if prompt is not None:
            return {
                "step": STEP_TERMS,
                "text": _plain(prompt.text, limit=TEXT_MAX_CHARS),
                "button": _plain(prompt.btn_label, limit=LABEL_MAX_CHARS, default=DEFAULT_TERMS_BUTTON),
            }
    return {"step": STEP_READY}


async def accept_entry_terms(session: AsyncSession, user: BotUser, *, scope: MiniScope) -> None:
    """Record that the person accepted this bot's current entry terms (the same record the bot keeps).

    Accepting twice keeps one row (unique per person, shop and gate). Staff are never asked,
    and nothing is recorded while the terms are off.
    """
    if not is_gated_customer(user, scope):
        return
    ui = await get_all_settings(session, reseller_id=scope.shop_id)
    if build_prompt(ui, "entry") is None:
        return
    await record_acceptance(
        session,
        bot_user_id=int(user.id),
        shop_owner_id=shop_scope_id(reseller_owner_id=scope.shop_id),
        gate="entry",
        ui=ui,
    )
