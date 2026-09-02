"""Users-list quota cache + edit-modal sticky flash (v9.0.3)."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]


class QuotaCacheSyncTests(unittest.TestCase):
    def test_sync_from_info_and_kwargs(self):
        from app.services.bot_user_admin import sync_service_quota_cache

        svc = SimpleNamespace(
            quota_expire_at=None,
            quota_data_limit_bytes=None,
            quota_synced_at=None,
        )
        exp = int((datetime.now(timezone.utc) + timedelta(days=10)).timestamp())
        sync_service_quota_cache(
            svc,
            {"expire": exp, "data_limit": 5 * (1024**3)},
        )
        self.assertIsNotNone(svc.quota_synced_at)
        self.assertIsNotNone(svc.quota_expire_at)
        self.assertEqual(svc.quota_data_limit_bytes, 5 * (1024**3))

        svc2 = SimpleNamespace(
            quota_expire_at=datetime.now(timezone.utc),
            quota_data_limit_bytes=99,
            quota_synced_at=None,
        )
        sync_service_quota_cache(svc2, {"expire": 0, "data_limit": 0})
        self.assertIsNone(svc2.quota_expire_at)
        self.assertEqual(svc2.quota_data_limit_bytes, 0)
        self.assertIsNotNone(svc2.quota_synced_at)

        svc3 = SimpleNamespace(
            quota_expire_at=None,
            quota_data_limit_bytes=None,
            quota_synced_at=None,
        )
        sync_service_quota_cache(
            svc3, expire_ts=exp, data_limit_bytes=2 * (1024**3)
        )
        self.assertEqual(svc3.quota_data_limit_bytes, 2 * (1024**3))
        self.assertIsNotNone(svc3.quota_expire_at)

    def test_list_prefers_quota_cache_over_plan(self):
        from app.services.users_ops import build_user_ops_row

        now = datetime(2026, 9, 2, tzinfo=timezone.utc)
        plan = SimpleNamespace(name="ماهانه", duration_days=30, data_limit_gb=10.0)
        # Plan approx would say ~28 days left from created_at; cache says 5 days / 3 GB
        svc = SimpleNamespace(
            id=7,
            pg_username="u7",
            created_at=now - timedelta(days=2),
            plan=plan,
            notified_traffic=False,
            notified_expire=False,
            quota_synced_at=now,
            quota_expire_at=now + timedelta(days=5),
            quota_data_limit_bytes=3 * (1024**3),
        )
        user = SimpleNamespace(id=1, is_blocked=False, role="user", risk_flags=None)
        row = build_user_ops_row(user, [svc], expire_days=3, now=now)
        self.assertEqual(row.expire_text, "5 روز")
        self.assertIn("گیگ", row.volume_text)
        # Must not still show plan 10 GB as the sole volume signal when cache is 3 GB
        self.assertTrue(row.services[0].sort_volume < 5.0)

    def test_unsynced_still_uses_plan_approx(self):
        from app.services.users_ops import build_user_ops_row

        now = datetime(2026, 8, 20, tzinfo=timezone.utc)
        plan = SimpleNamespace(name="ماهانه", duration_days=30, data_limit_gb=10.0)
        svc = SimpleNamespace(
            id=7,
            pg_username="u7",
            created_at=now - timedelta(days=28),
            plan=plan,
            notified_traffic=False,
            notified_expire=False,
            quota_synced_at=None,
            quota_expire_at=None,
            quota_data_limit_bytes=None,
        )
        user = SimpleNamespace(id=1, is_blocked=False, role="user", risk_flags=None)
        row = build_user_ops_row(user, [svc], expire_days=3, now=now)
        self.assertTrue(row.expiring)
        self.assertIn("گیگ", row.volume_text)


class AdminExtendCachesQuotaTests(unittest.IsolatedAsyncioTestCase):
    async def test_extend_writes_quota_cache(self):
        from app.services.bot_user_admin import admin_extend_service

        now = datetime.now(timezone.utc)
        svc = SimpleNamespace(
            id=1,
            pg_user_id=99,
            pg_username="u99",
            subscription_url=None,
            subscription_token=None,
            notified_expire=True,
            notified_traffic=True,
            quota_expire_at=None,
            quota_data_limit_bytes=None,
            quota_synced_at=None,
            plan=None,
        )
        cur_exp = int((now + timedelta(days=2)).timestamp())
        new_exp = cur_exp + 3 * 86400
        new_lim = 12 * (1024**3)
        live = {
            "expire": new_exp,
            "data_limit": new_lim,
            "used_traffic": 0,
            "status": "active",
            "username": "u99",
            "subscription_url": "https://example/sub",
        }
        pg = MagicMock()
        pg.get_user_by_id = AsyncMock(
            return_value={
                "expire": cur_exp,
                "data_limit": 10 * (1024**3),
                "used_traffic": 0,
                "status": "active",
                "username": "u99",
            }
        )
        pg.modify_user_by_id = AsyncMock(return_value=live)
        # After modify, snapshot re-fetches live PG
        async def _get(uid):
            return live

        pg.get_user_by_id = AsyncMock(
            side_effect=[
                {
                    "expire": cur_exp,
                    "data_limit": 10 * (1024**3),
                    "used_traffic": 0,
                    "status": "active",
                    "username": "u99",
                },
                live,
            ]
        )
        pg.reset_user_by_id = AsyncMock()
        session = AsyncMock()
        session.commit = AsyncMock()

        with patch(
            "app.services.bot_user_admin._pg_client_for_service", return_value=pg
        ):
            snap = await admin_extend_service(
                session, svc, extra_days=3, extra_gb=2.0
            )

        self.assertIsNotNone(svc.quota_synced_at)
        self.assertEqual(svc.quota_data_limit_bytes, new_lim)
        self.assertIsNotNone(svc.quota_expire_at)
        self.assertFalse(svc.notified_expire)
        self.assertFalse(svc.notified_traffic)
        self.assertIsNotNone(snap)
        self.assertGreaterEqual(session.commit.await_count, 1)

    async def test_partial_modify_response_still_caches_kwargs(self):
        from app.services.bot_user_admin import admin_set_service_quota

        now = datetime.now(timezone.utc)
        svc = SimpleNamespace(
            id=2,
            pg_user_id=7,
            pg_username="x",
            subscription_url=None,
            subscription_token=None,
            notified_expire=False,
            notified_traffic=False,
            quota_expire_at=None,
            quota_data_limit_bytes=None,
            quota_synced_at=None,
            plan=None,
        )
        exp = int((now + timedelta(days=9)).timestamp())
        lim = 4 * (1024**3)
        pg = MagicMock()
        # Partial response — missing expire/data_limit
        pg.modify_user_by_id = AsyncMock(return_value={"status": "active"})
        pg.get_user_by_id = AsyncMock(
            return_value={
                "expire": exp,
                "data_limit": lim,
                "used_traffic": 0,
                "status": "active",
            }
        )
        session = AsyncMock()
        session.commit = AsyncMock()
        with patch(
            "app.services.bot_user_admin._pg_client_for_service", return_value=pg
        ):
            await admin_set_service_quota(
                session, svc, expire_ts=exp, data_limit_bytes=lim
            )
        self.assertEqual(svc.quota_data_limit_bytes, lim)
        self.assertIsNotNone(svc.quota_expire_at)
        self.assertIsNotNone(svc.quota_synced_at)


class ModalStripFlashTests(unittest.TestCase):
    def test_users_modal_strips_edit_and_flash(self):
        users = (ROOT / "app/web/templates/users.html").read_text(encoding="utf-8")
        self.assertIn('id="modal-user-edit"', users)
        self.assertIn('data-modal-strip-keys="edit,ok,err,_"', users)
        self.assertIn("data-modal-close-strip", users)
        self.assertIn("searchParams.delete('ok')", users)
        self.assertIn("searchParams.delete('err')", users)
        self.assertIn("replaceState", users)

    def test_migration_and_model_columns(self):
        mig = (
            ROOT / "alembic/versions/0024_user_service_quota_cache.py"
        ).read_text(encoding="utf-8")
        self.assertIn("quota_expire_at", mig)
        self.assertIn("quota_data_limit_bytes", mig)
        self.assertIn("quota_synced_at", mig)
        self.assertIn('down_revision: Union[str, None] = "0023_lucky_wheel"', mig)

        models = (ROOT / "app/db/models.py").read_text(encoding="utf-8")
        self.assertIn("quota_expire_at", models)
        self.assertIn("quota_data_limit_bytes", models)
        self.assertIn("quota_synced_at", models)

        session = (ROOT / "app/db/session.py").read_text(encoding="utf-8")
        self.assertIn("quota_expire_at", session)
        self.assertIn("quota_synced_at", session)


if __name__ == "__main__":
    unittest.main()
