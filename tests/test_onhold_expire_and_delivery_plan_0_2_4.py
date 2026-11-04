"""v0.2.4 — on_hold ≠ unlimited; delivery shows plan type; category modal scroll."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]


class OnHoldExpireTests(unittest.TestCase):
    def test_format_expire_on_hold_uses_duration(self):
        from app.services.formatting import (
            expire_remaining_days,
            format_expire,
            format_expire_short,
            service_card,
        )

        self.assertIn(
            "پس از اتصال",
            format_expire(0, status="on_hold", expire_duration=30 * 86400),
        )
        self.assertNotIn(
            "نامحدود",
            format_expire(0, status="on_hold", expire_duration=30 * 86400),
        )
        self.assertIn("در انتظار", format_expire_short(0, status="on_hold", expire_duration=7 * 86400))
        self.assertEqual(
            expire_remaining_days(0, status="on_hold", expire_duration=30 * 86400),
            30,
        )
        card = service_card(
            {
                "username": "u1",
                "status": "on_hold",
                "used_traffic": 0,
                "data_limit": 10 * (1024**3),
                "expire": 0,
                "expire_duration": 15 * 86400,
            }
        )
        self.assertIn("پس از اتصال", card)
        self.assertNotIn("نامحدود", card.split("انقضا")[1].split("\n")[0])


class OnHoldAddonApplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_apply_duration_on_hold_uses_expire_duration(self):
        from app.services.service_addons import KIND_DURATION, _apply_pack_to_service

        svc = SimpleNamespace(
            id=1,
            pg_user_id=9,
            plan=SimpleNamespace(duration_days=30),
            quota_expire_at=None,
            quota_data_limit_bytes=None,
            quota_synced_at=None,
        )
        pg = MagicMock()
        pg.get_user_by_id = AsyncMock(
            return_value={
                "status": "on_hold",
                "expire": 0,
                "expire_duration": 10 * 86400,
                "data_limit": 5 * (1024**3),
            }
        )
        pg.modify_user_by_id = AsyncMock(return_value={})
        session = AsyncMock()

        with patch("app.services.pasarguard.get_pg", return_value=pg), patch(
            "app.services.users.current_shop_reseller_id", return_value=None
        ):
            await _apply_pack_to_service(
                session, svc, kind=KIND_DURATION, amount=5, order_reseller_id=None
            )

        payload = pg.modify_user_by_id.await_args.args[1]
        self.assertIn("expire", payload)
        exp = int(payload["expire"])
        import time

        # ~15 days from now (10 hold + 5 addon)
        self.assertGreater(exp, int(time.time()) + 14 * 86400)
        self.assertLess(exp, int(time.time()) + 16 * 86400)

    def test_parse_expire_zero_is_unset(self):
        from app.services.formatting import parse_expire

        self.assertIsNone(parse_expire(0))
        self.assertIsNone(parse_expire(-1))

    def test_parse_expire_accepts_datetime_for_stored_quota(self):
        from datetime import datetime, timedelta, timezone

        from app.services.formatting import format_expire_short, parse_expire

        aware = datetime.now(timezone.utc) + timedelta(days=5)
        naive = datetime.utcnow() + timedelta(days=5)
        self.assertEqual(parse_expire(aware), aware.astimezone(timezone.utc))
        self.assertEqual(parse_expire(naive).tzinfo, timezone.utc)
        self.assertIn("روز", format_expire_short(aware))
        self.assertNotEqual(format_expire_short(aware), "—")

    def test_sync_on_hold_does_not_mark_unlimited(self):
        from app.services.bot_user_admin import sync_service_quota_cache

        svc = SimpleNamespace(
            quota_expire_at=None,
            quota_data_limit_bytes=None,
            quota_synced_at=None,
            plan=SimpleNamespace(duration_days=30),
        )
        sync_service_quota_cache(
            svc,
            {
                "status": "on_hold",
                "expire": 0,
                "expire_duration": 30 * 86400,
                "data_limit": 5 * (1024**3),
            },
        )
        self.assertIsNotNone(svc.quota_expire_at)
        self.assertIsNotNone(svc.quota_synced_at)
        # ~30 days from now
        delta = svc.quota_expire_at - datetime.now(timezone.utc)
        self.assertGreater(delta.total_seconds(), 29 * 86400)
        self.assertLess(delta.total_seconds(), 31 * 86400)

        svc2 = SimpleNamespace(
            quota_expire_at=datetime.now(timezone.utc),
            quota_data_limit_bytes=1,
            quota_synced_at=None,
            plan=None,
        )
        # Truly unlimited active user still clears expire
        sync_service_quota_cache(svc2, {"status": "active", "expire": 0, "data_limit": 0})
        self.assertIsNone(svc2.quota_expire_at)


class DeliveryPlanTypeTests(unittest.IsolatedAsyncioTestCase):
    async def test_delivery_includes_plan_type_line(self):
        from app.services.delivery import build_delivery_content

        plan = SimpleNamespace(id=3, name="یک‌ماهه", is_trial=False, billing_mode=None, plan_kind=None)
        order = SimpleNamespace(
            id=11,
            service_id=1,
            plan_id=3,
            plan=plan,
            reseller_id=None,
            quantity=1,
            note=None,
            subscription_url=None,
        )
        svc = SimpleNamespace(
            id=1,
            subscription_token=None,
            subscription_url=None,
            pg_username="u11",
        )
        session = AsyncMock()
        session.get = AsyncMock(return_value=svc)

        async def _settings(*a, **k):
            return {
                "delivery_title": "✅ سرویس آماده است",
                "purchase_success_text": "سفارش #{order_id} فعال شد.",
                "show_sub_link_in_text": "0",
                "shop_title": "Shop",
            }

        with patch("app.services.delivery.get_all_settings", side_effect=_settings), patch(
            "app.services.delivery.kb.back_home", return_value=None
        ), patch("app.services.delivery.kb.service_actions", return_value=None):
            out = await build_delivery_content(session, None, order)

        text = (out.get("text") or "") + "\n" + (out.get("detail_text") or "")
        self.assertIn("نوع پلن", text)
        self.assertIn("یک‌ماهه", text)

    def test_plan_type_labels(self):
        from app.services.delivery import _plan_type_label

        self.assertEqual(
            _plan_type_label(SimpleNamespace(note="custom", plan=None)),
            "دلخواه",
        )
        self.assertEqual(
            _plan_type_label(SimpleNamespace(note="wholesale:5", plan=None)),
            "فروش عمده",
        )
        self.assertEqual(
            _plan_type_label(
                SimpleNamespace(note=None, plan=SimpleNamespace(is_trial=True))
            ),
            "تست",
        )


class CategoryModalScrollTests(unittest.TestCase):
    def test_modal_table_does_not_clamp_height(self):
        css = (ROOT / "app/web/static/panel.css").read_text(encoding="utf-8")
        self.assertIn(".ui-modal-scroll > .table-wrap", css)
        self.assertIn("flex-shrink: 0", css)
        self.assertIn("max-height: none", css)
        html = (ROOT / "app/web/templates/plans.html").read_text(encoding="utf-8")
        self.assertIn("plan-cat-list", html)
        self.assertIn('id="modal-plan-categories"', html)


if __name__ == "__main__":
    unittest.main()
