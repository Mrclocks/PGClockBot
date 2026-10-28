"""Disabled integrations preserve the legacy checkout query sequence and panel requests."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import event, func, select

from app.db.customer_models import Customer
from app.db.models import OrderStatus
from app.services import integration_runtime, pasarguard
from app.services.integration_runtime import load_integration_runtime
from app.services.orders import pay_with_wallet
from app.services.users import clear_settings_cache, get_all_settings
from test_order_delivery_scope import OrderDeliveryScopeTests

TRACE = Path(__file__).parent / "fixtures/integration/disabled_legacy_checkout.json"


def _normalize_eager_reads(statements: list[list[Any]]) -> list[list[Any]]:
    # SQLAlchemy may load these two independent relationships in either order.
    result = list(statements)
    reversed_reads = [["SELECT", ["plans"]], ["SELECT", ["bot_users"]]]
    for index in range(len(result) - 1):
        if result[index:index + 2] == reversed_reads:
            result[index:index + 2] = list(reversed(reversed_reads))
    return result


@pytest.mark.parametrize("execution,api", [(False, False), (False, True), (True, False)])
def test_disabled_checkout_matches_legacy_query_sequence_and_panel_payload(execution: bool, api: bool) -> None:
    async def scenario() -> None:
        case = OrderDeliveryScopeTests()
        await case.asyncSetUp()
        statements: list[list[Any]] = []
        requests: list[list[Any]] = []

        async def send(method: str, path: str, **kwargs: Any) -> dict:
            requests.append([method, path, kwargs])
            return {"id": 901, "username": "new_service", "subscription_url": "https://example.invalid/sub/synthetic"}

        client = object.__new__(pasarguard.PasarGuardClient)
        client._request_unfenced = send
        case.buyer.wallet_balance = 1000
        case.order.status = OrderStatus.PENDING.value
        await case.session.commit()
        clear_settings_cache()

        @event.listens_for(case.engine.sync_engine, "before_cursor_execute")
        def trace(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool) -> None:
            statements.append([statement.split()[0], re.findall(r"\b(?:FROM|JOIN|UPDATE|INTO)\s+(\w+)", statement, re.I)])

        try:
            with (
                patch.object(integration_runtime, "_api_enabled", api),
                patch.object(integration_runtime, "_has_execution_history", False),
                patch.object(integration_runtime, "get_settings", return_value=SimpleNamespace(integration_execution_enabled=execution)),
                patch.object(pasarguard, "_pg", client),
                patch("app.services.users.get_all_settings", get_all_settings),
                patch("app.services.purchase_contact.get_all_settings", get_all_settings),
                patch("app.services.loyalty.on_order_delivered", AsyncMock()),
                patch("app.services.orders.generate_pg_username", AsyncMock(return_value="new_service")),
            ):
                result = await pay_with_wallet(case.session, case.order, case.buyer)
            assert result.status == "delivered"
            assert {"queries": _normalize_eager_reads(statements), "requests": requests} == json.loads(TRACE.read_text())
            assert len(statements) == 22 and len(requests) == 1
            assert await case.session.scalar(select(func.count()).select_from(Customer)) == 0
        finally:
            clear_settings_cache()
            await case.asyncTearDown()
    asyncio.run(scenario())


@pytest.mark.parametrize("execution,api", [(False, False), (False, True), (True, False)])
def test_disabled_native_writes_open_no_integration_session(execution: bool, api: bool) -> None:
    async def scenario() -> None:
        client = object.__new__(pasarguard.PasarGuardClient)
        send = AsyncMock(return_value={"id": 701})
        client._request_unfenced = send
        sessions = AsyncMock(side_effect=AssertionError("Disabled native writes must not open a session"))
        client._integration_sessions = sessions
        with (
            patch.object(integration_runtime, "_api_enabled", api),
            patch.object(integration_runtime, "get_settings", return_value=SimpleNamespace(integration_execution_enabled=execution)),
            patch.object(pasarguard, "_pg", client),
        ):
            pg = pasarguard.get_pg()
            await pg.create_user({"username": "synthetic"})
            await pg.modify_user_by_id(701, {"data_limit": 2048})
            await pg.delete_user_by_id(701)
        assert send.await_args_list[0].args == ("POST", "/api/user")
        assert send.await_args_list[1].args == ("PUT", "/api/user/by-id/701")
        assert send.await_args_list[1].kwargs == {"json": {"data_limit": 2048}}
        assert send.await_args_list[2].args == ("DELETE", "/api/user/by-id/701")
        assert send.await_count == 3
        sessions.assert_not_called()
    asyncio.run(scenario())


def test_execution_requires_both_controls_and_reused_clients_follow_switch_changes() -> None:
    async def scenario() -> None:
        client = object.__new__(pasarguard.PasarGuardClient)
        client._request_unfenced = AsyncMock(return_value={})
        fence = AsyncMock(return_value={})
        with (
            patch.object(integration_runtime, "_api_enabled", False),
            patch.object(integration_runtime, "get_settings", return_value=SimpleNamespace(integration_execution_enabled=True)),
            patch.object(pasarguard, "_pg", client),
            patch("app.services.integration_panel_mutations.panel_mutation", fence),
        ):
            integration_runtime.observe_api_switch("1")
            await pasarguard.get_pg().modify_user_by_id(701, {"status": "disabled"})
            fence.assert_awaited_once()
            integration_runtime.observe_api_switch("0")
            await pasarguard.get_pg().modify_user_by_id(701, {"status": "active"})
            assert fence.await_count == 1
            client._request_unfenced.assert_awaited_once()
    asyncio.run(scenario())


def test_process_disabled_startup_does_not_query_integration_setting() -> None:
    async def scenario() -> None:
        session = AsyncMock()
        with patch.object(integration_runtime, "get_settings", return_value=SimpleNamespace(integration_execution_enabled=False)):
            await load_integration_runtime(session)
        session.scalar.assert_not_awaited()
    asyncio.run(scenario())


def test_disabled_execution_worker_does_not_scan_the_database() -> None:
    from app.jobs import integration_operations

    async def scenario() -> None:
        batch = AsyncMock()
        with patch.object(integration_operations, "integration_execution_active", return_value=False), patch.object(
            integration_operations, "integration_capacity_retained", return_value=False,
        ), patch.object(integration_operations, "run_operation_batch", batch):
            await integration_operations.run_integration_operations()
        batch.assert_not_awaited()
    asyncio.run(scenario())
