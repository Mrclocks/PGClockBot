"""3.3.8 — compact admin menus, QR preview bg, scoped PG stats, wholesale."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]


class CompactAdminMenuTests(unittest.TestCase):
    def test_admin_home_is_legacy_stub(self):
        from app.bot.keyboards import admin_home, admin_reply_keyboard

        kb = admin_home({"menu_layout": "compact"})
        # Inline hub gutted — live nav is reply keyboard
        self.assertEqual(len(kb.inline_keyboard), 1)
        flat = [
            b.text
            for row in admin_reply_keyboard(
                {
                    "menu_layout": "compact",
                    "btn_back": "⬅️ بازگشت",
                    "btn_menu_home": "🏠 منوی اصلی",
                    "btn_adm_payments": "🧾 رسیدها",
                }
            ).keyboard
            for b in row
        ]
        self.assertIn("🧾 رسیدها", flat)

    def test_admin_home_classic_singles(self):
        from app.bot.keyboards import admin_home

        kb = admin_home({"menu_layout": "classic"})
        self.assertTrue(all(len(r) == 1 for r in kb.inline_keyboard))

    def test_pg_admin_is_legacy_stub(self):
        from app.bot.keyboards import pg_admin_keyboard, pg_reply_keyboard

        kb = pg_admin_keyboard({"menu_layout": "compact"})
        self.assertEqual(kb.inline_keyboard, [])
        flat = [b.text for row in pg_reply_keyboard({"menu_layout": "compact", "btn_back": "⬅️ بازگشت", "btn_menu_home": "🏠 منوی اصلی"}).keyboard for b in row]
        self.assertIn("🏠 نمای کلی", flat)

    def test_admin_main_menu_is_legacy_stub(self):
        from app.bot.keyboards import admin_main_menu, admin_reply_keyboard

        kb = admin_main_menu({"menu_layout": "compact"})
        self.assertEqual(kb.inline_keyboard, [])
        flat = [b.text for row in admin_reply_keyboard({"menu_layout": "compact", "btn_back": "⬅️ بازگشت", "btn_menu_home": "🏠 منوی اصلی"}).keyboard for b in row]
        self.assertIn("📊 داشبورد", flat)

    def test_reseller_home_is_legacy_stub(self):
        from app.bot.keyboards import reseller_home

        kb = reseller_home(None, ui={"menu_layout": "compact"})
        self.assertEqual(kb.inline_keyboard, [])


class QrPreviewBackgroundTests(unittest.TestCase):
    def test_preview_markup_has_bg_img(self):
        html = (ROOT / "app/web/templates/_tg_preview_chat.html").read_text(encoding="utf-8")
        self.assertIn('id="pv-qr-bg"', html)
        self.assertIn("tg-qr-bg", html)

    def test_preview_js_applies_background(self):
        js = (ROOT / "app/web/templates/_tg_preview_chat_js.html").read_text(encoding="utf-8")
        self.assertIn("applyQrBackground", js)
        self.assertIn("qr_background", js)
        self.assertIn("createObjectURL", js)
        self.assertIn("s_qr_background", js)


class WholesaleLogicTests(unittest.TestCase):
    def test_parse_tiers_and_price(self):
        from app.services.orders import (
            calc_wholesale_price,
            parse_wholesale_tiers,
            wholesale_description,
            wholesale_tier_percent,
        )

        tiers = parse_wholesale_tiers(
            '[{"min":5,"percent":10},{"min":20,"percent":20},{"min":-1,"percent":5}]'
        )
        self.assertEqual(tiers, [{"min": 5, "percent": 10}, {"min": 20, "percent": 20}])
        self.assertEqual(wholesale_tier_percent(4, tiers), 0)
        self.assertEqual(wholesale_tier_percent(5, tiers), 10)
        self.assertEqual(wholesale_tier_percent(25, tiers), 20)
        amount, discount = calc_wholesale_price(unit_price=100_000, quantity=5, percent=10)
        self.assertEqual(amount, 450_000)
        self.assertEqual(discount, 50_000)
        desc = wholesale_description(
            {
                "wholesale_min_qty": "5",
                "wholesale_max_qty": "20",
                "wholesale_tiers": '[{"min":5,"percent":10}]',
            }
        )
        self.assertIn("حداقل خرید", desc)
        self.assertIn("10٪", desc)
        self.assertIn("5", desc)

    def test_plans_keyboard_has_wholesale(self):
        from app.bot.keyboards import plans_keyboard, shop_reply_keyboard

        p = MagicMock()
        p.id = 1
        p.name = "A"
        p.price = 1000
        p.is_trial = False
        kb = plans_keyboard([p], {"btn_wholesale": "فروش عمده"}, wholesale_enabled=True)
        flat = [b.callback_data for row in kb.inline_keyboard for b in row]
        # Plan names only — wholesale chrome moved to reply KB
        self.assertTrue(any(str(x).startswith("shop:plan:") for x in flat))
        self.assertNotIn("shop:wholesale", flat)
        shop = shop_reply_keyboard(
            {"btn_wholesale": "فروش عمده", "btn_back": "⬅️ بازگشت", "btn_menu_home": "🏠 منوی اصلی"},
            wholesale_enabled=True,
        )
        sflat = [b.text for row in shop.keyboard for b in row]
        self.assertIn("فروش عمده", sflat)

    def test_web_modal_and_route_exist(self):
        html = (ROOT / "app/web/templates/plans.html").read_text(encoding="utf-8")
        self.assertIn("modal-wholesale", html)
        self.assertIn("/plans/wholesale", html)
        self.assertIn("wholesale_tiers", html)
        api = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        self.assertIn('"/plans/wholesale"', api)
        self.assertIn("plans_wholesale_save", api)

    def test_order_has_quantity(self):
        src = (ROOT / "app/db/models.py").read_text(encoding="utf-8")
        self.assertIn("quantity: Mapped[int]", src)
        mig = (ROOT / "app/db/session.py").read_text(encoding="utf-8")
        self.assertIn('ADD COLUMN quantity', mig)
        orders = (ROOT / "app/services/orders.py").read_text(encoding="utf-8")
        self.assertIn("create_wholesale_order", orders)
        self.assertIn("quantity=qty", orders)


class ScopedPgUserStatsTests(unittest.IsolatedAsyncioTestCase):
    async def test_owned_stats_filters_by_owner(self):
        from app.services.pg_overview import _owned_user_stats

        pg = MagicMock()
        pg.get_users = AsyncMock(
            return_value={
                "users": [
                    {
                        "username": "mine1",
                        "admin": "res1",
                        "status": "active",
                        "online_at": None,
                    },
                    {
                        "username": "other",
                        "admin": "owner",
                        "status": "active",
                        "online_at": None,
                    },
                    {
                        "username": "mine2",
                        "admin": {"username": "res1"},
                        "status": "expired",
                        "online_at": None,
                    },
                ]
            }
        )
        stats = await _owned_user_stats(pg, "res1", fallback_total=99)
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["active"], 1)
        self.assertEqual(stats["expired"], 1)
        self.assertEqual(stats["online"], 0)

    async def test_overview_includes_user_stats_key(self):
        from app.services.pg_overview import build_reseller_pg_overview

        pg = MagicMock()
        pg.get_admin = AsyncMock(
            return_value={
                "username": "res1",
                "total_users": 3,
                "used_traffic": 0,
                "status": "active",
            }
        )
        pg.get_users = AsyncMock(
            return_value={
                "users": [
                    {"username": "a", "admin": "res1", "status": "active"},
                    {"username": "b", "admin": "res1", "status": "limited"},
                ]
            }
        )
        with patch(
            "app.services.pasarguard.get_pg_for_staff",
            new=AsyncMock(return_value=(pg, False)),
        ):
            out = await build_reseller_pg_overview(
                {"pg_admin_username": "res1", "role": "reseller", "pg_client_ready": True},
                session=AsyncMock(),
            )
        self.assertTrue(out["ready"])
        self.assertIsNotNone(out.get("user_stats"))
        self.assertEqual(out["user_stats"]["active"], 1)
        self.assertEqual(out["user_stats"]["limited"], 1)

    def test_pg_home_renders_user_stats(self):
        html = (ROOT / "app/web/templates/pg_home.html").read_text(encoding="utf-8")
        self.assertIn("user_stats", html)
        self.assertIn("آنلاین", html)
        self.assertIn("کل کاربران من", html)


if __name__ == "__main__":
    unittest.main()
