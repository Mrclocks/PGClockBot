"""PG admin web-panel gate: disabled/deleted vs limited/quota."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


class ClassifyPgAdminTests(unittest.TestCase):
    def test_missing(self):
        from app.services.pg_staff_access import classify_pg_admin_dict

        self.assertEqual(classify_pg_admin_dict(None), "missing")
        self.assertEqual(classify_pg_admin_dict({}), "missing")

    def test_disabled_variants(self):
        from app.services.pg_staff_access import classify_pg_admin_dict

        self.assertEqual(classify_pg_admin_dict({"username": "a", "enabled": False}), "disabled")
        self.assertEqual(classify_pg_admin_dict({"username": "a", "is_disabled": True}), "disabled")
        self.assertEqual(classify_pg_admin_dict({"username": "a", "is_active": False}), "disabled")
        self.assertEqual(classify_pg_admin_dict({"username": "a", "status": "disabled"}), "disabled")
        self.assertEqual(classify_pg_admin_dict({"username": "a", "status": "inactive"}), "disabled")

    def test_limited_and_exhausted_still_ok(self):
        from app.services.pg_staff_access import classify_pg_admin_dict

        self.assertEqual(classify_pg_admin_dict({"username": "a", "status": "limited"}), "ok")
        self.assertEqual(classify_pg_admin_dict({"username": "a", "status": "expired"}), "ok")
        self.assertEqual(classify_pg_admin_dict({"username": "a", "is_limited": True}), "ok")
        self.assertEqual(
            classify_pg_admin_dict(
                {"username": "a", "status": "active", "used_traffic": 99, "data_limit": 99}
            ),
            "ok",
        )

    def test_active_ok(self):
        from app.services.pg_staff_access import classify_pg_admin_dict

        self.assertEqual(classify_pg_admin_dict({"username": "a", "status": "active"}), "ok")
        self.assertEqual(classify_pg_admin_dict({"username": "a"}), "ok")


class EnforceGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_blocks_without_revoke(self):
        from app.services.pg_staff_access import (
            PG_ACCESS_DENIED_MSG,
            enforce_pg_admin_web_gate,
        )

        session = AsyncMock()
        with (
            patch(
                "app.services.pg_staff_access.fetch_pg_admin_gate",
                new=AsyncMock(return_value=("disabled", {"status": "disabled"})),
            ),
            patch(
                "app.services.pg_staff_access.revoke_web_access",
                new=AsyncMock(),
            ) as revoke,
        ):
            ok, msg = await enforce_pg_admin_web_gate(session, "pg1")
        self.assertFalse(ok)
        self.assertEqual(msg, PG_ACCESS_DENIED_MSG)
        revoke.assert_not_awaited()

    async def test_missing_revokes_staff_row(self):
        from app.services.pg_staff_access import (
            PG_ACCESS_DENIED_MSG,
            enforce_pg_admin_web_gate,
        )

        session = AsyncMock()
        with (
            patch(
                "app.services.pg_staff_access.fetch_pg_admin_gate",
                new=AsyncMock(return_value=("missing", None)),
            ),
            patch(
                "app.services.pg_staff_access.revoke_web_access",
                new=AsyncMock(return_value=True),
            ) as revoke,
        ):
            ok, msg = await enforce_pg_admin_web_gate(session, "gone")
        self.assertFalse(ok)
        self.assertEqual(msg, PG_ACCESS_DENIED_MSG)
        revoke.assert_awaited_once()

    async def test_unreachable_denies_without_revoke(self):
        from app.services.pg_staff_access import (
            PG_UNAVAILABLE_MSG,
            enforce_pg_admin_web_gate,
        )

        with patch(
            "app.services.pg_staff_access.fetch_pg_admin_gate",
            new=AsyncMock(return_value=("unreachable", None)),
        ), patch(
            "app.services.pg_staff_access.revoke_web_access", new=AsyncMock()
        ) as revoke:
            ok, msg = await enforce_pg_admin_web_gate(
                AsyncMock(), f"unreachable_user_{id(self)}"
            )
        self.assertFalse(ok)
        self.assertEqual(msg, PG_UNAVAILABLE_MSG)
        revoke.assert_not_awaited()

    async def test_ok_allows(self):
        from app.services.pg_staff_access import enforce_pg_admin_web_gate

        with patch(
            "app.services.pg_staff_access.fetch_pg_admin_gate",
            new=AsyncMock(return_value=("ok", {"status": "active"})),
        ):
            ok, msg = await enforce_pg_admin_web_gate(
                AsyncMock(), f"ok_user_{id(self)}"
            )
        self.assertTrue(ok)
        self.assertIsNone(msg)


class PurgeOrphanTests(unittest.IsolatedAsyncioTestCase):
    async def test_purge_deletes_missing_admins(self):
        from app.services.pg_staff_access import purge_orphaned_staff_access

        keep = SimpleNamespace(id=1, pg_username="alive")
        drop = SimpleNamespace(id=2, pg_username="dead")
        session = AsyncMock()
        with (
            patch(
                "app.services.pg_staff_access.list_access_rows",
                new=AsyncMock(return_value=[keep, drop]),
            ),
            patch("app.services.pasarguard.get_pg") as get_pg,
            patch(
                "app.services.pg_staff_access._detach_pg_staff_fk_deps",
                new=AsyncMock(),
            ) as detach,
        ):
            get_pg.return_value.get_admins = AsyncMock(
                return_value=[{"username": "alive"}]
            )
            n = await purge_orphaned_staff_access(session)
        self.assertEqual(n, 1)
        detach.assert_awaited_once_with(session, 2)
        session.delete.assert_awaited()
        session.commit.assert_awaited()


class LoginWiringTests(unittest.TestCase):
    def test_login_uses_access_denied_message(self):
        from pathlib import Path

        from app.services.pg_staff_access import PG_ACCESS_DENIED_MSG

        src = Path("app/api/app.py").read_text(encoding="utf-8")
        self.assertIn("PG_ACCESS_DENIED_MSG", src)
        self.assertIn("enforce_pg_admin_web_gate", src)
        self.assertIn("login?err=", src)
        self.assertTrue(PG_ACCESS_DENIED_MSG)


if __name__ == "__main__":
    unittest.main()
