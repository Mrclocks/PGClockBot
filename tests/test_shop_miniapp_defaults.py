"""A shop learns why its Mini App cannot be switched on, and new shops get the permission."""

from __future__ import annotations

from app.api.reseller_pages import _shop_miniapp_may_open
from app.db.models import ResellerProfile
from app.services.miniapp_shop import PLATFORM_SWITCH, shop_miniapp_blocker
from app.services.resellers import DEFAULT_FEATURE_PERMS, normalize_feature_perms, parse_perms
from app.services.users import clear_settings_cache, set_setting
from tests.test_miniapp_shop_readonly import ShopFixture


class DefaultPermissionTests(ShopFixture):
    async def test_a_new_shop_gets_the_miniapp_permission_but_a_saved_list_is_kept(self) -> None:
        self.assertIn("miniapp", parse_perms(DEFAULT_FEATURE_PERMS))
        self.assertIn("miniapp", parse_perms(normalize_feature_perms("")))
        self.assertNotIn("miniapp", parse_perms(normalize_feature_perms("orders,payments")))

    async def test_the_blocker_names_what_is_missing(self) -> None:
        async with self.Session() as session:
            profile = await session.get(ResellerProfile, 2)
            await set_setting(session, PLATFORM_SWITCH, "0")
            clear_settings_cache()
            self.assertIn("مدیریت پلتفرم", await shop_miniapp_blocker(session, profile))
            await set_setting(session, PLATFORM_SWITCH, "1")
            clear_settings_cache()
            self.assertEqual(await shop_miniapp_blocker(session, profile), "")
            profile.web_permissions = profile.bot_permissions = "orders,payments"
            self.assertIn("دسترسی «مینی‌اپ»", await shop_miniapp_blocker(session, profile))
            profile.web_permissions = profile.bot_permissions = "orders,miniapp"
            profile.bot_token = ""
            self.assertIn("راه‌اندازی ربات", await shop_miniapp_blocker(session, profile))

    async def test_a_change_of_permission_or_active_state_is_noticed_for_the_menu_button(self) -> None:
        async with self.Session() as session:
            profile = await session.get(ResellerProfile, 2)
            profile.web_permissions = profile.bot_permissions = "orders,miniapp"
            profile.is_active = True
            self.assertTrue(_shop_miniapp_may_open(profile))
            profile.web_permissions = profile.bot_permissions = "orders"
            self.assertFalse(_shop_miniapp_may_open(profile))
            profile.web_permissions = profile.bot_permissions = "orders,miniapp"
            profile.is_active = False
            self.assertFalse(_shop_miniapp_may_open(profile))
