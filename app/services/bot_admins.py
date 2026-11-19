"""Who is a bot admin, and which role changes the web panel may make for a person.

The bot takes admin authority only from ``ADMIN_IDS``; ``BotUser.role == "admin"`` is a
copy that ``get_or_create_user`` and ``demote_stale_admin`` re-sync from that list on the
person's next update. A role form that removed or granted «admin» in the database would
be undone silently, so the panel refuses those changes and points to the list instead.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import get_settings
from app.db.models import BotUser, Role

BOT_ADMIN_LABEL = "ادمین ربات"
BOT_ADMIN_SETTINGS_HREF = "/settings?tab=bot"
BOT_ADMIN_ROLE_LOCKED = (
    "این شخص ادمین ربات است و نقشش از این فرم عوض نمی‌شود. "
    "برای برداشتن دسترسی، آیدی تلگرامش را از تنظیمات ربات ← آیدی ادمین‌ها پاک کنید."
)
BOT_ADMIN_GRANT_REFUSED = (
    "ادمین ربات از این فرم ساخته نمی‌شود. "
    "آیدی تلگرام شخص را در تنظیمات ربات ← آیدی ادمین‌ها اضافه کنید."
)
WEB_ASSIGNABLE_ROLES = frozenset({Role.USER.value, Role.RESELLER.value})


class BotAdminRoleLocked(ValueError):
    """A role change the bot would undo, because only ``ADMIN_IDS`` decides who is admin."""


@dataclass(frozen=True, slots=True)
class RoleBadge:
    label: str
    css_class: str


def is_bot_admin(user: BotUser | None) -> bool:
    """True when the person's Telegram id is in ``ADMIN_IDS``, the one source of bot admin authority."""
    if user is None:
        return False
    try:
        telegram_id = int(getattr(user, "telegram_id", 0) or 0)
    except (TypeError, ValueError):
        return False
    return telegram_id > 0 and telegram_id in get_settings().admin_ids


def role_badge(user: BotUser) -> RoleBadge:
    """The role the bot actually applies to this person, for the panel's role badge.

    A stored ``admin`` role without an ``ADMIN_IDS`` entry is dropped by the bot on the
    next update, so it reads as an ordinary user here.
    """
    if is_bot_admin(user):
        return RoleBadge(BOT_ADMIN_LABEL, "role-admin")
    if getattr(user, "role", None) == Role.RESELLER.value:
        return RoleBadge("نماینده", "role-reseller")
    return RoleBadge("کاربر", "role-user")


def assert_role_change_allowed(user: BotUser, new_role: str) -> None:
    """Refuse role changes the bot would revert on the person's next message.

    Raises ``BotAdminRoleLocked`` for any change to a bot admin, and for granting «admin»
    to anyone: both go through ``ADMIN_IDS`` in the bot settings.
    """
    if is_bot_admin(user):
        raise BotAdminRoleLocked(BOT_ADMIN_ROLE_LOCKED)
    if new_role == Role.ADMIN.value:
        raise BotAdminRoleLocked(BOT_ADMIN_GRANT_REFUSED)
