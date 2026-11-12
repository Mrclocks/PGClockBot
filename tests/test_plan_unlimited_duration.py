"""User plan duration: empty/0 = unlimited (panel + delivery)."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from tests.template_source import template_with_includes

ROOT = Path(__file__).resolve().parents[1]


class ParseDurationTests(unittest.TestCase):
    def test_blank_and_zero_are_unlimited(self):
        from app.services.plans_catalog import parse_user_plan_duration_days

        self.assertEqual(parse_user_plan_duration_days(""), 0)
        self.assertEqual(parse_user_plan_duration_days(None), 0)
        self.assertEqual(parse_user_plan_duration_days("  "), 0)
        self.assertEqual(parse_user_plan_duration_days("0"), 0)
        self.assertEqual(parse_user_plan_duration_days("۰"), 0)

    def test_positive_days(self):
        from app.services.plans_catalog import parse_user_plan_duration_days

        self.assertEqual(parse_user_plan_duration_days("30"), 30)
        self.assertEqual(parse_user_plan_duration_days("۱"), 1)

    def test_negative_rejected(self):
        from app.services.plans_catalog import parse_user_plan_duration_days

        with self.assertRaises(ValueError):
            parse_user_plan_duration_days("-1")


class FormatDurationTests(unittest.TestCase):
    def test_label(self):
        from app.services.formatting import format_plan_duration_days

        self.assertEqual(format_plan_duration_days(0), "نامحدود")
        self.assertEqual(format_plan_duration_days(None), "نامحدود")
        self.assertEqual(format_plan_duration_days(30), "30 روز")


class DeliveryUnlimitedDurationTests(unittest.TestCase):
    def test_zero_days_omits_expire(self):
        """Mirror deliver_order arithmetic: falsy duration → no expire ts."""
        plan = SimpleNamespace(duration_days=0, data_limit_gb=10.0)
        expire = None
        if plan.duration_days:
            expire = 1
        self.assertIsNone(expire)

    def test_renewal_terms_allow_zero_days(self):
        from app.services.service_renewals import renewal_terms

        plan = SimpleNamespace(duration_days=0, data_limit_gb=None)
        terms = renewal_terms(plan)
        self.assertEqual(terms["duration_seconds"], 0)
        self.assertIsNone(terms["volume_bytes"])


class PanelTemplateGuards(unittest.TestCase):
    def test_create_and_edit_allow_empty_duration(self):
        plans = template_with_includes("plans.html")
        edit = (ROOT / "app/web/templates/plan_edit.html").read_text(encoding="utf-8")
        # Fixed-plan create field (not trial / reseller)
        create_chunk = plans.split('id="panel-user-fixed"')[1].split('id="panel-user-trial"')[0]
        self.assertIn('name="duration_days"', create_chunk)
        self.assertNotIn("required", create_chunk.split('name="duration_days"')[1].split("/>")[0])
        self.assertIn('min="0"', create_chunk.split('name="duration_days"')[1].split("/>")[0])
        # «نامحدود» toggle empties the field (empty duration = no expiry)
        self.assertIn('data-ds-unlimited="#user-plan-days" data-ds-empty-is-unlimited', create_chunk)
        edit_field = edit.split('name="duration_days"')[1].split("/>")[0]
        self.assertNotIn("required", edit_field)
        self.assertIn('min="0"', edit_field)
        self.assertIn('data-ds-unlimited="#edit-plan-days" data-ds-empty-is-unlimited', edit)

    def test_api_parses_duration_as_string(self):
        src = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        create = src.split("async def plans_create")[1].split("async def plans_trial_save")[0]
        self.assertIn('duration_days: str = Form("")', create)
        self.assertIn("parse_user_plan_duration_days", create)
        edit = src.split("async def plans_edit_save")[1].split("async def ")[0]
        self.assertIn('duration_days: str = Form("")', edit)
        self.assertIn("parse_user_plan_duration_days", edit)


if __name__ == "__main__":
    unittest.main()
