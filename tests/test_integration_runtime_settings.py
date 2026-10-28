"""Only committed platform switch writes change native integration activation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import select

from app.db.models import Setting
from app.services import integration_runtime
from app.services.users import clear_settings_cache, set_setting, set_settings_bulk
from test_integration_customers import database


def test_platform_switch_updates_activation_without_inheriting_tenant_overrides() -> None:
    async def scenario() -> None:
        async with database() as (sessions, integration, key), sessions() as session:
            with patch.object(integration_runtime, "_api_enabled", False), patch.object(
                integration_runtime, "get_settings", return_value=SimpleNamespace(integration_execution_enabled=True),
            ):
                await set_setting(session, "integration_api_enabled", "1", reseller_id=0)
                assert integration_runtime.integration_execution_active()
                await set_setting(session, "shop_title", "آزمایش", reseller_id=0)
                assert integration_runtime.integration_execution_active()
                await set_setting(session, "integration_api_enabled", "0", reseller_id=11)
                assert integration_runtime.integration_execution_active()
                assert await session.scalar(select(Setting.value).where(Setting.key == "integration_api_enabled")) == "1"
                await set_settings_bulk(session, {"integration_api_enabled": "0"}, reseller_id=0)
                assert not integration_runtime.integration_execution_active()
        clear_settings_cache()
    asyncio.run(scenario())
