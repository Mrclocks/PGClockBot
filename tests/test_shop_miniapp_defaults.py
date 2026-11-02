"""A shop learns why its Mini App cannot be switched on, and new shops get the permission."""

from __future__ import annotations

from app.api.reseller_pages import MINIAPP_STATUS_TEXT, _shop_miniapp_may_open
from app.db.models import ResellerProfile
from app.services.miniapp_shop import (
    PLATFORM_SWITCH,
    SHOP_SWITCH,
    STATE_NO_PERMISSION,
    STATE_OPEN,
    STATE_PLATFORM_OFF,
    STATE_SETUP,
    STATE_SHOP_OFF,
    shop_miniapp_blocker,
    shop_miniapp_state,
)
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


class MiniAppStateTests(ShopFixture):
    async def test_the_state_is_the_first_missing_condition_and_every_state_reads_for_the_owner(self) -> None:
        async with self.Session() as session:
            profile = await session.get(ResellerProfile, 2)
            profile.web_permissions = profile.bot_permissions = "orders,miniapp"
            seen = [await shop_miniapp_state(session, profile)]
            await set_setting(session, SHOP_SWITCH, "0", reseller_id=int(profile.user_id))
            clear_settings_cache()
            seen.append(await shop_miniapp_state(session, profile))
            profile.web_permissions = profile.bot_permissions = "orders"
            seen.append(await shop_miniapp_state(session, profile))
            await set_setting(session, PLATFORM_SWITCH, "0")
            clear_settings_cache()
            seen.append(await shop_miniapp_state(session, profile))
            profile.is_active = False
            seen.append(await shop_miniapp_state(session, profile))
        self.assertEqual(seen, [STATE_OPEN, STATE_SHOP_OFF, STATE_NO_PERMISSION, STATE_PLATFORM_OFF, STATE_SETUP])
        self.assertEqual(set(MINIAPP_STATUS_TEXT), set(seen))
        self.assertTrue(all(text.strip() for text in MINIAPP_STATUS_TEXT.values()))

    async def test_a_shop_whose_own_switch_is_off_is_not_told_to_ask_the_platform(self) -> None:
        async with self.Session() as session:
            profile = await session.get(ResellerProfile, 2)
            profile.web_permissions = profile.bot_permissions = "orders,miniapp"
            await set_setting(session, SHOP_SWITCH, "0", reseller_id=int(profile.user_id))
            clear_settings_cache()
            # Its own switch is the one it can flip; nothing blocks it.
            self.assertEqual(await shop_miniapp_blocker(session, profile), "")
