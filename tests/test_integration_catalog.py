"""Catalog terms are technical, own-principal scoped, reproducible and price-free."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db.integration_read_models import IntegrationCatalogPlan
from app.db.models import Plan
from app.services.integration_catalog import CatalogSelection, list_integration_plans, materialize_plan
from app.services.integration_pagination import IntegrationCursors
from app.services.integration_policy import IntegrationError
from test_integration_service_reads import SECRET, read_database


def test_catalog_handles_stay_stable_when_terms_change_and_never_include_price():
    async def scenario():
        async with read_database() as (sessions, integration, key, first, second, plan_id, access), sessions() as session:
            client = AsyncMock()
            async def groups():
                assert not session.in_transaction()
                return [{"id": 1}]
            client.get_groups_simple.side_effect = groups
            client.request.return_value = {"username": "pg-race", "role": {"id": 1, "permissions": {
                "users": {"read": True}, "groups": {"read": True}}, "access": {"allowed_group_ids": [1]}}}
            with patch("app.services.integration_catalog.integration_pg_client", AsyncMock(return_value=client)):
                page = await list_integration_plans(session, access, CatalogSelection(), IntegrationCursors(SECRET))
                assert len(page.items) == 1
                view = page.items[0]
                assert set(asdict(view)) == {"id", "name", "technical_terms", "can_provision", "can_renew"}
                assert "price" not in str(asdict(view)) and view.id.startswith("pln_")
                assert view.technical_terms.data_limit_bytes == 1024**3
                plan = await session.get(Plan, plan_id)
                plan.duration_days = 60
                await session.commit()
                updated = await list_integration_plans(session, access, CatalogSelection(), IntegrationCursors(SECRET))
            assert updated.items[0].id == view.id
            assert updated.items[0].technical_terms.terms_revision != view.technical_terms.terms_revision
            row = (await session.scalars(select(IntegrationCatalogPlan))).one()
            assert row.terms_revision == updated.items[0].technical_terms.terms_revision
    asyncio.run(scenario())


@pytest.mark.parametrize("field,value", [("data_limit_gb", float("nan")), ("data_limit_gb", True),
    ("duration_days", True), ("duration_days", -1), ("pg_group_ids", "1,bad"),
    ("pg_template_id", 1), ("pg_username_pattern", "{username}"), ("pg_username_prefix", "<hostile>")])
def test_unsupported_or_malformed_plan_semantics_are_never_materialized(field, value):
    plan = Plan(id=1, name="synthetic", price=1, duration_days=30, data_limit_gb=1, pg_group_ids="1")
    setattr(plan, field, value)
    with pytest.raises(IntegrationError) as error:
        materialize_plan(plan, {}, frozenset({1}))
    assert error.value.code == "unsupported_terms"
