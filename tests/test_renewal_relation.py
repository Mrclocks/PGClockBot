"""Renewal plan list marks the service's plan, upgrades and downgrades."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.bot.handlers import services as service_handlers
from app.services.service_renewals import (
    RENEWAL_CURRENT,
    RENEWAL_DOWNGRADE,
    RENEWAL_UPGRADE,
    renewal_relation,
)


def plan(plan_id: int, *, gb: float | None, days: int, price: int = 100_000, name: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        id=plan_id, name=name or f"p{plan_id}", data_limit_gb=gb, duration_days=days, price=price,
        category_id=None, button_style=None,
    )


MONTH_50 = plan(1, gb=50, days=30)


class RenewalRelationTests(unittest.TestCase):
    def test_relations(self) -> None:
        cases = {
            "same plan": (plan(1, gb=50, days=30), RENEWAL_CURRENT),
            "more volume and time": (plan(2, gb=100, days=60), RENEWAL_UPGRADE),
            "more volume same time": (plan(3, gb=100, days=30), RENEWAL_UPGRADE),
            "unlimited volume": (plan(4, gb=None, days=30), RENEWAL_UPGRADE),
            "less volume": (plan(5, gb=20, days=30), RENEWAL_DOWNGRADE),
            "more volume less time": (plan(6, gb=100, days=7), None),
            "same size other price": (plan(7, gb=50, days=30, price=90_000), None),
        }
        for name, (target, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(renewal_relation(MONTH_50, target), expected)

    def test_unknown_current_plan_has_no_labels(self) -> None:
        self.assertIsNone(renewal_relation(None, plan(2, gb=100, days=60)))


class RenewalPlanButtonsTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_plan_first_and_moves_labelled(self) -> None:
        plans = [plan(5, gb=20, days=30, name="کم"), plan(2, gb=100, days=60, name="زیاد"), plan(1, gb=50, days=30, name="فعلی")]
        svc = SimpleNamespace(id=9, remark="", plan_id=1)
        session = MagicMock(get=AsyncMock(return_value=svc))
        callback = MagicMock(data="svc:renew:9", answer=AsyncMock(), message=MagicMock())
        edit = AsyncMock()
        with (
            patch.object(service_handlers, "get_all_settings", AsyncMock(return_value={})),
            patch.object(service_handlers, "customer_may_use_service", AsyncMock(return_value=True)),
            patch.object(service_handlers, "list_active_plans", AsyncMock(return_value=plans)),
            patch("app.services.plan_categories.shop_category_map", AsyncMock(return_value={})),
            patch.object(service_handlers, "safe_edit_text", edit),
        ):
            await service_handlers.svc_renew(callback, session, SimpleNamespace(id=4))
        texts = [row[0].text for row in edit.await_args.kwargs["reply_markup"].inline_keyboard[:3]]
        self.assertTrue(texts[0].startswith("🔄 پلن فعلی · فعلی"))
        self.assertTrue(texts[1].startswith("⬇️ تنزل · کم"))
        self.assertTrue(texts[2].startswith("⬆️ ارتقا · زیاد"))


if __name__ == "__main__":
    unittest.main()
