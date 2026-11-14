"""Which shop a referral belongs to.

A person has one referrer, recorded in the bot where they registered with an invite
code. That bot's shop (``BotUser.referral_shop_id``, None = the platform) is the only
shop whose purchases reward the referrer and whose club counts the invite: a customer
of a shop and of the platform must not earn either side's referral rewards from
purchases made on the other side.
"""

from __future__ import annotations

from sqlalchemy import ColumnElement

from app.db.models import BotUser


def referral_shop_clause(shop_id: int | None) -> ColumnElement[bool]:
    """People whose referral was made in ``shop_id``'s bot (None = the platform)."""
    if shop_id:
        return BotUser.referral_shop_id == int(shop_id)
    return BotUser.referral_shop_id.is_(None)


def is_referral_of_shop(user: BotUser, shop_id: int | None) -> bool:
    """True when ``user`` was referred in ``shop_id``'s bot (None = the platform)."""
    if not user.referred_by_id:
        return False
    return (user.referral_shop_id or None) == (int(shop_id) if shop_id else None)
