"""Bot menu: category premium icon, plan color inherit, no forced emoji, lasting pcm KB."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.bot.keyboards import (
    normalize_reply_label,
    plans_keyboard,
    resolve_reply_mapping,
    shop_kind_keyboard,
    wholesale_plans_keyboard,
)
from app.services.button_styles import (
    PLAN_BUTTON_STYLE_OPTIONS,
    resolve_plan_button_style,
    setting_key,
)
from app.services.plan_categories import split_category_label_from_message


def _plan(**kw):
    defaults = {
        "id": 1,
        "name": "A",
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
        "icon_custom_emoji_id": None,
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


class PlanColorInheritCategoryTests(unittest.TestCase):
    def test_inherit_label_is_category(self):
        labels = [opt[1] for opt in PLAN_BUTTON_STYLE_OPTIONS]
        self.assertIn("ارث از دسته", labels)
        self.assertNotIn("ارث از نوع پلن", labels)

    def test_resolve_uses_category_when_plan_inherits(self):
        ui = {setting_key("shop_kind_fixed"): "primary"}
        plan = _plan(button_style=None, category_id=3)
        cat = _cat(id=3, button_style="danger")
        self.assertEqual(
            resolve_plan_button_style(ui, plan, kind="fixed", category=cat),
            "danger",
        )

    def test_resolve_falls_back_to_kind_without_category(self):
        ui = {setting_key("shop_kind_fixed"): "success"}
        plan = _plan(button_style=None, category_id=None)
        self.assertEqual(
            resolve_plan_button_style(ui, plan, kind="fixed"),
            "success",
        )

    def test_plans_keyboard_passes_category_style(self):
        ui = {setting_key("shop_kind_fixed"): "primary"}
        plan = _plan(id=9, name="VIP", button_style=None, category_id=2)
        cat = _cat(id=2, button_style="danger")
        kb = plans_keyboard(
            [plan], ui, kind="fixed", categories={2: cat}, back_callback="shop:list"
        )
        self.assertEqual(kb.inline_keyboard[0][0].style, "danger")
        self.assertNotIn("💎", kb.inline_keyboard[0][0].text)
        self.assertNotIn("🎁", kb.inline_keyboard[0][0].text)


class NoHardcodedShopEmojiTests(unittest.TestCase):
    def test_shop_kind_plain_labels(self):
        markup = shop_kind_keyboard(None, fixed_on=True, trial_on=True, custom_on=True)
        texts = [b.text for row in markup.inline_keyboard for b in row]
        self.assertIn("تست", texts)
        self.assertIn("ثابت", texts)
        self.assertIn("دلخواه", texts)
        self.assertTrue(all("💎" not in t and "🎁" not in t and "✨" not in t for t in texts))

    def test_category_button_uses_premium_icon_field(self):
        cats = [_cat(id=3, name="VIP", icon_custom_emoji_id="12345")]
        markup = shop_kind_keyboard(
            None, fixed_on=True, categories=cats, include_uncategorized=False
        )
        vip = next(
            b for row in markup.inline_keyboard for b in row if b.callback_data == "shop:cat:3"
        )
        self.assertEqual(vip.text, "VIP")
        self.assertEqual(getattr(vip, "icon_custom_emoji_id", None), "12345")

    def test_wholesale_no_diamond_prefix(self):
        kb = wholesale_plans_keyboard([_plan(name="نامحدود")], {"btn_back": "بازگشت"})
        self.assertEqual(kb.inline_keyboard[0][0].text.startswith("نامحدود"), True)


class CategoryLabelSplitTests(unittest.TestCase):
    def test_split_without_entities(self):
        name, icon = split_category_label_from_message("اقتصادی")
        self.assertEqual(name, "اقتصادی")
        self.assertIsNone(icon)

    def test_normalize_reply_strips_leading_symbols(self):
        self.assertEqual(
            normalize_reply_label("🛠 بازگشت به پنل ادمین"),
            "بازگشت به پنل ادمین",
        )
        mapping = {"🛠 بازگشت به پنل ادمین": "admin"}
        self.assertEqual(
            resolve_reply_mapping(mapping, "🌐 بازگشت به پنل ادمین"),
            "admin",
        )


class PcmLastingKeyboardGuards(unittest.TestCase):
    def test_open_categories_uses_present_inline_only(self):
        import inspect

        from app.bot.handlers import plan_catalog_manage as pcm

        src = inspect.getsource(pcm.open_categories_manage)
        self.assertIn("present_inline_only", src)
        self.assertIn("_cat_list_markup", src)
        self.assertNotIn("admin_plans_kind_reply_keyboard", src)

    def test_cat_list_has_back(self):
        from app.bot.handlers.plan_catalog_manage import _cat_list_markup

        markup = _cat_list_markup([_cat()], ui={"btn_back": "بازگشت"})
        cbs = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("pcm:back", cbs)


class ResellerApplyCategoryKeyboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_home_keyboard_shows_categories(self):
        from app.bot.handlers.reseller import _resapply_home_keyboard

        cats = [_cat(id=7, name="ویژه", button_style="success")]
        plans = [_plan(id=1, category_id=7)]
        session = MagicMock()
        with (
            patch(
                "app.bot.handlers.reseller.list_active_reseller_plans",
                new=AsyncMock(return_value=plans),
            ),
            patch(
                "app.services.plan_categories.reseller_categories_for_menu",
                new=AsyncMock(return_value=cats),
            ),
            patch(
                "app.services.plan_categories.uncategorized_reseller_plans",
                return_value=[],
            ),
        ):
            markup = await _resapply_home_keyboard(session, {})
        cbs = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("resapply:cat:7", cbs)
        self.assertIn("menu:home", cbs)
        self.assertNotIn("resapply:mode:fixed", cbs)


if __name__ == "__main__":
    unittest.main()
