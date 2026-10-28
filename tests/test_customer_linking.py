"""Both proofs, scoped uniqueness, replay, expiry and consent revocation preserve ownership."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, update

from app.db.customer_link_models import CustomerLinkChallenge, CustomerTelegramLink
from app.db.customer_models import Customer
from app.db.models import BotUser, ResellerProfile
from app.services.customer_linking import (
    complete_link_challenge, confirm_telegram_challenge, present_telegram_challenge, read_link_challenge,
    start_link_challenge, unlink_telegram_customer, unlink_website_customer,
)
from app.services.integration_credentials import issue_credential
from app.services.integration_customers import CustomerInput, create_or_get_integration_customer, read_integration_customer
from app.services.integration_policy import IntegrationError
from app.services.integrations import configure_integration
from test_integration_concurrency import STAFF
from test_integration_customers import database


@asynccontextmanager
async def linking_database(url="sqlite+aiosqlite:///:memory:", schema=None):
    async with database(url, schema) as (sessions, integration, key):
        async with sessions() as session:
            await session.execute(update(ResellerProfile).values(web_permissions=
                "integrations_manage,integration_customers_read,integration_customers_write,integration_telegram_link"))
            await session.commit()
            scopes = frozenset({"customers:read", "customers:write", "telegram:link"})
            await configure_integration(session, STAFF, integration, status="active", grants=scopes,
                                        customer_entry_policy="website")
            key = await issue_credential(session, STAFF, integration, scopes=scopes)
            customer = (await create_or_get_integration_customer(session, key.bearer, CustomerInput("web-1"))).customer
        yield sessions, integration, key, customer


async def telegram_proof(session, challenge, *, sender_id=10011):
    challenge_id = await present_telegram_challenge(session, challenge.start_token, sender_id=sender_id, shop_id=11)
    await confirm_telegram_challenge(session, challenge_id, sender_id=sender_id, shop_id=11)


def test_opening_link_is_not_consent_and_website_cannot_assert_telegram_identity() -> None:
    async def scenario():
        async with linking_database() as (sessions, integration, key, customer), sessions() as session:
            challenge = await start_link_challenge(session, key.bearer, customer.id, idempotency_key="create-challenge-1")
            await present_telegram_challenge(session, challenge.start_token, sender_id=10011, shop_id=11)
            assert (await read_link_challenge(session, key.bearer, customer.id, challenge.challenge_id)).state == "awaiting_telegram"
            with pytest.raises(IntegrationError, match="تأیید"):
                await complete_link_challenge(session, key.bearer, customer.id, challenge.challenge_id,
                                              idempotency_key="confirm-challenge1", confirm=True)
            assert not (await read_integration_customer(session, key.bearer, customer.id)).telegram_linked
            assert await session.scalar(select(func.count()).select_from(CustomerTelegramLink)) == 0
    asyncio.run(scenario())


def test_dual_proof_replay_unlink_and_relink_preserve_customers_and_administrative_role() -> None:
    async def scenario():
        async with linking_database() as (sessions, integration, key, customer), sessions() as session:
            user = await session.get(BotUser, 11)
            user.role = "admin"
            await session.commit()
            challenge = await start_link_challenge(session, key.bearer, customer.id, idempotency_key="create-challenge-1")
            replay = await start_link_challenge(session, key.bearer, customer.id, idempotency_key="create-challenge-1")
            assert replay.challenge_id == challenge.challenge_id and replay.start_token is None
            await telegram_proof(session, challenge)
            for _ in range(2):
                await complete_link_challenge(session, key.bearer, customer.id, challenge.challenge_id,
                                              idempotency_key="confirm-challenge1", confirm=True)
            assert (await read_integration_customer(session, key.bearer, customer.id)).telegram_linked
            link = (await session.scalars(select(CustomerTelegramLink))).one()
            assert link.version == 1 and link.bot_user_id == 11
            assert user.role == "admin" and user.telegram_id == 10011
            canonical = await session.scalar(select(Customer).where(Customer.public_id == customer.id))
            assert canonical.legacy_bot_user_id is None
            for _ in range(2):
                await unlink_website_customer(session, key.bearer, customer.id, idempotency_key="unlink-customer-1")
            assert not (await read_integration_customer(session, key.bearer, customer.id)).telegram_linked
            with pytest.raises(IntegrationError):
                await complete_link_challenge(session, key.bearer, customer.id, challenge.challenge_id,
                                              idempotency_key="confirm-challenge1", confirm=True)
            assert await session.scalar(select(func.count()).select_from(CustomerTelegramLink)) == 1
            assert await session.scalar(select(func.count()).select_from(Customer)) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["wrong_sender", "wrong_shop", "missing_sender", "synthetic_sender", "expired", "cancelled"])
def test_telegram_confirmation_fails_closed(failure):
    async def scenario():
        async with linking_database() as (sessions, integration, key, customer), sessions() as session:
            challenge = await start_link_challenge(session, key.bearer, customer.id, idempotency_key="create-challenge-1")
            if failure == "expired":
                await session.execute(update(CustomerLinkChallenge).values(expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)))
                await session.commit()
            if failure == "cancelled":
                await unlink_website_customer(session, key.bearer, customer.id, idempotency_key="unlink-customer-1")
            with pytest.raises(IntegrationError):
                await present_telegram_challenge(session, challenge.start_token,
                    sender_id=10012 if failure == "wrong_sender" else (0 if failure == "synthetic_sender" else (True if failure == "missing_sender" else 10011)),
                    shop_id=12 if failure == "wrong_shop" else 11)
            assert await session.scalar(select(func.count()).select_from(CustomerTelegramLink)) == 0
    asyncio.run(scenario())


def test_same_telegram_account_cannot_take_two_customers_in_one_integration() -> None:
    async def scenario():
        async with linking_database() as (sessions, integration, key, customer), sessions() as session:
            second = (await create_or_get_integration_customer(session, key.bearer, CustomerInput("web-2"))).customer
            for index, value in enumerate((customer, second)):
                challenge = await start_link_challenge(session, key.bearer, value.id, idempotency_key=f"create-challenge-{index}")
                await telegram_proof(session, challenge)
                if index:
                    with pytest.raises(IntegrationError) as error:
                        await complete_link_challenge(session, key.bearer, value.id, challenge.challenge_id,
                                                      idempotency_key="confirm-customer2", confirm=True)
                    assert error.value.code == "link_conflict"
                else:
                    await complete_link_challenge(session, key.bearer, value.id, challenge.challenge_id,
                                                  idempotency_key="confirm-customer1", confirm=True)
            assert (await read_integration_customer(session, key.bearer, customer.id)).telegram_linked
            assert not (await read_integration_customer(session, key.bearer, second.id)).telegram_linked
    asyncio.run(scenario())


def test_customer_selector_key_binding_and_challenge_limit() -> None:
    async def scenario():
        async with linking_database() as (sessions, integration, key, customer), sessions() as session:
            second = (await create_or_get_integration_customer(session, key.bearer, CustomerInput("web-2"))).customer
            challenge = await start_link_challenge(session, key.bearer, customer.id, idempotency_key="create-challenge-0")
            with pytest.raises(IntegrationError) as error:
                await start_link_challenge(session, key.bearer, second.id, idempotency_key="create-challenge-0")
            assert error.value.code == "idempotency_conflict"
            with pytest.raises(IntegrationError) as error:
                await read_link_challenge(session, key.bearer, second.id, challenge.challenge_id)
            assert error.value.code == "not_found"
            for index in (1, 2):
                await start_link_challenge(session, key.bearer, customer.id, idempotency_key=f"create-challenge-{index}")
            with pytest.raises(IntegrationError) as error:
                await start_link_challenge(session, key.bearer, customer.id, idempotency_key="create-challenge-3")
            assert error.value.code == "queue_limit_reached"
    asyncio.run(scenario())


def test_bot_consent_revocation_is_scoped_and_survives_disabled_integration() -> None:
    from app.db.customer_link_models import CustomerLinkAction
    from app.db.integration_models import Integration

    async def scenario():
        async with linking_database() as (sessions, integration, key, customer), sessions() as session:
            challenge = await start_link_challenge(session, key.bearer, customer.id, idempotency_key="create-challenge-1")
            await telegram_proof(session, challenge)
            await complete_link_challenge(session, key.bearer, customer.id, challenge.challenge_id,
                                          idempotency_key="confirm-customer1", confirm=True)
            link = (await session.scalars(select(CustomerTelegramLink))).one()
            link_id = link.id
            await session.execute(update(Integration).values(status="disabled"))
            await session.commit()
            with pytest.raises(IntegrationError):
                await unlink_telegram_customer(session, link_id, sender_id=10012, confirmed=True)
            for _ in range(2):
                await unlink_telegram_customer(session, link_id, sender_id=10011, confirmed=True)
            await session.refresh(link)
            assert link.ended_at is not None and link.version == 2
            actions = list(await session.scalars(select(CustomerLinkAction).where(CustomerLinkAction.actor_kind == "telegram")))
            assert len(actions) == 1 and actions[0].actor_bot_user_id == 11
    asyncio.run(scenario())


def test_legacy_deletion_cannot_erase_telegram_consent_or_its_actor_evidence() -> None:
    from unittest.mock import AsyncMock, patch

    from app.services.users import delete_bot_user

    async def scenario():
        async with linking_database() as (sessions, integration, key, customer), sessions() as session:
            session.add(BotUser(id=12, telegram_id=10012, referral_code="synthetic-customer"))
            await session.commit()
            challenge = await start_link_challenge(session, key.bearer, customer.id, idempotency_key="create-challenge-1")
            await telegram_proof(session, challenge, sender_id=10012)
            await complete_link_challenge(session, key.bearer, customer.id, challenge.challenge_id,
                                          idempotency_key="confirm-customer1", confirm=True)
            with patch("app.services.pasarguard.get_pg", AsyncMock()) as panel:
                with pytest.raises(ValueError, match="سابقه"):
                    await delete_bot_user(session, 12)
                panel.assert_not_called()
            assert await session.scalar(select(func.count()).select_from(CustomerTelegramLink)) == 1
            assert await session.get(BotUser, 12) is not None
    asyncio.run(scenario())


def test_hourly_challenge_creation_budget_survives_unlink_and_replays() -> None:
    async def scenario():
        async with linking_database() as (sessions, integration, key, customer), sessions() as session:
            for index in range(5):
                challenge = await start_link_challenge(session, key.bearer, customer.id, idempotency_key=f"hourly-challenge-{index}")
                replay = await start_link_challenge(session, key.bearer, customer.id, idempotency_key=f"hourly-challenge-{index}")
                assert replay.challenge_id == challenge.challenge_id and replay.start_token is None
                await unlink_website_customer(session, key.bearer, customer.id, idempotency_key=f"hourly-unlink-key-{index}")
            with pytest.raises(IntegrationError) as error:
                await start_link_challenge(session, key.bearer, customer.id, idempotency_key="hourly-challenge-5")
            assert error.value.code == "queue_limit_reached"
    asyncio.run(scenario())
