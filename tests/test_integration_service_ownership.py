"""Website services count locally but cannot enter legacy commerce or delivery paths."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

from app.db.customer_models import Customer
from app.db.models import BotUser, UserService
from app.services.bot_user_admin import admin_delete_service, detach_local_services_for_pg_user, get_owned_service
from app.services.customer_backfill import backfill_legacy_customers, backfill_legacy_services
from app.services.home_overview import reseller_shop_summary
from app.services.integration_customers import CustomerInput, create_or_get_integration_customer
from app.services.service_ownership import ownership_coverage
from app.services.service_scope import customer_may_use_service, list_customer_services, page_customer_services
from app.services.volume_allocation import sum_service_bytes
from test_integration_customers import database


async def web_service(session, integration: str, bearer: str) -> UserService:
    registered = await create_or_get_integration_customer(session, bearer, CustomerInput("web-customer"))
    customer = await session.scalar(select(Customer).where(Customer.public_id == registered.customer.id))
    service = UserService(customer_id=customer.id, integration_id=integration, origin_operation_id="op_synthetic",
                          owner_principal_id=11, shop_id=11, pg_user_id=701, pg_username="synthetic-web",
                          quota_data_limit_bytes=1024)
    session.add(service)
    await session.commit()
    return service


def test_neutral_services_count_in_shop_capacity_and_reports_without_bot_identity() -> None:
    async def scenario() -> None:
        async with database() as (sessions, integration, key), sessions() as session:
            await web_service(session, integration, key.bearer)
            assert await sum_service_bytes(session, 11) == 1024
            assert await sum_service_bytes(session, 12) == 0
            assert (await reseller_shop_summary(session, 11))["services"] == 1
            assert (await reseller_shop_summary(session, 12))["services"] == 0
            assert await session.scalar(select(func.count()).select_from(BotUser)) == 1
    asyncio.run(scenario())


def test_web_services_are_not_legacy_customer_resources_or_deletion_targets() -> None:
    async def scenario() -> None:
        async with database() as (sessions, integration, key), sessions() as session:
            service = await web_service(session, integration, key.bearer)
            for user_id, shop_id in ((11, 11), (12, 11), (11, 12), (11, None)):
                assert not await customer_may_use_service(session, service, user_id, shop_id)
                assert await list_customer_services(session, user_id, shop_id) == []
            with patch("app.services.bot_user_admin._pg_client_for_bot_service", AsyncMock()) as client:
                with pytest.raises(ValueError):
                    await admin_delete_service(session, service)
                client.assert_not_called()
            assert await detach_local_services_for_pg_user(session, 701) == 0
            assert await session.get(UserService, service.id) is not None
            with pytest.raises(ValueError):
                await get_owned_service(session, service_id=service.id, bot_user_id=11, shop_id=11)
    asyncio.run(scenario())


def test_canonical_mismatch_fails_closed_and_backfill_preserves_original_evidence() -> None:
    async def scenario() -> None:
        async with database() as (sessions, integration, key), sessions() as session:
            service = UserService(bot_user_id=11, pg_username="legacy", pg_user_id=999, shop_id=11, shop_needs_review=True)
            session.add(service)
            await session.commit()
            assert not (await ownership_coverage(session)).is_complete
            await backfill_legacy_customers(session)
            await backfill_legacy_services(session)
            await session.commit()
            await session.refresh(service)
            assert (await ownership_coverage(session)).is_complete
            assert service.shop_needs_review and service.shop_id == 11 and service.pg_user_id == 999
            service.shop_needs_review = False
            await session.commit()
            assert await customer_may_use_service(session, service, 11, 11)
            wrong = Customer()
            session.add(wrong)
            await session.flush()
            service.customer_id = wrong.id
            await session.commit()
            assert not await customer_may_use_service(session, service, 11, 11)
            assert not (await ownership_coverage(session)).is_complete
    asyncio.run(scenario())


def test_explicit_legacy_block_and_unblock_synchronize_only_its_adapter() -> None:
    from app.services.customer_identity import set_legacy_customer_blocked

    async def scenario() -> None:
        async with database() as (sessions, integration, key), sessions() as session:
            user = await session.get(BotUser, 11)
            web = await web_service(session, integration, key.bearer)
            for blocked in (True, False):
                with patch("app.services.customer_identity.integration_execution_active", return_value=True):
                    await set_legacy_customer_blocked(session, user, blocked=blocked)
                await session.commit()
                state = await session.scalar(select(Customer.state).where(Customer.legacy_bot_user_id == user.id))
                assert state == ("blocked" if blocked else "active")
                assert (await session.get(Customer, web.customer_id)).state == "active"
    asyncio.run(scenario())


def test_website_customer_presentation_is_scoped_and_does_not_expose_delivery() -> None:
    from dataclasses import asdict

    from app.services.integration_customer_views import website_customer_page
    from app.services.integration_policy import IntegrationError
    from test_integration_concurrency import STAFF

    async def scenario() -> None:
        async with database() as (sessions, integration, key), sessions() as session:
            await web_service(session, integration, key.bearer)
            rows, cursor = await website_customer_page(session, STAFF, integration)
            assert len(rows) == 1 and rows[0].service_count == 1 and cursor is None
            assert set(asdict(rows[0])) == {"id", "display_name", "state", "service_count"}
            with pytest.raises(IntegrationError):
                await website_customer_page(session, STAFF, "intg_wrong")
    asyncio.run(scenario())


def test_paginated_legacy_reader_checks_canonical_owner_shop_and_customer() -> None:
    async def scenario() -> None:
        async with database() as (sessions, integration, key), sessions() as session:
            await web_service(session, integration, key.bearer)
            session.add(BotUser(id=12, telegram_id=10012, referral_code="page-other"))
            session.add_all([
                UserService(bot_user_id=11, pg_username="valid", shop_id=11),
                UserService(bot_user_id=11, pg_username="foreign-shop", shop_id=12),
                UserService(bot_user_id=12, pg_username="foreign-customer", shop_id=11),
            ])
            await session.commit()
            await backfill_legacy_customers(session)
            await backfill_legacy_services(session)
            await session.commit()
            valid = await session.scalar(select(UserService).where(UserService.pg_username == "valid"))
            rows, more = await page_customer_services(session, 11, 11, limit=1)
            assert [row.id for row in rows] == [valid.id] and not more
            wrong = Customer()
            session.add(wrong)
            await session.flush()
            valid.customer_id = wrong.id
            await session.commit()
            assert await page_customer_services(session, 11, 11, limit=1) == ([], False)
            assert await page_customer_services(session, 11, 11, limit=1, text="valid") == ([], False)
    asyncio.run(scenario())
