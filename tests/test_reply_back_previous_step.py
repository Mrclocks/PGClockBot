"""Reply «بازگشت» must restore the previous step — not always home.

Root causes:
- shop:list used state.clear() and wiped the nav stack
- deep shop screens had no within-section step marker
- services list never set a nav level, so service→back jumped to home
"""

from __future__ import annotations

import inspect
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]


class ReplyBackPreviousStepSourceTests(unittest.TestCase):
    def test_shop_list_keeps_nav_stack(self):
        from app.bot.handlers import shop as shop_h

        src = inspect.getsource(shop_h.shop_list)
        self.assertIn("clear_fsm_keep_nav", src)
        self.assertNotIn("await state.clear()", src)

    def test_handle_back_checks_shop_and_loyalty_steps(self):
        from app.bot.handlers import reply_nav

        src = inspect.getsource(reply_nav.handle_back)
        self.assertIn("SHOP_STEP", src)
        self.assertIn("LOY_STEP", src)
        self.assertIn("NAV_SERVICES", src)
        self.assertIn("open_shop_list", src)
        self.assertIn("open_loyalty_home", src)

    def test_open_services_list_sets_nav_level(self):
        from app.bot.handlers import reply_nav

        src = inspect.getsource(reply_nav.open_services_list)
        self.assertIn("NAV_SERVICES", src)
        self.assertIn("set_nav_level", src)

    def test_menu_nav_exposes_step_helpers(self):
        from app.bot import menu_nav as nav

        self.assertTrue(hasattr(nav, "set_shop_step"))
        self.assertTrue(hasattr(nav, "set_loy_step"))
        self.assertTrue(hasattr(nav, "clear_fsm_keep_nav"))
        self.assertEqual(nav.NAV_SERVICES, "services")


class ReplyBackShopStepTests(unittest.IsolatedAsyncioTestCase):
    async def test_back_from_shop_plans_reopens_hub_not_home(self):
        from app.bot import menu_nav as nav
        from app.bot.handlers.reply_nav import handle_back

        state = AsyncMock()
        state.get_data = AsyncMock(
            return_value={
                nav.NAV_LEVEL: nav.NAV_SHOP,
                nav.NAV_STACK: [nav.NAV_MAIN],
                nav.SHOP_STEP: "plans",
            }
        )
        state.set_state = AsyncMock()
        state.set_data = AsyncMock()
        state.update_data = AsyncMock()

        message = MagicMock()
        message.answer = AsyncMock()
        session = MagicMock()
        db_user = MagicMock()

        with patch(
            "app.bot.handlers.reply_nav.open_shop_list", new=AsyncMock()
        ) as open_shop, patch(
            "app.bot.handlers.start.render_home", new=AsyncMock()
        ) as home:
            await handle_back(message, session, db_user, state)

        open_shop.assert_awaited_once()
        self.assertEqual(home.await_count, 0)


class MenuReorderShopMovableTests(unittest.TestCase):
    def test_settings_menu_allows_shop_drag_and_move(self):
        html = (ROOT / "app/web/templates/_settings_menu.html").read_text(encoding="utf-8")
        self.assertIn("draggable", html)
        self.assertIn("btn-up", html)
        self.assertIn("قابل جابه‌جایی", html)
        # Must not claim shop is frozen in place
        self.assertNotIn("ثابت — قابل حذف نیست", html)

    def test_shop_settings_menu_same_contract(self):
        html = (ROOT / "app/web/templates/shop_settings.html").read_text(encoding="utf-8")
        self.assertIn("draggable", html)
        self.assertIn("قابل جابه‌جایی", html)


if __name__ == "__main__":
    unittest.main()
