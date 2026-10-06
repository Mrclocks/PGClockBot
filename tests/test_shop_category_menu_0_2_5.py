"""Shop menu: categories replace «ثابت» when linked; legacy fallback; button colors."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.bot.keyboards import shop_kind_keyboard
from app.services.button_styles import resolve_category_button_style
from app.services.plan_categories import (
    plans_in_category,
    shop_categories_for_menu,
    uncategorized_fixed_plans,
)


def _plan(**kw):
    defaults = {
        "id": 1,
        "name": "P",
        "price": 1000,
        "is_trial": False,
        "category_id": None,
        "button_style": None,
        "is_active": True,
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _cat(**kw):
    defaults = {
        "id": 1,
        "name": "اقتصادی",
        "is_active": True,
        "button_style": None,
        "audience": "users",
        "owner_reseller_id": None,
        "sort_order": 0,
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


class ShopCategoryMenuLogicTests(unittest.TestCase):
    def test_uncategorized_and_filter(self):
        plans = [
            _plan(id=1, category_id=10),
            _plan(id=2, category_id=None),
            _plan(id=3, category_id=10),
        ]
        self.assertEqual([p.id for p in uncategorized_fixed_plans(plans)], [2])
        self.assertEqual([p.id for p in plans_in_category(plans, 10)], [1, 3])
        self.assertEqual([p.id for p in plans_in_category(plans, None)], [2])

    def test_shop_categories_for_menu_empty_without_links(self):
        async def run():
            session = MagicMock()
            with patch(
                "app.services.plan_categories.list_shop_categories",
                new=AsyncMock(return_value=[_cat(id=5, name="VIP")]),
            ):
                out = await shop_categories_for_menu(
                    session, fixed_plans=[_plan(category_id=None)]
                )
            self.assertEqual(out, [])

        import asyncio

        asyncio.run(run())

    def test_shop_categories_for_menu_only_linked(self):
        async def run():
            cats = [_cat(id=5, name="VIP"), _cat(id=6, name="اقتصادی")]
            session = MagicMock()
            with patch(
                "app.services.plan_categories.list_shop_categories",
                new=AsyncMock(return_value=cats),
            ):
                out = await shop_categories_for_menu(
                    session,
                    fixed_plans=[_plan(id=1, category_id=6), _plan(id=2, category_id=None)],
                )
            self.assertEqual([c.id for c in out], [6])

        import asyncio

        asyncio.run(run())


class ShopKindKeyboardCategoryTests(unittest.TestCase):
    def test_legacy_fixed_when_no_categories(self):
        markup = shop_kind_keyboard(None, fixed_on=True, trial_on=True)
        cbs = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("shop:kind:fixed", cbs)
        self.assertIn("shop:kind:trial", cbs)
        self.assertTrue(all(not (c or "").startswith("shop:cat:") for c in cbs))

    def test_categories_replace_fixed(self):
        cats = [_cat(id=3, name="VIP", button_style="danger"), _cat(id=4, name="اقتصادی")]
        markup = shop_kind_keyboard(
            {"btn_style_shop_kind_fixed": "primary"},
            fixed_on=True,
            trial_on=True,
            custom_on=True,
            categories=cats,
            include_uncategorized=True,
        )
        cbs = [b.callback_data for row in markup.inline_keyboard for b in row]
        texts = [b.text for row in markup.inline_keyboard for b in row]
        self.assertNotIn("shop:kind:fixed", cbs)
        self.assertIn("shop:cat:3", cbs)
        self.assertIn("shop:cat:4", cbs)
        self.assertIn("shop:cat:none", cbs)
        self.assertIn("shop:kind:trial", cbs)
        self.assertIn("shop:kind:custom", cbs)
        self.assertTrue(any("VIP" in t for t in texts))
        # danger style on VIP category button
        vip_btn = next(
            b for row in markup.inline_keyboard for b in row if b.callback_data == "shop:cat:3"
        )
        self.assertEqual(getattr(vip_btn, "style", None), "danger")

    def test_no_other_without_uncategorized_flag(self):
        markup = shop_kind_keyboard(
            None,
            fixed_on=True,
            categories=[_cat(id=1)],
            include_uncategorized=False,
        )
        cbs = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertNotIn("shop:cat:none", cbs)


class CategoryButtonStyleTests(unittest.TestCase):
    def test_inherit_uses_fixed_kind(self):
        ui = {"btn_style_shop_kind_fixed": "success"}
        self.assertEqual(resolve_category_button_style(ui, _cat(button_style=None)), "success")

    def test_explicit_override(self):
        ui = {"btn_style_shop_kind_fixed": "success"}
        self.assertEqual(resolve_category_button_style(ui, _cat(button_style="danger")), "danger")

    def test_white_explicit(self):
        ui = {"btn_style_shop_kind_fixed": "success"}
        self.assertIsNone(resolve_category_button_style(ui, _cat(button_style="")))


if __name__ == "__main__":
    unittest.main()
