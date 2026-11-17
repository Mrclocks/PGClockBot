"""Capsule num-stepper + signed day/GB adjust (web + bot)."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]


class NumStepperUiTests(unittest.TestCase):
    def test_macro_and_css_js_present(self):
        macros = (ROOT / "app/web/templates/macros.html").read_text(encoding="utf-8")
        self.assertIn("{% macro num_stepper(", macros)
        self.assertIn("data-num-stepper", macros)
        self.assertIn("data-num-stepper-dec", macros)
        self.assertIn("data-num-stepper-inc", macros)

        css = (ROOT / "app/web/static/panel.css").read_text(encoding="utf-8")
        self.assertIn(".num-stepper {", css)
        self.assertIn(".num-stepper-btn", css)
        self.assertIn(".num-stepper-input", css)
        # Circular ± buttons inset inside the outer capsule
        btn = css.split(".num-stepper-btn {", 1)[1].split("}", 1)[0]
        self.assertIn("border-radius: var(--control-radius)", btn)
        # Value centered — must be excluded from global input text-align:right
        self.assertIn(":not(.num-stepper-input)", css)
        self.assertIn(
            "text-align: center",
            css.split("input.num-stepper-input[dir=\"ltr\"]", 1)[1][:500],
        )

        js = (ROOT / "app/web/static/panel.js").read_text(encoding="utf-8")
        self.assertIn("[data-num-stepper]", js)
        self.assertIn("data-num-stepper-dec", js)
        self.assertIn("initNumSteppers", js)

    def test_user_edit_uses_steppers_not_plus_only(self):
        body = (ROOT / "app/web/templates/_user_edit_body.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("num_stepper", body)
        self.assertIn("تغییر مانده", body)
        self.assertNotIn("افزایش مانده", body)
        # Positive amounts plus «کاهش» / «افزایش», like the wallet form (the sign comes from the button).
        extend = body.split("/extend", 1)[1].split("</form>", 1)[0]
        self.assertIn("min=0", extend)
        self.assertIn("max=3650", extend)
        self.assertIn('name="mode" value="debit"', extend)
        self.assertIn('name="mode" value="credit"', extend)

    def test_reseller_edit_subscription_adjust(self):
        body = (ROOT / "app/web/templates/_reseller_edit_body.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("/subscription/adjust", body)
        self.assertIn("تغییر ظرفیت", body)
        self.assertIn("num_stepper", body)
        pages = (ROOT / "app/api/reseller_pages.py").read_text(encoding="utf-8")
        self.assertIn("/resellers/{user_id}/subscription/adjust", pages)
        self.assertIn("admin_adjust_reseller_subscription", pages)


class AdminExtendNegativesTests(unittest.IsolatedAsyncioTestCase):
    async def _run_extend(self, *, info: dict, days: int = 0, gb: float = 0):
        from app.services.bot_user_admin import admin_extend_service

        session = AsyncMock()
        svc = MagicMock(pg_user_id=7, id=3)
        pg = AsyncMock()
        pg.get_user_by_id = AsyncMock(return_value=info)
        snap = MagicMock()
        with (
            patch(
                "app.services.bot_user_admin._pg_client_for_bot_service",
            AsyncMock(return_value=pg),
            ),
            patch(
                "app.services.bot_user_admin.admin_set_service_quota",
                AsyncMock(return_value=snap),
            ) as set_quota,
        ):
            out = await admin_extend_service(
                session, svc, extra_days=days, extra_gb=gb
            )
        return out, set_quota

    async def test_both_zero_error(self):
        from app.services.bot_user_admin import admin_extend_service

        with self.assertRaises(ValueError) as ctx:
            await admin_extend_service(
                AsyncMock(), MagicMock(pg_user_id=1), extra_days=0, extra_gb=0
            )
        self.assertIn("غیرصفر", str(ctx.exception))

    async def test_negative_days_allows_past_expire(self):
        now = datetime.now(timezone.utc)
        future = now + timedelta(days=10)
        _, set_quota = await self._run_extend(
            info={"expire": future.isoformat(), "data_limit": 5 * (1024**3)},
            days=-3,
        )
        kwargs = set_quota.await_args.kwargs
        expire_ts = kwargs["expire_ts"]
        expected = int(future.timestamp()) - 3 * 86400
        self.assertEqual(expire_ts, expected)
        # May be in the past relative to now if we had fewer days — still valid
        self.assertGreaterEqual(expire_ts, 0)

    async def test_negative_gb_floors_at_zero(self):
        from app.services.bot_user_admin import GB

        _, set_quota = await self._run_extend(
            info={"expire": None, "data_limit": int(2 * GB)},
            gb=-5,
        )
        kwargs = set_quota.await_args.kwargs
        self.assertEqual(kwargs["data_limit_bytes"], 0)

    async def test_reduce_unlimited_gb_errors(self):
        from app.services.bot_user_admin import admin_extend_service

        session = AsyncMock()
        svc = MagicMock(pg_user_id=7)
        pg = AsyncMock()
        pg.get_user_by_id = AsyncMock(return_value={"data_limit": 0})
        with patch(
            "app.services.bot_user_admin._pg_client_for_bot_service",
            AsyncMock(return_value=pg),
        ):
            with self.assertRaises(ValueError) as ctx:
                await admin_extend_service(
                    session, svc, extra_days=0, extra_gb=-1
                )
        self.assertIn("نامحدود", str(ctx.exception))

    async def test_positive_still_works(self):
        from app.services.bot_user_admin import GB

        now = datetime.now(timezone.utc)
        future = now + timedelta(days=5)
        _, set_quota = await self._run_extend(
            info={"expire": future.isoformat(), "data_limit": int(10 * GB)},
            days=2,
            gb=1.5,
        )
        kwargs = set_quota.await_args.kwargs
        self.assertEqual(kwargs["expire_ts"], int(future.timestamp()) + 2 * 86400)
        self.assertEqual(kwargs["data_limit_bytes"], int(10 * GB) + int(1.5 * GB))


class ResellerAdjustTests(unittest.IsolatedAsyncioTestCase):
    async def test_adjust_days_and_gb_floor(self):
        from app.services.pg_admin_subscription import admin_adjust_reseller_subscription

        now = datetime.now(timezone.utc)
        sub = MagicMock(
            pg_username="r1",
            expires_at=now + timedelta(days=20),
            base_gb=100,
            extra_gb_purchased=30,
            base_users=10,
            extra_users_purchased=0,
        )
        profile = MagicMock(pg_admin_username="r1")
        session = AsyncMock()
        with (
            patch(
                "app.services.pg_admin_subscription.get_subscription",
                AsyncMock(return_value=sub),
            ),
            patch(
                "app.services.pg_admin_subscription._assert_owner_capacity_budget",
                AsyncMock(),
            ),
            patch(
                "app.services.pg_admin_subscription.sync_pg_capacity_from_sub",
                AsyncMock(),
            ),
        ):
            result = await admin_adjust_reseller_subscription(
                session, profile, extra_days=-5, extra_gb=-40
            )
        self.assertEqual(sub.extra_gb_purchased, 0)
        self.assertEqual(sub.base_gb, 90)  # 100+30-40 → eat 30 extra then 10 base
        self.assertEqual(result["total_gb"], 90)
        delta = sub.expires_at - (now + timedelta(days=20))
        self.assertAlmostEqual(delta.total_seconds(), -5 * 86400, delta=2)

    async def test_unlimited_negative_days_errors(self):
        from app.services.pg_admin_subscription import admin_adjust_reseller_subscription

        sub = MagicMock(
            pg_username="r1",
            expires_at=None,
            base_gb=10,
            extra_gb_purchased=0,
            base_users=0,
            extra_users_purchased=0,
        )
        profile = MagicMock(pg_admin_username="r1")
        with patch(
            "app.services.pg_admin_subscription.get_subscription",
            AsyncMock(return_value=sub),
        ):
            with self.assertRaises(ValueError) as ctx:
                await admin_adjust_reseller_subscription(
                    AsyncMock(), profile, extra_days=-1, extra_gb=0
                )
        self.assertIn("بدون انقضا", str(ctx.exception))

    async def test_fail_closed_no_username(self):
        from app.services.pg_admin_subscription import admin_adjust_reseller_subscription

        with self.assertRaises(ValueError):
            await admin_adjust_reseller_subscription(
                AsyncMock(), MagicMock(pg_admin_username=None), extra_days=1
            )


class BotCallbackParsingTests(unittest.TestCase):
    def test_user_service_adjust_callbacks(self):
        kb = (ROOT / "app/bot/keyboards.py").read_text(encoding="utf-8")
        self.assertIn("adm:users:svcadj:", kb)
        self.assertIn("admin_user_service_adjust_keyboard", kb)
        self.assertIn(":days:+", kb)
        self.assertIn(":days:-", kb)
        self.assertIn(":gb:+", kb)
        self.assertIn(":confirm", kb)
        self.assertNotIn("svcext:", kb)
        self.assertNotIn(":d30", kb)
        self.assertNotIn(":g10", kb)
        admin = (ROOT / "app/bot/handlers/admin.py").read_text(encoding="utf-8")
        self.assertIn('F.data.startswith("adm:users:svcadj:")', admin)
        self.assertNotIn("adm:users:svcext:", admin)
        self.assertIn("svc_adjust_days_input", admin)
        self.assertIn('detail in {"+", "-"}', admin)

    def test_reseller_capacity_adjust_callbacks(self):
        kb = (ROOT / "app/bot/keyboards.py").read_text(encoding="utf-8")
        self.assertIn("adm:resellers:capadj:", kb)
        self.assertIn("admin_reseller_capacity_adjust_keyboard", kb)
        admin = (ROOT / "app/bot/handlers/admin.py").read_text(encoding="utf-8")
        self.assertIn('F.data.startswith("adm:resellers:capadj:")', admin)
        self.assertIn("reseller_cap_days_input", admin)


if __name__ == "__main__":
    unittest.main()
