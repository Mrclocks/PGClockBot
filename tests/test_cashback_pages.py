"""Cashback management: permission, form validation, shop isolation, navigation and escaping."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.cashback_pages import rule_from_form, settings_from_form
from app.db.models import Base, BotUser, CashbackRule
from app.services.cashback_rules import (
    cashback_scope,
    delete_rule,
    list_rules,
    load_settings,
    save_rule,
    save_settings,
    set_rule_enabled,
)
from app.services.marketing_nav import marketing_groups, marketing_sections

VALID = {"name": "۱۰٪", "mode": "percent", "percent": "10", "event_new": "1", "allow_with_discount": "1"}


class FormTests(unittest.TestCase):
    def test_valid_rule(self) -> None:
        data = rule_from_form({**VALID, "percent": "2.5", "max_per_order": "50000", "event_renew": "1"})
        self.assertEqual((data.percent_bps, data.max_per_order, data.events), (250, 50000, "new,renew"))
        fixed = rule_from_form({**VALID, "mode": "fixed", "fixed_amount": "7000"})
        self.assertEqual((fixed.mode, fixed.fixed_amount, fixed.percent_bps), ("fixed", 7000, 0))

    def test_hostile_rules_are_refused(self) -> None:
        for extra in ({"name": ""}, {"percent": "150"}, {"percent": "-1"}, {"percent": "abc"},
                      {"event_new": ""}, {"percent": "0"}, {"max_per_order": "-5"},
                      {"customer_max_count": "99999999"}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                rule_from_form({**VALID, **extra})

    def test_settings(self) -> None:
        s = settings_from_form({"enabled": "1", "combine": "sum", "hold_hours": "48", "monthly_budget": "1000"})
        self.assertEqual((s.enabled, s.combine, s.hold_hours, s.monthly_budget), (True, "sum", 48, 1000))
        self.assertEqual(settings_from_form({"combine": "evil"}).combine, "best")
        with self.assertRaises(ValueError):
            settings_from_form({"hold_hours": "999999"})


class ScopeTests(unittest.TestCase):
    def test_who_may_manage_cashback(self) -> None:
        reseller = {"role": "reseller", "bot_user_id": 5, "permissions": ["cashback"]}
        with patch("app.services.cashback_rules.shop_owner_id", return_value=5):
            self.assertEqual(cashback_scope(reseller), 5)
            with self.assertRaises(PermissionError):
                cashback_scope({**reseller, "permissions": ["gift_codes"]})
        with patch("app.services.cashback_rules.shop_owner_id", return_value=None), self.assertRaises(PermissionError):
            cashback_scope(reseller)

    def test_marketing_strip_offers_cashback_only_when_allowed(self) -> None:
        with patch("app.services.marketing_nav.cashback_scope", return_value=5):
            sections = marketing_sections({"role": "reseller", "permissions": []})
        self.assertIn("/cashback", [s.href for s in sections])
        offers = [g for g in marketing_groups(sections) if g.key == "offers"]
        self.assertEqual([s.key for s in offers[0].sections][-1], "cashback")
        with patch("app.services.marketing_nav.cashback_scope", side_effect=PermissionError):
            denied = marketing_sections({"role": "reseller", "permissions": []})
        self.assertNotIn("/cashback", [s.href for s in denied])


class IsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([BotUser(id=5, telegram_id=5, referral_code="a"), BotUser(id=6, telegram_id=6, referral_code="b")])
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_a_shop_never_reaches_another_shops_rules_or_settings(self) -> None:
        async with self.Session() as session:
            rule = await save_rule(session, 5, rule_from_form(VALID))
            for action in (lambda: set_rule_enabled(session, 6, rule.id, False), lambda: delete_rule(session, 6, rule.id),
                           lambda: save_rule(session, 6, rule_from_form(VALID), rule_id=rule.id)):
                with self.assertRaises(ValueError):
                    await action()
            self.assertEqual([r.id for r in await list_rules(session, 5)], [rule.id])
            self.assertEqual(await list_rules(session, 6), [])
            self.assertEqual(await list_rules(session, None), [])
            await save_settings(session, 5, settings_from_form({"enabled": "1", "hold_hours": "1"}))
            self.assertTrue((await load_settings(session, 5)).enabled)
            self.assertFalse((await load_settings(session, 6)).enabled)
            stored = await session.get(CashbackRule, rule.id)
            self.assertTrue(stored.enabled)

    async def test_an_edit_changes_the_same_rule_in_place(self) -> None:
        async with self.Session() as session:
            rule = await save_rule(session, 5, rule_from_form(VALID))
            await save_rule(session, 5, rule_from_form({**VALID, "name": "۵٪", "percent": "5"}), rule_id=rule.id)
            rules = await list_rules(session, 5)
        self.assertEqual([(r.id, r.name, r.percent_bps) for r in rules], [(rule.id, "۵٪", 500)])


class TemplateTests(unittest.TestCase):
    def test_page_escapes_rule_names(self) -> None:
        from jinja2 import ChainableUndefined

        from app.api.app import templates
        from app.services.cashback_rules import CashbackSettings

        env = templates.env.overlay(undefined=ChainableUndefined)
        rule = SimpleNamespace(id=3, name="<script>x</script>", mode="percent", percent_bps=1000, fixed_amount=0,
                               max_per_order=None, events="new", first_purchase_only=False, enabled=True)
        html = env.get_template("cashback.html").render(
            request=SimpleNamespace(query_params={}, url=SimpleNamespace(path="/cashback"), cookies={}),
            staff={"role": "admin"}, rules=[rule], settings=CashbackSettings(True, "best", 24, 0), budget_used=0,
            csrf_token="t",
        )
        self.assertNotIn("<script>x</script>", html)
        self.assertIn('action="/cashback/rules/3/delete"', html)
        self.assertIn('action="/cashback/rules/3"', html)  # in-place edit form
        self.assertEqual(html.count('value="&lt;script&gt;x&lt;/script&gt;"'), 1)


if __name__ == "__main__":
    unittest.main()
