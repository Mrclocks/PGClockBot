"""v0.2.16 — delivery MissingGreenlet + Mini App services list resilience.

Regression for:
1. First approve/delivery crash when PG returns on_hold without expire_duration
   and sync_service_quota_cache touched ``service.plan`` under AsyncSession.
2. Mini App hiding owned services when subscription_token was null but URL
   still had /sub/…, or when PG enrich failed entirely.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]


class SyncQuotaNoLazyPlanTests(unittest.TestCase):
    def test_on_hold_without_duration_does_not_touch_plan_descriptor(self):
        from app.services.bot_user_admin import sync_service_quota_cache

        class _Trap(dict):
            """Simulate ORM __dict__ without a loaded plan relationship."""

            def get(self, key, default=None):  # noqa: A003
                if key == "plan":
                    return None
                return super().get(key, default)

        class _Svc:
            def __init__(self):
                self.__dict__ = _Trap(
                    quota_expire_at=None,
                    quota_data_limit_bytes=None,
                    quota_synced_at=None,
                )

            @property
            def plan(self):  # pragma: no cover - must never be reached
                raise RuntimeError("greenlet_spawn has not been called")

        svc = _Svc()
        # Would previously crash via getattr(service, "plan")
        sync_service_quota_cache(
            svc,
            {"status": "on_hold", "expire": 0, "data_limit": 0},
            fallback_duration_days=30,
        )
        self.assertIsNotNone(svc.__dict__["quota_expire_at"])
        delta = svc.__dict__["quota_expire_at"] - datetime.now(timezone.utc)
        self.assertGreater(delta.total_seconds(), 29 * 86400)
        self.assertLess(delta.total_seconds(), 31 * 86400)

    def test_source_never_getattr_plan(self):
        src = (ROOT / "app/services/bot_user_admin.py").read_text(encoding="utf-8")
        # Narrow to sync_service_quota_cache body
        start = src.index("def sync_service_quota_cache")
        end = src.index("\nasync def admin_set_service_quota", start)
        body = src[start:end]
        self.assertNotIn('getattr(service, "plan"', body)
        self.assertIn('service.__dict__.get("plan")', body)
        self.assertIn("fallback_duration_days", body)


class DeliverOrderNamingAndQuotaTests(unittest.TestCase):
    def test_deliver_uses_order_id_for_naming_and_passes_plan_days(self):
        src = (ROOT / "app/services/orders.py").read_text(encoding="utf-8")
        self.assertIn("user_id=int(order.id)", src)
        self.assertIn("fallback_duration_days=plan_days", src)


class MiniAppServicesResilienceTests(unittest.IsolatedAsyncioTestCase):
    def test_service_sub_token_falls_back_to_url(self):
        from app.api.miniapp_pages import _service_sub_token

        svc = SimpleNamespace(
            subscription_token=None,
            subscription_url="https://pg.example/sub/tokABC",
        )
        self.assertEqual(_service_sub_token(svc), "tokABC")
        svc2 = SimpleNamespace(
            subscription_token="  stored  ",
            subscription_url="https://pg.example/sub/other",
        )
        self.assertEqual(_service_sub_token(svc2), "stored")

    async def test_enrich_returns_rows_when_pg_fails(self):
        from app.api.miniapp_pages import _enrich_services

        svc = SimpleNamespace(
            id=7,
            pg_username="u7",
            subscription_url="https://pg.example/sub/abc",
            subscription_token=None,
            plan_id=1,
        )
        with patch(
            "app.api.miniapp_pages._fetch_pg_info",
            new=AsyncMock(return_value={"error": "upstream_unavailable"}),
        ):
            out = await _enrich_services([svc])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["id"], 7)
        self.assertEqual(out[0]["username"], "u7")
        self.assertEqual(out[0]["error"], "upstream_unavailable")

    async def test_enrich_survives_serialize_exception(self):
        from app.api.miniapp_pages import _enrich_services

        svc = SimpleNamespace(
            id=9,
            pg_username="u9",
            subscription_url="",
            subscription_token="t",
            plan_id=None,
        )
        with patch(
            "app.api.miniapp_pages._fetch_pg_info",
            new=AsyncMock(return_value={"status": "active", "expire": 0}),
        ), patch(
            "app.api.miniapp_pages._serialize_service",
            side_effect=RuntimeError("boom"),
        ):
            out = await _enrich_services([svc])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["id"], 9)
        self.assertEqual(out[0]["username"], "u9")

    def test_serialize_passes_on_hold_to_expire_helpers(self):
        from app.api.miniapp_pages import _serialize_service

        svc = SimpleNamespace(
            id=1,
            pg_username="hold1",
            subscription_url="https://pg.example/sub/x",
            plan_id=2,
        )
        out = _serialize_service(
            svc,
            {
                "status": "on_hold",
                "expire": 0,
                "expire_duration": 10 * 86400,
                "used_traffic": 0,
                "data_limit": 5 * (1024**3),
            },
        )
        self.assertEqual(out["status"], "on_hold")
        self.assertIsNotNone(out["expire_days"])
        self.assertGreaterEqual(int(out["expire_days"]), 10)
        self.assertNotEqual(out["expire"], "—")
        self.assertIn("انتظار", out["expire"])
        self.assertTrue(out["pending_start"])
        self.assertNotIn("نامحدود", out["expire_days_label"])
        self.assertIn("روز", out["expire_days_label"])

    def test_serialize_on_hold_without_duration_not_unlimited(self):
        from app.api.miniapp_pages import _serialize_service

        svc = SimpleNamespace(
            id=2,
            pg_username="hold2",
            subscription_url="https://pg.example/sub/y",
            plan_id=3,
        )
        out = _serialize_service(
            svc,
            {
                "status": "on_hold",
                "expire": 0,
                "used_traffic": 0,
                "data_limit": 0,
            },
        )
        self.assertIsNone(out["expire_days"])
        self.assertTrue(out["pending_start"])
        self.assertEqual(out["expire_days_label"], "پس از اتصال")
        self.assertNotEqual(out["expire_days_label"], "نامحدود")


class OnHoldDisplayLabelTests(unittest.TestCase):
    def test_time_remaining_label_on_hold(self):
        from app.services.formatting import pg_expire_fields, time_remaining_label

        self.assertEqual(
            time_remaining_label(days_left=None, status="on_hold"),
            "پس از اتصال",
        )
        self.assertEqual(
            time_remaining_label(days_left=12, status="on_hold"),
            "12 روز (پس از اتصال)",
        )
        self.assertEqual(
            time_remaining_label(days_left=None, status="active"),
            "نامحدود",
        )
        fields = pg_expire_fields(
            {
                "status": "on_hold",
                "expire": 0,
                "expire_duration": 7 * 86400,
            }
        )
        self.assertEqual(fields["days_left"], 7)
        self.assertTrue(fields["pending_start"])
        self.assertNotIn("نامحدود", fields["time_label"])
        self.assertNotIn("نامحدود", fields["expire_text"])

    def test_snapshot_time_label_on_hold(self):
        from app.services.bot_user_admin import ServiceSnapshot, snapshot_telegram_lines

        svc = SimpleNamespace(id=3, plan=SimpleNamespace(name="پلن"))
        snap = ServiceSnapshot(
            service=svc,
            pg={"status": "on_hold", "expire": 0},
            status_fa="در انتظار",
            used_text="0",
            limit_text="—",
            volume_text="—",
            remain_gb_text="—",
            days_left=None,
            expire_text="در انتظار",
            subscription_url=None,
            error=None,
        )
        self.assertEqual(snap.time_label, "پس از اتصال")
        text = snapshot_telegram_lines(snap)
        self.assertIn("پس از اتصال", text)
        self.assertNotIn("نامحدود", text)

    def test_ui_sources_not_map_null_days_to_unlimited(self):
        # Mini App label: tests/miniapp_app.test.cjs (on_hold never reads «نامحدود»).
        html = (ROOT / "app/web/templates/_user_edit_body.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("s.time_label", html)
        self.assertNotIn(
            "s.days_left is none %}نامحدود",
            html,
        )
        svc_src = (ROOT / "app/bot/handlers/services.py").read_text(encoding="utf-8")
        self.assertIn("fetch_live_service_info", svc_src)
        self.assertIn("service_card(sub_info)", svc_src)
        pg_src = (ROOT / "app/api/pg_pages.py").read_text(encoding="utf-8")
        self.assertIn("pg_expire_fields", pg_src)


if __name__ == "__main__":
    unittest.main()
