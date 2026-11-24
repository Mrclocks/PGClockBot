"""Settings cache + bot performance regression tests."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


class SettingsCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_all_settings_cached(self):
        from app.services import users as users_mod

        users_mod.clear_settings_cache()
        session = MagicMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        session.execute = AsyncMock(return_value=result)

        first = await users_mod.get_all_settings(session)
        second = await users_mod.get_all_settings(session)
        self.assertEqual(first, second)
        self.assertEqual(session.execute.await_count, 1)

        users_mod.clear_settings_cache()
        await users_mod.get_all_settings(session)
        self.assertEqual(session.execute.await_count, 2)


class ForceJoinCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_membership_cached(self):
        from app.bot import middlewares as mw

        mw._FORCE_JOIN_MEMBER_CACHE.clear()
        bot = AsyncMock()
        member = MagicMock()
        member.status = "member"
        bot.get_chat_member = AsyncMock(return_value=member)

        a = await mw.check_force_join_member(bot, 42, "@chan")
        b = await mw.check_force_join_member(bot, 42, "@chan")
        self.assertTrue(a)
        self.assertTrue(b)
        self.assertEqual(bot.get_chat_member.await_count, 1)


class FooterRestoreTests(unittest.TestCase):
    def test_footer_has_star_and_visible_on_mobile(self):
        html = Path("app/web/templates/base.html").read_text(encoding="utf-8")
        self.assertIn("site-footer", html)
        self.assertIn("btn-star", html)
        self.assertIn("footer-meta", html)
        self.assertNotIn("site-footer desk-only", html)
        css = Path("app/web/static/panel.css").read_text(encoding="utf-8")
        self.assertIn(".btn-star", css)
        self.assertIn(".footer-meta", css)


class PerfWiringTests(unittest.TestCase):

    def test_shop_reuses_plans(self):
        src = Path("app/bot/handlers/shop.py").read_text(encoding="utf-8")
        self.assertIn("plans=plans", src)

    def test_notify_uses_gather(self):
        src = Path("app/services/notifications.py").read_text(encoding="utf-8")
        self.assertIn("asyncio.gather", src)

    def test_pg_groups_cached_in_fsm(self):
        src = Path("app/bot/handlers/admin_pg_users.py").read_text(encoding="utf-8")
        self.assertIn("pg_groups_cache", src)


if __name__ == "__main__":
    unittest.main()
