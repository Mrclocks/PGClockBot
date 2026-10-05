"""Regression: reseller bot group list must match web when allow-list is open.

Web panel uses filter_groups_for_staff default (trust_pg_list_scope → True for
shop resellers). Bot catalog previously hardcoded trust_client_scope=False, so
a reseller whose PG role has allowed_group_ids=None saw groups in web but
«گروهی نیست» in the bot.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.services.bot_pg_catalog_authz import BotPgCatalogGate, list_scoped_pg_catalog
from app.services.pg_read import trust_pg_list_scope
from app.services.plans_catalog import filter_groups_for_staff, filter_templates_for_staff


ROOT = Path(__file__).resolve().parents[1]


class ResellerBotGroupsWebParityTests(unittest.TestCase):
    def test_reseller_open_allow_list_keeps_own_client_groups(self):
        staff = {
            "role": "reseller",
            "bot_user_id": 42,
            "pg_access": {},  # allowed_group_ids none → open on own client
        }
        self.assertTrue(trust_pg_list_scope(staff))
        items = [{"id": 1, "name": "g1"}, {"id": 2, "name": "g2"}]
        self.assertEqual(filter_groups_for_staff(items, staff), items)
        self.assertEqual(filter_templates_for_staff([{"id": 9}], staff), [{"id": 9}])

    def test_pg_staff_without_credentials_still_fail_closed(self):
        staff = {"role": "pg_staff", "pg_access": {}, "pg_credentials_ready": False}
        self.assertFalse(trust_pg_list_scope(staff))
        self.assertEqual(filter_groups_for_staff([{"id": 1}], staff), [])

    def test_list_scoped_source_no_longer_hardcodes_false(self):
        src = (ROOT / "app/services/bot_pg_catalog_authz.py").read_text(encoding="utf-8")
        block = src.split("async def list_scoped_pg_catalog", 1)[1].split(
            "\nasync def ", 1
        )[0]
        self.assertNotIn("trust_client_scope=False", block)
        self.assertIn("filter_groups_for_staff(items, gate.staff)", block)
        handler = (ROOT / "app/bot/handlers/admin_pg_users.py").read_text(encoding="utf-8")
        filt = handler.split("def _filter_staff_groups", 1)[1].split("\n\n", 1)[0]
        self.assertNotIn("trust_client_scope=False", filt)


class ListScopedCatalogTrustTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_groups_keeps_items_for_reseller_open_access(self):
        pg = SimpleNamespace(
            get_groups=AsyncMock(
                return_value=[{"id": 7, "name": "shop-group"}, {"id": 8, "name": "g2"}]
            ),
            get_groups_simple=AsyncMock(return_value=[]),
        )
        gate = BotPgCatalogGate(
            allowed=True,
            reason="ok",
            user_message="",
            resolution=None,
            staff={
                "role": "reseller",
                "bot_user_id": 99,
                "pg_access": {},
            },
            pg_object=None,
            pg_client=pg,
            kind="groups",
            as_owner_client=False,
        )
        rows = await list_scoped_pg_catalog(gate, kind="groups")
        self.assertEqual({int(r["id"]) for r in rows}, {7, 8})
        pg.get_groups.assert_awaited()

    async def test_list_groups_empty_full_falls_back_to_simple(self):
        pg = SimpleNamespace(
            get_groups=AsyncMock(return_value=[]),
            get_groups_simple=AsyncMock(return_value=[{"id": 3, "name": "simple"}]),
        )
        gate = BotPgCatalogGate(
            allowed=True,
            reason="ok",
            user_message="",
            resolution=None,
            staff={"role": "reseller", "bot_user_id": 5, "pg_access": {}},
            pg_object=None,
            pg_client=pg,
            kind="groups",
            as_owner_client=False,
        )
        rows = await list_scoped_pg_catalog(gate, kind="groups")
        self.assertEqual([int(r["id"]) for r in rows], [3])
        pg.get_groups_simple.assert_awaited()

    async def test_list_groups_respects_explicit_allow_list(self):
        pg = SimpleNamespace(
            get_groups=AsyncMock(
                return_value=[{"id": 1}, {"id": 2}, {"id": 3}]
            ),
            get_groups_simple=AsyncMock(return_value=[]),
        )
        gate = BotPgCatalogGate(
            allowed=True,
            reason="ok",
            user_message="",
            resolution=None,
            staff={
                "role": "reseller",
                "bot_user_id": 5,
                "pg_access": {"allowed_group_ids": [2]},
            },
            pg_object=None,
            pg_client=pg,
            kind="groups",
            as_owner_client=False,
        )
        rows = await list_scoped_pg_catalog(gate, kind="groups")
        self.assertEqual([int(r["id"]) for r in rows], [2])


if __name__ == "__main__":
    unittest.main()
