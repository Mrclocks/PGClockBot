"""Independent Telegram consent and website confirmation; never transfer resources."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.customer_models import Customer
from app.db.customer_link_models import CustomerLinkAction, CustomerLinkChallenge, CustomerTelegramLink
from app.db.integration_models import Integration
from app.db.models import BotUser
from app.services.integration_authority import IntegrationAuthority, resolve_integration_authority
from app.services.integration_events import link_event
from app.services.integration_credentials import verify_credential
from app.services.integration_customers import _customer_query
from app.services.integration_policy import IntegrationError, utc_time
from app.services.integration_request_keys import request_key_digest
from app.services.integration_replays import RequestBinding, bind_replay, lookup_replay

LINK_SCOPES = frozenset({"telegram:link"})
CHALLENGE_TTL = timedelta(minutes=10)
CHALLENGE_LIMIT = 3
CHALLENGE_HOURLY_LIMIT = 5


@dataclass(frozen=True, slots=True)
class LinkChallengeView:
    challenge_id: str
    state: str
    expires_at: datetime
    start_token: str | None = None


async def _web_context(session: AsyncSession, bearer: str, customer_id: str) -> tuple[IntegrationAuthority, Customer]:
    await verify_credential(session, bearer, lock_integration=True)
    authority = await resolve_integration_authority(session, bearer, required_scopes=LINK_SCOPES)
    row = (await session.execute(_customer_query(authority).where(Customer.public_id == customer_id))).one_or_none()
    if row is None:
        raise IntegrationError("not_found")
    if row[0].state != "active":
        raise IntegrationError("customer_blocked")
    return authority, row[0]


def challenge_view(row: CustomerLinkChallenge, now: datetime, token: str | None = None) -> LinkChallengeView:
    if row.cancelled_at:
        state = "cancelled"
    elif row.consumed_at:
        state = "linked"
    elif utc_time(row.expires_at) <= now:
        state = "expired"
    else:
        state = "awaiting_website" if row.telegram_confirmed_at else "awaiting_telegram"
    return LinkChallengeView(row.id, state, utc_time(row.expires_at), token)


async def start_link_challenge(
    session: AsyncSession, bearer: str, customer_id: str, *, idempotency_key: str,
) -> LinkChallengeView:
    digest = request_key_digest(idempotency_key)
    try:
        authority, customer = await _web_context(session, bearer, customer_id)
        binding = RequestBinding("POST", "/telegram-link-challenges", customer_id, {})
        await lookup_replay(session, authority, idempotency_key, binding)
        now = datetime.now(timezone.utc)
        existing = await session.scalar(select(CustomerLinkChallenge).where(
            CustomerLinkChallenge.integration_id == authority.integration_id,
            CustomerLinkChallenge.creation_key_digest == digest,
        ))
        if existing:
            if existing.customer_id != customer.id:
                raise IntegrationError("idempotency_conflict")
            result = challenge_view(existing, now)
        else:
            pending = await session.scalar(select(func.count()).select_from(CustomerLinkChallenge).where(
                CustomerLinkChallenge.integration_id == authority.integration_id,
                CustomerLinkChallenge.customer_id == customer.id, CustomerLinkChallenge.expires_at > now,
                CustomerLinkChallenge.consumed_at.is_(None), CustomerLinkChallenge.cancelled_at.is_(None),
            ))
            recent = await session.scalar(select(func.count()).select_from(CustomerLinkChallenge).where(
                CustomerLinkChallenge.integration_id == authority.integration_id,
                CustomerLinkChallenge.customer_id == customer.id, CustomerLinkChallenge.created_at > now-timedelta(hours=1),
            ))
            if pending >= CHALLENGE_LIMIT or recent >= CHALLENGE_HOURLY_LIMIT:
                raise IntegrationError("queue_limit_reached")
            token = secrets.token_urlsafe(32)
            row = CustomerLinkChallenge(integration_id=authority.integration_id, customer_id=customer.id,
                initiating_credential_selector=authority.credential_selector, creation_key_digest=digest, token_digest=hashlib.sha256(token.encode("ascii")).hexdigest(),
                expires_at=now + CHALLENGE_TTL, created_at=now)
            session.add(row)
            await session.flush()
            result = challenge_view(row, now, token)
        await bind_replay(session, authority, idempotency_key, binding, resource_kind="challenge", resource_id=result.challenge_id)
        await session.commit()
        return result
    except BaseException:
        await session.rollback()
        raise


async def read_link_challenge(session: AsyncSession, bearer: str, customer_id: str, challenge_id: str) -> LinkChallengeView:
    try:
        authority, customer = await _web_context(session, bearer, customer_id)
        row = await session.scalar(select(CustomerLinkChallenge).where(
            CustomerLinkChallenge.id == challenge_id, CustomerLinkChallenge.integration_id == authority.integration_id,
            CustomerLinkChallenge.customer_id == customer.id,
        ))
        if row is None:
            raise IntegrationError("not_found")
        return challenge_view(row, datetime.now(timezone.utc))
    finally:
        await session.rollback()


async def telegram_sender(session: AsyncSession, sender_id: int) -> BotUser:
    if type(sender_id) is not int or not 0 < sender_id <= 2**63 - 1:
        raise IntegrationError("permission_denied")
    user = await session.scalar(select(BotUser).where(BotUser.telegram_id == sender_id, BotUser.is_blocked.is_(False)))
    if user is None:
        raise IntegrationError("permission_denied")
    return user


async def present_telegram_challenge(session: AsyncSession, token: str, *, sender_id: int, shop_id: int | None) -> str:
    """Opening a token identifies a candidate but does not record consent."""
    try:
        if not isinstance(token, str) or len(token) != 43 or not token.isascii():
            raise IntegrationError("not_found")
        user = await telegram_sender(session, sender_id)
        now = datetime.now(timezone.utc)
        row = await session.scalar(select(CustomerLinkChallenge).join(Integration).join(
            Customer, Customer.id == CustomerLinkChallenge.customer_id,
        ).where(CustomerLinkChallenge.token_digest == hashlib.sha256(token.encode("ascii")).hexdigest(),
            Integration.shop_id == shop_id, Integration.status == "active", Customer.state == "active",
            CustomerLinkChallenge.expires_at > now, CustomerLinkChallenge.consumed_at.is_(None),
            CustomerLinkChallenge.cancelled_at.is_(None)))
        if row is None:
            raise IntegrationError("not_found")
        claimed = await session.execute(update(CustomerLinkChallenge).where(
            CustomerLinkChallenge.id == row.id, CustomerLinkChallenge.expires_at > now,
            CustomerLinkChallenge.consumed_at.is_(None), CustomerLinkChallenge.cancelled_at.is_(None),
            or_(CustomerLinkChallenge.candidate_bot_user_id.is_(None), CustomerLinkChallenge.candidate_bot_user_id == user.id),
        ).values(candidate_bot_user_id=user.id).execution_options(synchronize_session=False))
        if claimed.rowcount != 1:
            raise IntegrationError("link_conflict")
        await session.commit()
        return row.id
    except BaseException:
        await session.rollback()
        raise


async def confirm_telegram_challenge(
    session: AsyncSession, challenge_id: str, *, sender_id: int, shop_id: int | None,
) -> None:
    try:
        user = await telegram_sender(session, sender_id)
        now = datetime.now(timezone.utc)
        matching_integration = select(Integration.id).where(Integration.shop_id == shop_id, Integration.status == "active")
        claimed = await session.execute(update(CustomerLinkChallenge).where(
            CustomerLinkChallenge.id == challenge_id, CustomerLinkChallenge.candidate_bot_user_id == user.id,
            CustomerLinkChallenge.integration_id.in_(matching_integration), CustomerLinkChallenge.expires_at > now,
            CustomerLinkChallenge.cancelled_at.is_(None), CustomerLinkChallenge.consumed_at.is_(None),
        ).values(telegram_confirmed_at=now).execution_options(synchronize_session=False))
        if claimed.rowcount != 1:
            raise IntegrationError("link_not_confirmed")
        await session.commit()
    except BaseException:
        await session.rollback()
        raise


async def _record_action(session: AsyncSession, authority: IntegrationAuthority, customer: Customer,
                         *, key: str, action: str, challenge_id: str | None = None) -> bool:
    digest = request_key_digest(key)
    existing = await session.scalar(select(CustomerLinkAction).where(
        CustomerLinkAction.integration_id == authority.integration_id, CustomerLinkAction.key_digest == digest,
    ))
    if existing:
        if (existing.customer_id, existing.action, existing.challenge_id) != (customer.id, action, challenge_id):
            raise IntegrationError("idempotency_conflict")
        return False
    session.add(CustomerLinkAction(integration_id=authority.integration_id, customer_id=customer.id,
        key_digest=digest, action=action, challenge_id=challenge_id, credential_selector=authority.credential_selector, actor_kind="website"))
    return True


async def complete_link_challenge(session: AsyncSession, bearer: str, customer_id: str, challenge_id: str,
                                  *, idempotency_key: str, confirm: bool) -> None:
    try:
        if confirm is not True:
            raise IntegrationError("link_not_confirmed")
        authority, customer = await _web_context(session, bearer, customer_id)
        binding = RequestBinding("POST", "/telegram-link-challenges/confirm", customer_id,
                                 {"confirm": True, "challenge_id": challenge_id})
        await lookup_replay(session, authority, idempotency_key, binding)
        row = await session.scalar(select(CustomerLinkChallenge).where(
            CustomerLinkChallenge.id == challenge_id, CustomerLinkChallenge.integration_id == authority.integration_id,
            CustomerLinkChallenge.customer_id == customer.id,
        ).execution_options(populate_existing=True))
        if row is None:
            raise IntegrationError("not_found")
        await _record_action(session, authority, customer, key=idempotency_key, action="confirm", challenge_id=row.id)
        active = await session.scalar(select(CustomerTelegramLink.id).where(
            CustomerTelegramLink.challenge_id == row.id, CustomerTelegramLink.ended_at.is_(None),
        ))
        if row.consumed_at and active:
            await bind_replay(session, authority, idempotency_key, binding, resource_kind="challenge", resource_id=challenge_id)
            await session.commit()
            return
        now = datetime.now(timezone.utc)
        if row.cancelled_at or row.consumed_at:
            raise IntegrationError("link_conflict")
        if utc_time(row.expires_at) <= now:
            raise IntegrationError("challenge_expired")
        if not row.telegram_confirmed_at or row.candidate_bot_user_id is None:
            raise IntegrationError("link_not_confirmed")
        candidate = await session.scalar(select(BotUser.id).where(
            BotUser.id == row.candidate_bot_user_id, BotUser.telegram_id > 0, BotUser.is_blocked.is_(False),
        ))
        if candidate is None:
            raise IntegrationError("link_not_confirmed")
        claimed = await session.execute(update(CustomerLinkChallenge).where(
            CustomerLinkChallenge.id == row.id, CustomerLinkChallenge.consumed_at.is_(None),
            CustomerLinkChallenge.cancelled_at.is_(None), CustomerLinkChallenge.expires_at > now,
            CustomerLinkChallenge.telegram_confirmed_at.is_not(None),
            CustomerLinkChallenge.candidate_bot_user_id == candidate,
        ).values(consumed_at=now).execution_options(synchronize_session=False))
        if claimed.rowcount != 1:
            raise IntegrationError("link_conflict")
        session.add(CustomerTelegramLink(integration_id=authority.integration_id, customer_id=customer.id,
                                        bot_user_id=candidate, challenge_id=row.id))
        await session.flush()
        await link_event(session, authority.integration_id, customer.id, linked=True)
        await bind_replay(session, authority, idempotency_key, binding, resource_kind="challenge", resource_id=challenge_id)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise IntegrationError("link_conflict") from None
    except BaseException:
        await session.rollback()
        raise


async def end_customer_links(session: AsyncSession, integration_id: str, customer_id: int) -> None:
    now = datetime.now(timezone.utc)
    changed = await session.execute(update(CustomerTelegramLink).where(
        CustomerTelegramLink.integration_id == integration_id, CustomerTelegramLink.customer_id == customer_id,
        CustomerTelegramLink.ended_at.is_(None),
    ).values(ended_at=now, version=CustomerTelegramLink.version + 1))
    if changed.rowcount:
        await link_event(session, integration_id, customer_id, linked=False)
    await session.execute(update(CustomerLinkChallenge).where(
        CustomerLinkChallenge.integration_id == integration_id, CustomerLinkChallenge.customer_id == customer_id,
        CustomerLinkChallenge.cancelled_at.is_(None),
    ).values(cancelled_at=now))


async def unlink_website_customer(session: AsyncSession, bearer: str, customer_id: str, *, idempotency_key: str) -> None:
    try:
        authority, customer = await _web_context(session, bearer, customer_id)
        binding = RequestBinding("DELETE", "/telegram-link", customer_id, {})
        await lookup_replay(session, authority, idempotency_key, binding)
        if await _record_action(session, authority, customer, key=idempotency_key, action="unlink"):
            await end_customer_links(session, authority.integration_id, customer.id)
        await bind_replay(session, authority, idempotency_key, binding, resource_kind="customer", resource_id=customer_id)
        await session.commit()
    except BaseException:
        await session.rollback()
        raise


async def unlink_telegram_customer(session: AsyncSession, link_id: int, *, sender_id: int, confirmed: bool) -> None:
    """Consent revocation works even if the integration or public API was disabled."""
    try:
        if confirmed is not True or type(link_id) is not int or link_id <= 0:
            raise IntegrationError("link_not_confirmed")
        user = await telegram_sender(session, sender_id)
        row = await session.scalar(select(CustomerTelegramLink).where(
            CustomerTelegramLink.id == link_id, CustomerTelegramLink.bot_user_id == user.id,
        ))
        if row is None:
            raise IntegrationError("not_found")
        # The same integration write lock serializes revocation with website completion.
        await session.execute(update(Integration).where(Integration.id == row.integration_id).values(revision=Integration.revision + 1))
        await session.refresh(row)
        was_active = row.ended_at is None
        await end_customer_links(session, row.integration_id, row.customer_id)
        if was_active:
            session.add(CustomerLinkAction(integration_id=row.integration_id, customer_id=row.customer_id,
            key_digest=hashlib.sha256(f"telegram-unlink:{row.id}".encode()).hexdigest(), action="unlink",
                actor_bot_user_id=user.id, actor_kind="telegram"))
        await session.commit()
    except BaseException:
        await session.rollback()
        raise


@dataclass(frozen=True, slots=True)
class TelegramLinkLabel:
    id: str
    integration_id: str
    customer_id: str


async def telegram_challenge_label(session: AsyncSession, challenge_id: str, *, sender_id: int) -> TelegramLinkLabel:
    user = await telegram_sender(session, sender_id)
    row = (await session.execute(select(CustomerLinkChallenge.id, CustomerLinkChallenge.integration_id, Customer.public_id)
        .join(Customer, Customer.id == CustomerLinkChallenge.customer_id).where(
            CustomerLinkChallenge.id == challenge_id, CustomerLinkChallenge.candidate_bot_user_id == user.id,
        ))).one_or_none()
    if row is None:
        raise IntegrationError("not_found")
    return TelegramLinkLabel(*row)


async def telegram_link_labels(session: AsyncSession, *, sender_id: int, shop_id: int | None) -> tuple[TelegramLinkLabel, ...]:
    user = await telegram_sender(session, sender_id)
    rows = await session.execute(select(CustomerTelegramLink.id, CustomerTelegramLink.integration_id, Customer.public_id)
        .join(Customer, Customer.id == CustomerTelegramLink.customer_id)
        .join(Integration, Integration.id == CustomerTelegramLink.integration_id).where(
            CustomerTelegramLink.bot_user_id == user.id, CustomerTelegramLink.ended_at.is_(None),
            Integration.shop_id == shop_id,
        ).order_by(CustomerTelegramLink.id).limit(25))
    return tuple(TelegramLinkLabel(str(row[0]), row[1], row[2]) for row in rows)
