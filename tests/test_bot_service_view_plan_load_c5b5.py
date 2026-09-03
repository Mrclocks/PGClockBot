"""Bot admin service view must not MissingGreenlet on plan access (async ORM)."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[1]


class SnapshotTelegramLinesTests(unittest.TestCase):
    def test_renders_with_plan_loaded(self):
        from app.services.bot_user_admin import ServiceSnapshot, snapshot_telegram_lines

        plan = SimpleNamespace(name="ماهانه 30 گیگ")
        svc = SimpleNamespace(id=1, plan=plan)
        snap = ServiceSnapshot(
            service=svc,
            pg={},
            status_fa="فعال",
            used_text="0",
            limit_text="25 گیگ",
            volume_text="0 / 25 گیگ",
            remain_gb_text="25 گیگ",
            days_left=25,
            expire_text="2026/09/27 · 25 روز",
            subscription_url="sub://abc",
            error=None,
        )
        text = snapshot_telegram_lines(snap)
        self.assertIn("سرویس #1", text)
        self.assertIn("ماهانه 30 گیگ", text)
        self.assertIn("0 / 25 گیگ", text)
        self.assertIn("sub://abc", text)

    def test_renders_without_plan(self):
        from app.services.bot_user_admin import ServiceSnapshot, snapshot_telegram_lines

        svc = SimpleNamespace(id=2, plan=None)
        snap = ServiceSnapshot(
            service=svc,
            pg=None,
            status_fa="بدون پنل",
            used_text="—",
            limit_text="—",
            volume_text="—",
            remain_gb_text="—",
            days_left=None,
            expire_text="—",
            subscription_url=None,
            error="سرویس به پاسارگارد وصل نیست",
        )
        text = snapshot_telegram_lines(snap)
        self.assertIn("سرویس #2", text)
        self.assertIn("—", text)
        self.assertIn("⚠️", text)


class GetOwnedServiceEagerPlanTests(unittest.IsolatedAsyncioTestCase):
    async def test_execute_uses_selectinload_plan(self):
        from app.services.bot_user_admin import get_owned_service

        plan = SimpleNamespace(name="پلن")
        svc = SimpleNamespace(id=9, bot_user_id=1, plan=plan)
        result = MagicMock()
        result.scalar_one_or_none.return_value = svc
        session = AsyncMock()
        session.execute = AsyncMock(return_value=result)

        out = await get_owned_service(session, bot_user_id=1, service_id=9)
        self.assertIs(out, svc)
        session.execute.assert_awaited_once()
        self.assertEqual(out.plan.name, "پلن")

    async def test_card_ok_after_commit_with_eager_plan(self):
        """Plan already on the instance → snapshot_telegram_lines safe after commit."""
        from app.services.bot_user_admin import ServiceSnapshot, snapshot_telegram_lines

        plan = SimpleNamespace(name="ماهانه 30 گیگ")
        svc = SimpleNamespace(id=1, bot_user_id=42, plan=plan, pg_user_id=7)
        snap = ServiceSnapshot(
            service=svc,
            pg={"status": "active"},
            status_fa="فعال",
            used_text="0",
            limit_text="25 گیگ",
            volume_text="0 / 25 گیگ",
            remain_gb_text="25 گیگ",
            days_left=25,
            expire_text="2026/09/27 · 25 روز",
            subscription_url="sub://x",
            error=None,
        )
        session = AsyncMock()
        session.commit = AsyncMock()
        await session.commit()
        text = snapshot_telegram_lines(snap)
        self.assertIn("ماهانه 30 گیگ", text)
        self.assertIn("سرویس #1", text)


class SourceGuardTests(unittest.TestCase):
    def test_get_owned_eager_loads_plan(self):
        src = (ROOT / "app/services/bot_user_admin.py").read_text(encoding="utf-8")
        fn = src.split("async def get_owned_service", 1)[1].split("\nasync def ", 1)[0]
        self.assertIn("selectinload(UserService.plan)", fn)
        self.assertNotIn("session.get(UserService", fn)


if __name__ == "__main__":
    unittest.main()
