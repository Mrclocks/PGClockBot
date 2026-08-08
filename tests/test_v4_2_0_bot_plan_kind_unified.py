"""v4.2.0 — bot plan flows aligned with unified web modal (audience → kind)."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KEYBOARDS_SRC = (ROOT / "app/bot/keyboards.py").read_text(encoding="utf-8")


class ShopKindFlowTests(unittest.TestCase):
    def test_shop_kind_callbacks(self):
        src = (ROOT / "app/bot/handlers/shop.py").read_text(encoding="utf-8")
        self.assertIn("shop:kind:fixed", src)
        self.assertIn("shop:kind:trial", src)
        self.assertIn("shop:kind:custom", src)
        self.assertIn("shop:kind:wholesale", src)
        self.assertIn("shop_kind_keyboard", src)

    def test_shop_kind_keyboard_builder(self):
        self.assertIn("def shop_kind_keyboard", KEYBOARDS_SRC)
        self.assertIn("shop:kind:fixed", KEYBOARDS_SRC)
        self.assertIn("shop:kind:trial", KEYBOARDS_SRC)
        self.assertIn("shop:kind:custom", KEYBOARDS_SRC)
        self.assertIn("shop:kind:wholesale", KEYBOARDS_SRC)

    def test_shop_reply_keyboard_chrome_only(self):
        block = KEYBOARDS_SRC[
            KEYBOARDS_SRC.find("def shop_reply_keyboard")
            : KEYBOARDS_SRC.find("def shop_reply_keyboard") + 320
        ]
        self.assertIn("_submenu_footer", block)
        self.assertNotIn("REPLY_ACTION_SHOP_CUSTOM", block)

    def test_plans_keyboard_inline_back(self):
        self.assertIn("back_callback", KEYBOARDS_SRC)
        self.assertIn("shop:list", KEYBOARDS_SRC)


class AdminPlanKindFlowTests(unittest.TestCase):
    def test_admin_audience_and_kind_callbacks(self):
        admin_plans = (ROOT / "app/bot/handlers/admin_plans.py").read_text(encoding="utf-8")
        reply_nav = (ROOT / "app/bot/handlers/reply_nav.py").read_text(encoding="utf-8")
        self.assertIn("adm:plans:aud:", admin_plans)
        self.assertIn("adm:plans:kind:", admin_plans)
        self.assertIn("admin_plans_audience_reply_keyboard", KEYBOARDS_SRC)
        self.assertIn("admin_plans_kind_reply_keyboard", KEYBOARDS_SRC)
        self.assertIn("REPLY_ACTION_ADM_PLANS_AUD_USERS", reply_nav)

    def test_admin_kind_keyboard_user_kinds(self):
        block = KEYBOARDS_SRC[
            KEYBOARDS_SRC.find("def admin_plan_kind_keyboard")
            : KEYBOARDS_SRC.find("def admin_plan_kind_keyboard") + 2000
        ]
        self.assertIn("adm:plans:kind:users:fixed", block)
        self.assertIn("adm:plans:kind:users:custom", block)
        self.assertIn("adm:plans:kind:users:trial", block)
        self.assertIn("adm:plans:kind:users:wholesale", block)

    def test_admin_kind_keyboard_reseller_kinds(self):
        block = KEYBOARDS_SRC[
            KEYBOARDS_SRC.find("def admin_plan_kind_keyboard")
            : KEYBOARDS_SRC.find("def admin_plan_kind_keyboard") + 2000
        ]
        self.assertIn("adm:plans:kind:resellers:fixed", block)
        self.assertIn("adm:plans:kind:resellers:payg", block)
        self.assertNotIn("wholesale", block.split("resellers")[1][:400])


class VersionTests(unittest.TestCase):
    def test_version(self):
        from app.version import __version__

        self.assertEqual(__version__, "4.8.0")
        self.assertEqual((ROOT / "VERSION").read_text(encoding="utf-8").strip(), "4.8.0")


if __name__ == "__main__":
    unittest.main()
