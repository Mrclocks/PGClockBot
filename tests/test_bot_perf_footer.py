"""Settings cache + bot performance regression tests."""

from __future__ import annotations

import re
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


class SiteFooterTests(unittest.TestCase):
    def test_footer_links_to_github_channel_and_group_as_text(self):
        from jinja2 import Environment, FileSystemLoader, select_autoescape

        env = Environment(loader=FileSystemLoader("app/web/templates"), autoescape=select_autoescape())
        html = env.get_template("_site_footer.html").render()
        hrefs = re.findall(r'class="footer-link" href="([^"]+)"', html)
        self.assertEqual(
            hrefs,
            [
                "https://github.com/Mrclocks/PGClockBot",
                "https://t.me/MrClockHub",
                "https://t.me/MrClock_Chat",
            ],
        )
        self.assertIn("MrClock<", html)
        self.assertNotIn("MrClockBot", html)
        self.assertNotIn('class="btn', html)
        self.assertEqual(html.count('rel="noopener noreferrer"'), 3)


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
