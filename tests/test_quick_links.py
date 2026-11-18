"""Quick links: deep links per shop bot, where staff find them, and the bot screen that lists them."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.handlers.quick_links_view import NO_BOT, quick_links_text
from app.bot.hub_entries import QUICK_LINKS_CALLBACK
from app.bot.nav_inline import admin_groups_hub_keyboard, reseller_manage_hub_keyboard
from app.db import Base
from app.db.models import BotUser, ResellerProfile
from app.services.quick_links import (
    QuickLink,
    gift_code_link,
    quick_links,
    quick_links_page,
    service_renew_link,
    service_renew_links,
    start_link,
)
from app.services.settings_hub import TAB_DESCRIPTIONS
from app.services.settings_tab_groups import _GROUP_LAYOUT
from tests.postgres_support import scoped_async_engine

OWNER = {"role": "admin", "org_principal_id": 1, "org_depth": 0, "org_status": "active"}


def callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


class LinkTests(unittest.TestCase):
    def test_quick_links_point_at_the_given_bot(self) -> None:
        urls = {link.key: link.url for link in quick_links("shop_bot")}
        self.assertEqual(urls["renew"], "https://t.me/shop_bot?start=renew")
        self.assertEqual(urls["gift"], "https://t.me/shop_bot?start=gift")

    def test_no_bot_username_means_no_links(self) -> None:
        self.assertTrue(all(link.url is None for link in quick_links(None)))

    def test_service_and_gift_links(self) -> None:
        self.assertEqual(service_renew_link("shop_bot", 42), "https://t.me/shop_bot?start=renew_42")
        self.assertEqual(gift_code_link("shop_bot", "SPRING-25"), "https://t.me/shop_bot?start=gift_SPRING-25")

    def test_payloads_telegram_refuses_get_no_link(self) -> None:
        for code in ("has space", "کد", "x" * 60, "", "a/b"):
            with self.subTest(code=code):
                self.assertIsNone(gift_code_link("shop_bot", code))
        self.assertIsNone(start_link("shop_bot", "renew\n1"))


class PageAccessTests(unittest.TestCase):
    def test_owner_and_shop_settings_staff_get_their_page(self) -> None:
        self.assertEqual(quick_links_page(OWNER), "/settings?tab=links")
        self.assertEqual(
            quick_links_page({"role": "reseller", "bot_user_id": 5, "permissions": ["shop_settings"]}),
            "/shop-settings?tab=links",
        )

    def test_staff_without_settings_permission_get_no_link(self) -> None:
        self.assertIsNone(quick_links_page({"role": "reseller", "bot_user_id": 5, "permissions": ["orders"]}))
        self.assertIsNone(quick_links_page(None))

    def test_settings_list_describes_the_links_and_shows_them_first(self) -> None:
        self.assertIn("تمدید", TAB_DESCRIPTIONS["links"])
        self.assertNotIn("کانال", TAB_DESCRIPTIONS["links"])
        look = next(tabs for key, _label, tabs in _GROUP_LAYOUT if key == "look")
        self.assertEqual(look[0], "links")


class ServiceRenewLinksTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            for shop_id, username in ((10, "shop_ten_bot"), (20, "shop_twenty_bot")):
                session.add(BotUser(id=shop_id, telegram_id=shop_id, referral_code=f"s{shop_id}", role="reseller"))
                await session.flush()
                session.add(ResellerProfile(user_id=shop_id, is_active=True, bot_username=username))
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_each_service_links_through_the_bot_of_the_shop_that_sold_it(self) -> None:
        services = [SimpleNamespace(id=1, shop_id=10), SimpleNamespace(id=2, shop_id=20), SimpleNamespace(id=3, shop_id=None)]
        platform = SimpleNamespace(bot_username="platform_bot", bot_token="")
        with patch("app.services.quick_links.get_settings", return_value=platform), \
                patch("app.services.home_overview.check_bot_connection", AsyncMock()) as probe:
            async with self.Session() as session:
                links = await service_renew_links(session, services)
        probe.assert_not_awaited()
        self.assertEqual(links, {
            1: "https://t.me/shop_ten_bot?start=renew_1",
            2: "https://t.me/shop_twenty_bot?start=renew_2",
            3: "https://t.me/platform_bot?start=renew_3",
        })


class BotScreenTests(unittest.TestCase):
    def test_links_are_listed_escaped_and_copyable(self) -> None:
        text = quick_links_text("shop_bot", [QuickLink("x", "<b>label</b>", "https://t.me/shop_bot?start=x&y")])
        self.assertIn("<code>https://t.me/shop_bot?start=x&amp;y</code>", text)
        self.assertIn("&lt;b&gt;label&lt;/b&gt;", text)

    def test_missing_bot_username_explains_why(self) -> None:
        self.assertIn(NO_BOT, quick_links_text(None, []))

    def test_admin_and_shop_homes_offer_the_screen(self) -> None:
        self.assertIn(QUICK_LINKS_CALLBACK, callbacks(admin_groups_hub_keyboard({})))
        profile = SimpleNamespace(bot_permissions="", web_permissions="", is_active=True, bot_username=None)
        self.assertIn(QUICK_LINKS_CALLBACK, callbacks(reseller_manage_hub_keyboard(profile, {})))


if __name__ == "__main__":
    unittest.main()
