"""Mini App ops ACL — role isolation for reseller/admin review actions.

Fail-closed rules (never regress):
- Platform admin: only platform-scoped payments/tickets (no shop tenant data).
- Reseller: only own shop (``order.reseller_id == profile.user_id`` / sticky customers).
- Wallet top-ups: platform admin only (never reseller — shared wallet mint risk).
- No shop ContextVar spoofing; Mini App always runs on platform bot token.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, Order, Payment, ResellerProfile, Ticket
from app.services.miniapp_auth import load_reseller_profile, resolve_mini_persona
from app.services.resellers import has_bot_perm


def require_ops_persona(user: BotUser) -> str:
    persona = resolve_mini_persona(user)
    if persona not in {"admin", "reseller"}:
        raise HTTPException(403, "دسترسی عملیات ندارید")
    return persona


async def load_ops_context(
    session: AsyncSession, user: BotUser
) -> tuple[str, ResellerProfile | None]:
    """Return (persona, reseller_profile|None). Reseller without profile → 403."""
    persona = require_ops_persona(user)
    if persona == "admin":
        return persona, None
    profile = await load_reseller_profile(session, user)
    if not profile or not profile.is_active:
        raise HTTPException(403, "پروفایل نماینده فعال نیست")
    if int(profile.user_id) != int(user.id):
        raise HTTPException(403, "دسترسی ندارید")
    return persona, profile


def mini_can_review_payment(
    *,
    persona: str,
    reviewer: BotUser,
    payment: Payment,
    order: Order | None,
    profile: ResellerProfile | None,
) -> bool:
    """Authorize approve/reject for Mini App ops (no shop bot ContextVar)."""
    if payment is None:
        return False
    # Shared global wallet must never be mintable by a tenant reviewer.
    if payment.is_wallet_topup:
        return persona == "admin"

    if persona == "admin":
        if order is not None and order.reseller_id:
            return False
        return True

    if persona != "reseller" or profile is None:
        return False
    if not has_bot_perm(profile, "payments"):
        return False
    if order is None or order.reseller_id is None:
        return False
    return int(order.reseller_id) == int(profile.user_id)


def mini_can_access_ticket(
    *,
    persona: str,
    actor: BotUser,
    ticket: Ticket,
    profile: ResellerProfile | None,
) -> bool:
    if ticket is None:
        return False
    if persona == "admin":
        return ticket.reseller_id is None
    if persona == "reseller":
        if profile is None or not has_bot_perm(profile, "tickets"):
            return False
        return ticket.reseller_id is not None and int(ticket.reseller_id) == int(profile.user_id)
    # End-user: own tickets only
    return int(ticket.user_id) == int(actor.id)


def mini_can_manage_customer(
    *,
    persona: str,
    customer: BotUser,
    profile: ResellerProfile | None,
) -> bool:
    if customer is None:
        return False
    if persona == "admin":
        # Platform admin may see platform-sticky users only (no shop reseller_id).
        return customer.reseller_id is None
    if persona != "reseller" or profile is None:
        return False
    return customer.reseller_id is not None and int(customer.reseller_id) == int(profile.user_id)
