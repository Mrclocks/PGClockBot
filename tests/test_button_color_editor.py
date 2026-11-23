"""Button colors from the bot settings: owner/shop permissions, shop isolation, UI flow."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.handlers import button_colors as ui_module
from app.db.models import ResellerSetting, Setting
from app.services.button_color_editor import (
    color_groups,
    find_color_item,
    reset_button_color,
    set_button_color,
    shared_button_count,
)
from app.services.button_styles import CATALOG_BY_ID, allowed_style_setting_keys
from app.services.users import (
    DEFAULT_SETTINGS,
    clear_settings_cache,
    get_all_settings,
    reset_shop_reseller_id,
    set_shop_reseller_id,
)
from tests.postgres_support import add_shop_users, scoped_async_engine

SHOP_A = 501
SHOP_B = 502
SHOP_COLOR_ID = "wallet_topup"
ADMIN_ONLY_ID = "adm_dash"


class _DatabaseCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from app.db import Base
        import app.db.models  # noqa: F401  (registers every table on Base)

        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        await add_shop_users(self.Session, SHOP_A, SHOP_B)
        clear_settings_cache()

    async def asyncTearDown(self) -> None:
        clear_settings_cache()
        await self.engine.dispose()

    async def _platform_value(self, key: str) -> str | None:
        async with self.Session() as session:
            return await session.scalar(select(Setting.value).where(Setting.key == key))

    async def _shop_value(self, shop_id: int, key: str) -> str | None:
        async with self.Session() as session:
            return await session.scalar(
                select(ResellerSetting.value).where(
                    ResellerSetting.reseller_user_id == shop_id, ResellerSetting.key == key
                )
            )


class ButtonColorServiceTests(_DatabaseCase):
    async def test_owner_color_is_stored_in_platform_settings_only(self) -> None:
        async with self.Session() as session:
            await set_button_color(
                session, style_id=SHOP_COLOR_ID, color="danger", for_reseller=False, shop_id=None
            )
        self.assertEqual(await self._platform_value("btn_style_wallet_topup"), "danger")
        self.assertIsNone(await self._shop_value(SHOP_A, "btn_style_wallet_topup"))

    async def test_shop_color_stays_inside_that_shop(self) -> None:
        async with self.Session() as session:
            await set_button_color(
                session, style_id=SHOP_COLOR_ID, color="danger", for_reseller=True, shop_id=SHOP_A
            )
            shop_a = await get_all_settings(session, reseller_id=SHOP_A)
            shop_b = await get_all_settings(session, reseller_id=SHOP_B)
        self.assertEqual(shop_a["btn_style_wallet_topup"], "danger")
        self.assertEqual(shop_b["btn_style_wallet_topup"], DEFAULT_SETTINGS["btn_style_wallet_topup"])
        self.assertIsNone(await self._platform_value("btn_style_wallet_topup"))

    async def test_shop_cannot_color_an_admin_only_button(self) -> None:
        async with self.Session() as session:
            with self.assertRaises(ValueError):
                await set_button_color(
                    session, style_id=ADMIN_ONLY_ID, color="danger", for_reseller=True, shop_id=SHOP_A
                )
        self.assertIsNone(await self._shop_value(SHOP_A, "btn_style_adm_dash"))

    async def test_rejects_unknown_ids_aliases_and_colors(self) -> None:
        async with self.Session() as session:
            for style_id, color in (("no_such_button", "danger"), ("rev_ok", "danger"), (SHOP_COLOR_ID, "purple")):
                with self.subTest(style_id=style_id, color=color), self.assertRaises(ValueError):
                    await set_button_color(
                        session, style_id=style_id, color=color, for_reseller=False, shop_id=None
                    )
        self.assertIsNone(await self._platform_value("btn_style_no_such_button"))

    async def test_shop_write_requires_a_shop(self) -> None:
        async with self.Session() as session:
            with self.assertRaises(ValueError):
                await set_button_color(
                    session, style_id=SHOP_COLOR_ID, color="danger", for_reseller=True, shop_id=None
                )

    async def test_owner_write_ignores_a_leftover_shop_context(self) -> None:
        token = set_shop_reseller_id(SHOP_A)
        try:
            async with self.Session() as session:
                await set_button_color(
                    session, style_id=SHOP_COLOR_ID, color="success", for_reseller=False, shop_id=None
                )
        finally:
            reset_shop_reseller_id(token)
        self.assertEqual(await self._platform_value("btn_style_wallet_topup"), "success")
        self.assertIsNone(await self._shop_value(SHOP_A, "btn_style_wallet_topup"))

    async def test_reset_restores_the_catalog_default(self) -> None:
        async with self.Session() as session:
            await set_button_color(
                session, style_id=SHOP_COLOR_ID, color="danger", for_reseller=True, shop_id=SHOP_A
            )
            restored = await reset_button_color(
                session, style_id=SHOP_COLOR_ID, for_reseller=True, shop_id=SHOP_A
            )
        self.assertEqual(restored, CATALOG_BY_ID[SHOP_COLOR_ID]["default"])
        self.assertEqual(await self._shop_value(SHOP_A, "btn_style_wallet_topup"), restored)


class ButtonColorCatalogTests(unittest.TestCase):
    def test_shop_groups_list_only_colors_a_shop_may_save(self) -> None:
        allowed = allowed_style_setting_keys(for_reseller=True)
        for group in color_groups(dict(DEFAULT_SETTINGS), for_reseller=True):
            for item in group.items:
                with self.subTest(item=item.style_id):
                    self.assertIn(f"btn_style_{item.style_id}", allowed)

    def test_find_rejects_admin_only_for_shop(self) -> None:
        with self.assertRaises(ValueError):
            find_color_item(dict(DEFAULT_SETTINGS), ADMIN_ONLY_ID, for_reseller=True)
        self.assertEqual(find_color_item(dict(DEFAULT_SETTINGS), ADMIN_ONLY_ID, for_reseller=False).style_id, ADMIN_ONLY_ID)

    def test_shared_colors_are_counted(self) -> None:
        self.assertGreater(shared_button_count("confirm"), 1)
        self.assertEqual(shared_button_count(SHOP_COLOR_ID), 1)


class ButtonColorViewTests(unittest.TestCase):
    def _all_views(self, *, for_reseller: bool):
        audience = "r" if for_reseller else "a"
        groups = color_groups(dict(DEFAULT_SETTINGS), for_reseller=for_reseller)
        yield ui_module.groups_view(groups, audience)
        for index, group in enumerate(groups):
            pages = -(-len(group.items) // ui_module.ITEMS_PER_PAGE)
            for page in range(pages):
                yield ui_module.group_view(group, audience, index, page)
            for item in group.items:
                yield ui_module.picker_view(item, audience, index, 0)

    def test_every_callback_fits_and_parses(self) -> None:
        for for_reseller in (False, True):
            for _text, markup in self._all_views(for_reseller=for_reseller):
                for button in (b for row in markup.inline_keyboard for b in row):
                    data = button.callback_data
                    with self.subTest(data=data):
                        self.assertLessEqual(len(data.encode("utf-8")), 64)
                        if data.startswith("bcl:"):
                            self.assertIsNotNone(ui_module.parse_request(data))

    def test_group_pages_hold_at_most_eight_buttons(self) -> None:
        for _text, markup in self._all_views(for_reseller=False):
            items = [b for row in markup.inline_keyboard for b in row if ":i:" in b.callback_data]
            self.assertLessEqual(len(items), ui_module.ITEMS_PER_PAGE)

    def test_picker_shows_each_color_in_its_own_style_and_marks_current(self) -> None:
        item = find_color_item(dict(DEFAULT_SETTINGS), SHOP_COLOR_ID, for_reseller=False)
        _text, markup = ui_module.picker_view(item, "a", 0, 0)
        colors = [b for row in markup.inline_keyboard[:2] for b in row]
        self.assertEqual([b.style for b in colors], [None, "primary", "success", "danger"])
        self.assertEqual(sum(b.text.startswith("✓") for b in colors), 1)

    def test_shared_color_warning_names_the_count(self) -> None:
        item = find_color_item(dict(DEFAULT_SETTINGS), "confirm", for_reseller=False)
        text, _markup = ui_module.picker_view(item, "a", 0, 0)
        self.assertIn(f"بین {shared_button_count('confirm')} دکمه", text)

    def test_malformed_callbacks_are_rejected(self) -> None:
        for data in ("bcl:x:g", "bcl:a:g:one:0", "bcl:a:i:0", "bcl:a:s:0:wallet", "other:a:g", "", None):
            with self.subTest(data=data):
                self.assertIsNone(ui_module.parse_request(data))


def _callback(data: str) -> MagicMock:
    callback = MagicMock()
    callback.data = data
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()
    return callback


class ButtonColorHandlerTests(_DatabaseCase):
    def _shop_actor(self, profile: object | None):
        return patch(
            "app.services.reseller_access.load_reseller_actor",
            AsyncMock(return_value=(SHOP_A if profile else None, profile)),
        )

    async def _shop_tap(self, data: str, profile: object | None) -> MagicMock:
        callback = _callback(data)
        with self._shop_actor(profile), patch.object(ui_module, "has_bot_perm", return_value=True):
            async with self.Session() as session:
                await ui_module.shop_colors(callback, session, MagicMock(), is_reseller_bot=True, reseller_owner_id=SHOP_A)
        return callback

    def _group_index(self, style_id: str, *, for_reseller: bool) -> int:
        groups = color_groups(dict(DEFAULT_SETTINGS), for_reseller=for_reseller)
        return next(i for i, g in enumerate(groups) if any(it.style_id == style_id for it in g.items))

    async def test_shop_owner_saves_a_color_for_their_shop(self) -> None:
        group = self._group_index(SHOP_COLOR_ID, for_reseller=True)
        callback = await self._shop_tap(f"bcl:r:s:{group}:{SHOP_COLOR_ID}:d", SimpleNamespace(user_id=SHOP_A))
        self.assertEqual(await self._shop_value(SHOP_A, "btn_style_wallet_topup"), "danger")
        callback.answer.assert_awaited_with("رنگ ذخیره شد")
        self.assertIn("✓ 🔴", str(callback.message.edit_text.await_args))

    async def test_forged_admin_only_color_is_refused_for_a_shop(self) -> None:
        callback = await self._shop_tap(f"bcl:r:s:0:{ADMIN_ONLY_ID}:d", SimpleNamespace(user_id=SHOP_A))
        self.assertIsNone(await self._shop_value(SHOP_A, "btn_style_adm_dash"))
        self.assertTrue(callback.answer.await_args.kwargs.get("show_alert"))

    async def test_without_shop_access_nothing_is_saved(self) -> None:
        callback = await self._shop_tap(f"bcl:r:s:0:{SHOP_COLOR_ID}:d", None)
        self.assertIsNone(await self._shop_value(SHOP_A, "btn_style_wallet_topup"))
        self.assertTrue(callback.answer.await_args.kwargs.get("show_alert"))
        callback.message.edit_text.assert_not_awaited()

    async def test_owner_saves_to_platform_and_sees_the_new_color(self) -> None:
        group = self._group_index(ADMIN_ONLY_ID, for_reseller=False)
        callback = _callback(f"bcl:a:s:{group}:{ADMIN_ONLY_ID}:w")
        async with self.Session() as session:
            await ui_module.owner_colors(callback, session, MagicMock())
        self.assertEqual(await self._platform_value("btn_style_adm_dash"), "")
        self.assertIn("✓ ⚪", str(callback.message.edit_text.await_args))

    async def test_button_from_another_group_is_refused(self) -> None:
        wrong_group = self._group_index(SHOP_COLOR_ID, for_reseller=False) + 1
        callback = _callback(f"bcl:a:s:{wrong_group}:{SHOP_COLOR_ID}:d")
        async with self.Session() as session:
            await ui_module.owner_colors(callback, session, MagicMock())
        self.assertIsNone(await self._platform_value("btn_style_wallet_topup"))
        self.assertTrue(callback.answer.await_args.kwargs.get("show_alert"))


class SettingsEntryTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_menu_section_links_to_colors(self) -> None:
        from app.bot.handlers import admin_settings

        callback = _callback("adm:st:sec:menu")
        await admin_settings._render_section(callback, MagicMock(), "menu")
        markup = callback.message.edit_text.await_args.kwargs["reply_markup"]
        self.assertIn("bcl:a:g", [b.callback_data for row in markup.inline_keyboard for b in row])

    async def test_shop_menu_section_links_to_colors(self) -> None:
        from app.bot.handlers import reseller_settings

        callback = _callback("res:st:sec:menu")
        with patch.object(reseller_settings, "is_editor_enabled", AsyncMock(return_value=False)):
            await reseller_settings._render_section(callback, MagicMock(), "menu", SHOP_A)
        markup = callback.message.edit_text.await_args.kwargs["reply_markup"]
        self.assertIn("bcl:r:g", [b.callback_data for row in markup.inline_keyboard for b in row])


if __name__ == "__main__":
    unittest.main()
