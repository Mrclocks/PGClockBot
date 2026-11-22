"""Automatic discount management: permission, form validation, shop isolation, navigation and escaping."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.datastructures import FormData

from app.api.discount_rule_pages import rule_from_form
from app.db.models import Base, BotUser, DiscountRule, Plan
from app.services.discount_rule_admin import (
    delete_rule,
    discount_rule_scope,
    list_rules,
    rule_view,
    save_rule,
    set_rule_enabled,
)
from app.services.coupon_programs import TRIGGERS
from app.services.marketing_nav import marketing_groups, marketing_sections
from app.services.resellers import FEATURE_PERMS
from tests.postgres_support import scoped_async_engine

NOW = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
VALID = {"name": "۱۵٪ اولین خرید", "audience": "first", "mode": "percent", "percent": "15", "event_new": "1"}


def form(values: dict[str, str], *, plans: tuple[str, ...] = ()) -> FormData:
    return FormData([*values.items(), *(("plan_ids", plan) for plan in plans)])


class FormTests(unittest.TestCase):
    def test_valid_rule(self) -> None:
        data = rule_from_form(form({**VALID, "percent": "2.5", "event_renew": "1", "valid_days": "7"}, plans=("2", "1")),
                              now=NOW)
        self.assertEqual((data.percent_bps, data.events, data.plan_ids, data.ends_at),
                         (250, "new,renew", "1,2", NOW + timedelta(days=7)))
        fixed = rule_from_form(form({**VALID, "mode": "fixed", "fixed_amount": "7000"}), now=NOW)
        self.assertEqual((fixed.mode, fixed.fixed_amount, fixed.percent_bps, fixed.ends_at), ("fixed", 7000, 0, None))

    def test_hostile_numbers_are_refused_at_parse(self) -> None:
        for extra in ({"percent": "95"}, {"percent": "-1"}, {"percent": "abc"}, {"max_per_order": "-5"},
                      {"customer_max_count": "99999999"}, {"valid_days": "99999"}, {"audience_days": "x"}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                rule_from_form(form({**VALID, **extra}), now=NOW)


class ValidationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([BotUser(id=5, telegram_id=5, referral_code="a"), BotUser(id=6, telegram_id=6, referral_code="b")])
            await session.flush()
            session.add_all([Plan(id=1, name="الف", price=1, owner_reseller_id=5),
                             Plan(id=2, name="ب", price=1, owner_reseller_id=6)])
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_incomplete_rules_are_refused_by_the_service(self) -> None:
        cases = ({"name": ""}, {"audience": "evil"}, {"event_new": ""}, {"percent": "0"},
                 {"audience": "repeat", "min_purchases": "0"}, {"audience": "returning", "audience_days": "0"})
        async with self.Session() as session:
            for extra in cases:
                with self.subTest(extra=extra), self.assertRaises(ValueError):
                    await save_rule(session, 5, rule_from_form(form({**VALID, **extra}), now=NOW))
            self.assertEqual(await list_rules(session, 5), [])

    async def test_a_rule_cannot_target_another_shops_plan(self) -> None:
        async with self.Session() as session:
            with self.assertRaisesRegex(ValueError, "متعلق به این فروشگاه"):
                await save_rule(session, 5, rule_from_form(form(VALID, plans=("2",)), now=NOW))
            saved = await save_rule(session, 5, rule_from_form(form(VALID, plans=("1",)), now=NOW))
        self.assertEqual(saved.plan_ids, "1")

    async def test_a_shop_never_reaches_another_shops_rules(self) -> None:
        async with self.Session() as session:
            rule = await save_rule(session, 5, rule_from_form(form(VALID), now=NOW))
            for action in (lambda: set_rule_enabled(session, 6, rule.id, False), lambda: delete_rule(session, 6, rule.id),
                           lambda: save_rule(session, 6, rule_from_form(form(VALID), now=NOW), rule_id=rule.id)):
                with self.assertRaises(ValueError):
                    await action()
            self.assertEqual([r.id for r in await list_rules(session, 5)], [rule.id])
            self.assertEqual(await list_rules(session, 6), [])
            self.assertEqual(await list_rules(session, None), [])
            stored = await session.get(DiscountRule, rule.id)
        self.assertTrue(stored.enabled)
        self.assertEqual(stored.shop_id, 5)

    async def test_an_edit_changes_the_same_rule_and_keeps_its_use_count(self) -> None:
        async with self.Session() as session:
            rule = await save_rule(session, 5, rule_from_form(form(VALID), now=NOW))
            rule.used_count = 4
            await session.commit()
            await save_rule(session, 5, rule_from_form(form({**VALID, "name": "۵٪", "percent": "5"}), now=NOW),
                            rule_id=rule.id)
            rules = await list_rules(session, 5)
        self.assertEqual([(r.id, r.name, r.percent_bps, r.used_count) for r in rules], [(rule.id, "۵٪", 500, 4)])


class ScopeTests(unittest.TestCase):
    def test_who_may_manage_rules(self) -> None:
        reseller = {"role": "reseller", "bot_user_id": 5, "permissions": ["discount_rules"]}
        with patch("app.services.discount_rule_admin.shop_owner_id", return_value=5):
            self.assertEqual(discount_rule_scope(reseller), 5)
            with self.assertRaises(PermissionError):
                discount_rule_scope({**reseller, "permissions": ["cashback"]})
        with patch("app.services.discount_rule_admin.shop_owner_id", return_value=None), self.assertRaises(PermissionError):
            discount_rule_scope(reseller)

    def test_permission_is_listed_for_shop_staff(self) -> None:
        self.assertIn("discount_rules", {key for key, _ in FEATURE_PERMS})

    def test_marketing_strip_offers_the_page_only_when_allowed(self) -> None:
        with patch("app.services.marketing_nav.discount_rule_scope", return_value=5):
            sections = marketing_sections({"role": "reseller", "permissions": []})
        self.assertIn("/discount-rules", [s.href for s in sections])
        offers = [g for g in marketing_groups(sections) if g.key == "offers"]
        self.assertIn("discount_rules", [s.key for s in offers[0].sections])
        with patch("app.services.marketing_nav.discount_rule_scope", side_effect=PermissionError):
            denied = marketing_sections({"role": "reseller", "permissions": []})
        self.assertNotIn("/discount-rules", [s.href for s in denied])


class RuleViewTests(unittest.TestCase):
    def test_remaining_days_and_expiry(self) -> None:
        live = SimpleNamespace(percent_bps=1250, ends_at=NOW + timedelta(hours=30))
        self.assertEqual((rule_view(live, now=NOW).percent, rule_view(live, now=NOW).remaining_days), ("12.5", 2))
        ended = SimpleNamespace(percent_bps=1000, ends_at=NOW - timedelta(days=1))
        self.assertTrue(rule_view(ended, now=NOW).is_expired)
        self.assertIsNone(rule_view(SimpleNamespace(percent_bps=0, ends_at=None), now=NOW).remaining_days)


class TemplateTests(unittest.TestCase):
    def test_page_escapes_rule_names(self) -> None:
        from jinja2 import ChainableUndefined

        from app.api.app import templates

        env = templates.env.overlay(undefined=ChainableUndefined)
        rule = SimpleNamespace(id=3, name="<script>x</script>", audience="first", min_purchases=0, audience_days=0,
                               mode="percent", percent_bps=1000, fixed_amount=0, max_per_order=None, events="new",
                               plan_ids=None, min_order_amount=0, customer_max_count=None, max_uses=None, used_count=2,
                               ends_at=None, priority=0, enabled=True)
        html = env.get_template("discount_rules.html").render(
            request=SimpleNamespace(query_params={}, url=SimpleNamespace(path="/discount-rules"), cookies={}),
            staff={"role": "admin"}, rules=[rule], views={3: rule_view(rule, now=NOW)}, plans=[(1, "<i>p</i>", True)],
            csrf_token="t", programs={}, triggers=TRIGGERS, budget=0, budget_used=0, report_days=30,
            rule_results={3: SimpleNamespace(orders=2, discount=1000, sales=9000)}, coupon_results={},
        )
        self.assertIn('action="/discount-rules/coupons/next_purchase"', html)
        self.assertNotIn("<script>x</script>", html)
        self.assertNotIn("<i>p</i>", html)
        self.assertIn('action="/discount-rules/rules/3/delete"', html)
        self.assertIn('action="/discount-rules/rules/3"', html)
        self.assertEqual(html.count('value="&lt;script&gt;x&lt;/script&gt;"'), 1)


if __name__ == "__main__":
    unittest.main()
