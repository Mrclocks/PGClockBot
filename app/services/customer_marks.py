"""A customer's private staff note and risk tag, kept separately by each shop.

A shop's marks live on the person's membership row for that shop (``bot_user_shops``); the
platform's live on ``BotUser``. Every reader asks for one shop's marks, so a person who buys
from several shops is never shown another shop's note or tag.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.db.models import BotUser, BotUserShop
from app.services.authz import authz_from_staff, can_shop
from app.services.color_tags import DEFAULT_COLOR_TAG, LEGACY_GREEN_TAGS, LEGACY_YELLOW_TAGS, normalize_color_tag
from app.services.shop_membership import ensure_member, is_shop_customer
from app.services.shop_scope import is_platform_admin, shop_owner_id

CUSTOMER_NOTES_PERMISSION = "customer_notes"
NOTE_MAX = 2000


@dataclass(frozen=True, slots=True)
class CustomerMarks:
    note: str
    tag: str


EMPTY_MARKS = CustomerMarks(note="", tag=DEFAULT_COLOR_TAG)


def has_customer_notes_permission(staff: dict | None) -> bool:
    """The platform owner always; shop staff only with the ``customer_notes`` key."""
    if not staff:
        return False
    return is_platform_admin(staff) or can_shop(authz_from_staff(staff), CUSTOMER_NOTES_PERMISSION)


def marks_scope(staff: dict | None) -> int | None:
    """Shop whose marks ``staff`` reads and writes (``None`` = platform); raises ``PermissionError``."""
    if staff and is_platform_admin(staff):
        return None
    shop_id = shop_owner_id(staff or {})
    if shop_id is None:
        raise PermissionError("حساب شما به فروشگاه متصل نیست")
    return shop_id


def _marks(note: str | None, tag: str | None) -> CustomerMarks:
    return CustomerMarks(note=note or "", tag=normalize_color_tag(tag))


async def marks_for(session: AsyncSession, shop_id: int | None, user_ids: Iterable[int]) -> dict[int, CustomerMarks]:
    """One shop's marks for several people in one query; people without marks get the defaults."""
    ids = sorted({int(user_id) for user_id in user_ids})
    found: dict[int, CustomerMarks] = {user_id: EMPTY_MARKS for user_id in ids}
    if not ids:
        return found
    if shop_id is None:
        # Only the platform writes these fields; shop marks moved to the membership rows.
        query = select(BotUser.id, BotUser.staff_note, BotUser.color_tag).where(BotUser.id.in_(ids))
    else:
        query = select(BotUserShop.bot_user_id, BotUserShop.staff_note, BotUserShop.color_tag).where(
            BotUserShop.bot_user_id.in_(ids), BotUserShop.shop_id == int(shop_id)
        )
    for user_id, note, tag in (await session.execute(query)).all():
        found[int(user_id)] = _marks(note, tag)
    return found


async def marks_of(session: AsyncSession, shop_id: int | None, user: BotUser) -> CustomerMarks:
    return (await marks_for(session, shop_id, [int(user.id)]))[int(user.id)]


async def marks_for_staff(session: AsyncSession, staff: dict | None, user: BotUser) -> CustomerMarks:
    """The viewing staff member's own shop's marks; nothing for staff without a shop."""
    try:
        shop_id = marks_scope(staff)
    except PermissionError:
        return EMPTY_MARKS
    return await marks_of(session, shop_id, user)


async def may_edit_private_marks(session: AsyncSession, staff: dict | None, user: BotUser) -> bool:
    """Staff with the permission may mark any customer of their own shop (attributed or member)."""
    if not has_customer_notes_permission(staff):
        return False
    try:
        shop_id = marks_scope(staff)
    except PermissionError:
        return False
    return await is_shop_customer(session, user, shop_id)


async def save_marks(
    session: AsyncSession,
    *,
    staff: dict,
    user: BotUser,
    note: str | None = None,
    tag: str | None = None,
) -> None:
    """Write the acting shop's note and/or tag; the caller commits. Raises ``PermissionError``."""
    if not await may_edit_private_marks(session, staff, user):
        raise PermissionError("دسترسی به یادداشت و تگ این مشتری ندارید")
    shop_id = marks_scope(staff)
    values: dict[str, str | None] = {}
    if note is not None:
        values["staff_note"] = note.strip()[:NOTE_MAX] or None
    if tag is not None:
        values["color_tag"] = normalize_color_tag(tag)
    if not values:
        return
    if shop_id is None:
        for name, value in values.items():
            setattr(user, name, value)
        return
    await ensure_member(session, int(user.id), shop_id)
    await session.execute(
        update(BotUserShop)
        .where(BotUserShop.bot_user_id == int(user.id), BotUserShop.shop_id == int(shop_id))
        .values(**values)
        .execution_options(synchronize_session=False)
    )


def color_filter_clause(shop_id: int | None, color: str) -> ColumnElement[bool]:
    """For the users list: people carrying ``color`` in this shop's own marks."""
    if color == DEFAULT_COLOR_TAG:
        wanted = (DEFAULT_COLOR_TAG, *LEGACY_GREEN_TAGS)
    elif color == "yellow":
        wanted = ("yellow", *LEGACY_YELLOW_TAGS)
    else:
        wanted = (color,)
    if shop_id is None:
        stored = BotUser.color_tag.in_(wanted)
        return (stored | BotUser.color_tag.is_(None)) if color == DEFAULT_COLOR_TAG else stored
    tagged = exists(
        select(BotUserShop.id).where(
            BotUserShop.bot_user_id == BotUser.id,
            BotUserShop.shop_id == int(shop_id),
            BotUserShop.color_tag.in_(wanted),
        )
    )
    if color != DEFAULT_COLOR_TAG:
        return tagged
    # A customer the shop never tagged counts as the default.
    other = exists(
        select(BotUserShop.id).where(
            BotUserShop.bot_user_id == BotUser.id,
            BotUserShop.shop_id == int(shop_id),
            BotUserShop.color_tag.is_not(None),
            BotUserShop.color_tag.not_in(wanted),
        )
    )
    return ~other
