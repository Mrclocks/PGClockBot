"""Isolated PostgreSQL schemas shared by monitor behavior tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from app.db.models import BotUser, OrgPrincipal
from app.services.authz import authz_from_staff
from app.services.connection_monitor_access import MonitorAccess
from app.services.connection_monitor_subscription import subscription_entries
from tests.postgres_support import PostgresTestCase


OWNER_STAFF = {
    "role": "admin", "org_principal_id": 1, "org_depth": 0,
    "org_parent_id": None, "org_status": "active",
}


def subscription_fixture_key(index: int = 0) -> str:
    fixture = json.loads((Path(__file__).parent / "fixtures/connection_monitor_catalog.json").read_text())
    return subscription_entries(fixture["native_configs"])[index].config_key


def monitor_access(**changes: object) -> MonitorAccess:
    return MonitorAccess(authz_from_staff({**OWNER_STAFF, **changes}), True)


class MonitorDatabaseCase(PostgresTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.now = datetime.now(timezone.utc)
        self.access = monitor_access()
        async with self.Session() as session:
            session.add_all([
                BotUser(id=1, telegram_id=101, referral_code="owner", role="admin"),
                BotUser(id=2, telegram_id=102, referral_code="shop2", role="reseller"),
                BotUser(id=3, telegram_id=103, referral_code="shop3", role="reseller"),
                BotUser(id=4, telegram_id=104, referral_code="customer1", reseller_id=2),
                BotUser(id=5, telegram_id=105, referral_code="customer2", reseller_id=3),
                OrgPrincipal(id=1, depth=0, status="active"),
                OrgPrincipal(id=2, parent_id=1, depth=1, status="active"),
            ])
            await session.commit()

