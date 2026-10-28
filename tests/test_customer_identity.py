"""Legacy adapter backfill and real legacy creation remain atomic and repeat-safe."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.customer_models import Customer
from app.db.models import BotUser, UserService
from app.services.customer_backfill import IdentityBatch, backfill_legacy_customers, backfill_legacy_services
from app.services.customer_identity import ensure_legacy_customer
from app.services.bot_user_admin import admin_create_bot_user
from app.services.resellers import get_or_create_pg_linked_bot_user
from app.services.users import get_or_create_user
from test_integration_customers import database


@pytest.fixture(autouse=True)
def active_legacy_adapters() -> Any:
    with patch("app.services.customer_identity.integration_execution_active", return_value=True):
        yield


def test_keyset_backfill_is_bounded_resumable_repeat_safe_and_preserves_legacy_data() -> None:
    async def scenario() -> None:
        async with database() as (sessions, integration, key), sessions() as session:
            session.add_all([BotUser(id=n, telegram_id=20000+n, referral_code=f"legacy-{n}",
                                    is_blocked=n == 100, wallet_balance=n, role="admin" if n == 101 else "user")
                             for n in range(100, 602)])
            await session.flush()
            session.add_all([UserService(id=n, bot_user_id=n, pg_user_id=n, pg_username=f"pg-{n}",
                                        shop_id=11 if n % 2 else None, shop_needs_review=n == 102,
                                        subscription_token=f"synthetic-{n}") for n in range(100, 602)])
            await session.flush()
            await session.execute(update(UserService).values(public_id=None))
            await session.commit()
            before = list((await session.execute(select(UserService.id, UserService.bot_user_id, UserService.shop_id,
                UserService.shop_needs_review, UserService.pg_user_id, UserService.subscription_token))).all())
            queries = []
            @event.listens_for(session.bind.sync_engine, "before_cursor_execute")
            def count_queries(conn: Any, cursor: Any, statement: str, params: Any, context: Any, many: bool) -> None:
                queries.append(statement)
            first = await backfill_legacy_customers(session)
            assert first.scanned == 500
            assert len(queries) <= 7
            await session.commit()
            first_ids = dict((await session.execute(select(Customer.legacy_bot_user_id, Customer.public_id))).all())
            interrupted = await backfill_legacy_customers(session, after=first.last_id)
            assert interrupted.scanned == 3
            await session.rollback()
            assert await session.scalar(select(func.count()).select_from(Customer)) == 500
            second = await backfill_legacy_customers(session, after=first.last_id)
            await session.commit()
            assert second.scanned == 3
            assert (await backfill_legacy_customers(session, after=second.last_id)).scanned == 0
            await backfill_legacy_customers(session)
            await backfill_legacy_customers(session, after=first.last_id)
            await session.commit()
            after_ids = dict((await session.execute(select(Customer.legacy_bot_user_id, Customer.public_id))).all())
            assert all(after_ids[user] == public for user, public in first_ids.items())
            assert len(after_ids) == 503
            assert await session.scalar(select(Customer.state).where(Customer.legacy_bot_user_id == 100)) == "blocked"
            assert await session.scalar(select(Customer.id).where(Customer.legacy_bot_user_id == 101))
            first_services = await backfill_legacy_services(session)
            await session.commit()
            assert first_services.scanned == 500
            await backfill_legacy_services(session, after=first_services.last_id)
            await session.commit()
            service_ids = list((await session.execute(select(UserService.id, UserService.public_id, UserService.customer_id))).all())
            await backfill_legacy_services(session)
            await backfill_legacy_services(session, after=first_services.last_id)
            await session.commit()
            assert service_ids == list((await session.execute(select(UserService.id, UserService.public_id, UserService.customer_id))).all())
            assert before == list((await session.execute(select(UserService.id, UserService.bot_user_id, UserService.shop_id,
                UserService.shop_needs_review, UserService.pg_user_id, UserService.subscription_token))).all())
            assert await session.scalar(select(BotUser.wallet_balance).where(BotUser.id == 100)) == 100
    asyncio.run(scenario())


def test_backfill_never_reactivates_customer_and_preserves_service_ids_on_interruption() -> None:
    async def scenario() -> None:
        async with database() as (sessions, integration, key), sessions() as session:
            customer_id = await ensure_legacy_customer(session, 11)
            await session.execute(update(Customer).values(state="blocked"))
            session.add(UserService(bot_user_id=11, pg_username="legacy"))
            await session.flush()
            await session.execute(update(UserService).values(public_id=None))
            await session.commit()
            await ensure_legacy_customer(session, 11)
            await backfill_legacy_services(session)
            await session.rollback()
            await backfill_legacy_services(session)
            await session.commit()
            assert await session.scalar(select(Customer.state)) == "blocked"
            row = (await session.execute(select(UserService.customer_id, UserService.public_id))).one()
            assert row.customer_id == customer_id and row.public_id.startswith("svc_")
    asyncio.run(scenario())


@pytest.mark.parametrize("mismatch", [False, True])
def test_backfill_stops_entire_batch_on_missing_or_conflicting_owner(mismatch: bool) -> None:
    async def scenario() -> None:
        async with database() as (sessions, integration, key), sessions() as session:
            if mismatch:
                await ensure_legacy_customer(session, 11)
                wrong = Customer()
                session.add(wrong)
                await session.flush()
                wrong_id = wrong.id
            else:
                wrong_id = None
            session.add(UserService(bot_user_id=11, pg_username="legacy", customer_id=wrong_id))
            await session.commit()
            with pytest.raises(ValueError, match="needs review"):
                await backfill_legacy_services(session)
            await session.rollback()
            assert await session.scalar(select(UserService.customer_id)) == wrong_id
    asyncio.run(scenario())


@pytest.mark.parametrize("operation", [backfill_legacy_customers, backfill_legacy_services])
@pytest.mark.parametrize("arguments", [{"limit": 501}, {"limit": 0}, {"limit": True}, {"after": -1}, {"after": True}, {"after": 2**31}])
def test_backfill_rejects_invalid_batches(
    operation: Callable[..., Awaitable[IdentityBatch]], arguments: dict[str, int],
) -> None:
    async def scenario() -> None:
        async with database() as (sessions, integration, key), sessions() as session:
            with pytest.raises(ValueError):
                await operation(session, **arguments)
    asyncio.run(scenario())


@pytest.mark.parametrize("entry", ["telegram", "manual", "admin_compatibility"])
def test_real_legacy_user_creation_dual_writes_one_matching_customer(entry: str) -> None:
    async def scenario() -> None:
        async with database() as (sessions, integration, key), sessions() as session:
            if entry == "telegram":
                with patch("app.services.users.get_all_settings", AsyncMock(return_value={})), patch(
                    "app.services.users.get_settings") as settings:
                    settings.return_value.admin_ids = []
                    user = await get_or_create_user(session, 99001)
                    again = await get_or_create_user(session, 99001)
                    assert user.id == again.id
            elif entry == "manual":
                user = await admin_create_bot_user(session, telegram_id=99001)
            else:
                user = await get_or_create_pg_linked_bot_user(session, pg_username="synthetic-admin")
                await session.commit()
            customer = (await session.scalars(select(Customer))).one()
            assert customer.legacy_bot_user_id == user.id
            assert customer.state == "active"
    asyncio.run(scenario())


@pytest.mark.parametrize("entry", ["telegram", "manual", "admin_compatibility"])
def test_legacy_adapter_failure_rolls_back_user_and_customer(entry: str) -> None:
    async def scenario() -> None:
        async with database() as (sessions, integration, key), sessions() as session:
            async def fail_adapter(session: AsyncSession, user_id: int) -> None:
                await ensure_legacy_customer(session, user_id)
                raise RuntimeError("synthetic identity failure")
            with patch("app.services.customer_identity.ensure_legacy_customer", side_effect=fail_adapter), pytest.raises(RuntimeError):
                if entry == "telegram":
                    with patch("app.services.users.get_all_settings", AsyncMock(return_value={})), patch(
                        "app.services.users.get_settings") as settings:
                        settings.return_value.admin_ids = []
                        await get_or_create_user(session, 99001)
                elif entry == "manual":
                    await admin_create_bot_user(session, telegram_id=99001)
                else:
                    await get_or_create_pg_linked_bot_user(session, pg_username="synthetic-admin")
            await session.rollback()
            assert await session.scalar(select(func.count()).select_from(BotUser)) == 1
            assert await session.scalar(select(func.count()).select_from(Customer)) == 0
    asyncio.run(scenario())
