"""Menu rows («▦ چیدمان منو») and order (⬆️/⬇️): the rules, the real menus, the editor flow and shop isolation."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot import nav_inline as hubs
from app.bot import reply_keyboards as rk
from app.bot.handlers import menu_rows_editor as rows_ui
from app.bot.handlers import screen_editor as editor_ui
from app.bot.row_layout import MenuBody, arrange_buttons, button_key, menu_screen
from app.bot.screen_registry import SCREENS, ScreenContext, screen_body
from app.db.models import ResellerSetting, Setting
from app.services.menu_rows import (
    MAX_LAYOUT_BUTTONS,
    MAX_ROW_BUTTONS,
    MenuOrder,
    arrange,
    decode_sizes,
    default_order,
    layout_fits,
    moved_order,
    narrow_row,
    order_values,
    rows_reset_value,
    parse_layout,
    serialize_layout,
    setting_key,
    split_rows,
    widen_row,
)
from app.services.screen_editor import SCREEN_EDITOR_SETTING, EditorActor, edit_mode_store
from app.services.users import DEFAULT_SETTINGS, clear_settings_cache, get_all_settings
from tests.postgres_support import add_shop_users, scoped_async_engine
from tests.test_bot_ui_keyboard_snapshots import default_ui, sample_reseller_profile

OWNER = EditorActor(user_id=7, shop_id=None)
SHOP_A = 601
SHOP_B = 602
SHOP_OWNER = EditorActor(user_id=8, shop_id=SHOP_A)


def _keys(markup: InlineKeyboardMarkup) -> list[list[str]]:
    return [[button_key(b) or b.text for b in row] for row in markup.inline_keyboard]


def _with_layout(screen_id: str, rows: list[list[str]]) -> dict[str, str]:
    ui = default_ui()
    ui[setting_key(screen_id)] = serialize_layout(rows)
    return ui


class ArrangeRuleTests(unittest.TestCase):
    DEFAULT = [["a", "b"], ["c", "d"], ["e", "f"], ["g", "h"]]

    def test_without_a_layout_the_default_rows_stay(self) -> None:
        self.assertEqual(arrange(self.DEFAULT, None), self.DEFAULT)

    def test_rows_of_three_then_two_then_three(self) -> None:
        layout = parse_layout(serialize_layout([["a", "b", "c"], ["d", "e"], ["f", "g", "h"]]))
        self.assertEqual(arrange(self.DEFAULT, layout), [["a", "b", "c"], ["d", "e"], ["f", "g", "h"]])

    def test_a_missing_button_only_shortens_its_own_row(self) -> None:
        layout = parse_layout(serialize_layout([["a", "b", "c"], ["d", "e"], ["f", "g", "h"]]))
        without_b = [["a"], ["c", "d"], ["e", "f"], ["g", "h"]]
        self.assertEqual(arrange(without_b, layout), [["a", "c"], ["d", "e"], ["f", "g", "h"]])
        without_e = [["a", "b"], ["c", "d"], ["f"], ["g", "h"]]
        self.assertEqual(arrange(without_e, layout), [["a", "b", "c"], ["d"], ["f", "g", "h"]])

    def test_a_row_whose_buttons_are_all_gone_disappears(self) -> None:
        layout = parse_layout(serialize_layout([["a", "b"], ["c"], ["d"]]))
        self.assertEqual(arrange([["a", "b"], ["d"]], layout), [["a", "b"], ["d"]])

    def test_a_button_the_layout_does_not_know_keeps_its_default_place(self) -> None:
        layout = parse_layout(serialize_layout([["a", "b", "c"]]))
        self.assertEqual(arrange([["a", "b"], ["c", "new"]], layout), [["a", "b", "c"], ["new"]])
        self.assertEqual(arrange([["new"], ["a", "b"], ["c"]], layout), [["new"], ["a", "b", "c"]])

    def test_a_menu_reordered_in_the_web_panel_falls_back_to_its_default_rows(self) -> None:
        layout = parse_layout(serialize_layout([["a", "b", "c"]]))
        reordered = [["c", "a"], ["b"]]
        self.assertEqual(arrange(reordered, layout, follow_saved_order=False), reordered)
        self.assertFalse(layout_fits(reordered, layout, follow_saved_order=False))

    def test_the_saved_order_wins_for_menus_ordered_in_the_bot(self) -> None:
        layout = parse_layout(serialize_layout([["c", "a"], ["b"]]))
        self.assertEqual(arrange([["a", "b"], ["c"]], layout), [["c", "a"], ["b"]])
        self.assertTrue(layout_fits([["a", "b"], ["c"]], layout, follow_saved_order=True))

    def test_a_new_button_stays_after_the_button_it_follows_by_default(self) -> None:
        layout = parse_layout(serialize_layout([["c", "a"], ["b"]]))
        self.assertEqual(arrange([["a", "new"], ["b", "c"]], layout), [["c", "a"], ["new"], ["b"]])
        self.assertEqual(arrange([["first", "a"], ["b", "c"]], layout), [["first"], ["c", "a"], ["b"]])

    def test_repeated_keys_fall_back_to_the_default_rows(self) -> None:
        layout = parse_layout(serialize_layout([["a", "b"]]))
        self.assertEqual(arrange([["a"], ["a", "b"]], layout), [["a"], ["a", "b"]])

    def test_damaged_or_hostile_values_are_ignored(self) -> None:
        too_many = serialize_layout([[f"k{i}"] for i in range(MAX_LAYOUT_BUTTONS + 1)])
        for raw in (
            "", "not json", "{}", "[]", "[[]]", '[["a","b","c","d","e"]]', '[["a"],["a"]]',
            '[[1]]', '[["a", null]]', '["a"]', serialize_layout([["x" * 65]]), too_many, "[" * 5000,
        ):
            with self.subTest(raw=raw[:40]):
                self.assertIsNone(parse_layout(raw))


class EditorStepRuleTests(unittest.TestCase):
    def test_plus_takes_the_first_button_of_the_next_row(self) -> None:
        self.assertEqual(widen_row([2, 2, 2], 0), [3, 1, 2])
        self.assertEqual(widen_row([2, 1], 0), [3])

    def test_minus_moves_the_last_button_to_the_next_row(self) -> None:
        self.assertEqual(narrow_row([3, 2], 0), [2, 3])
        self.assertEqual(narrow_row([2, 2], 1), [2, 1, 1])
        self.assertEqual(narrow_row([2, MAX_ROW_BUTTONS], 0), [1, 1, MAX_ROW_BUTTONS])

    def test_impossible_steps_are_refused(self) -> None:
        for change, sizes, index in (
            (widen_row, [2, 2], 1),
            (widen_row, [MAX_ROW_BUTTONS, 1], 0),
            (widen_row, [2], 5),
            (narrow_row, [1, 2], 0),
            (narrow_row, [2], -1),
        ):
            with self.subTest(change=change.__name__, sizes=sizes, index=index):
                with self.assertRaises(ValueError):
                    change(sizes, index)

    def test_drafts_that_do_not_fit_the_menu_are_refused(self) -> None:
        self.assertEqual(decode_sizes("32", total=5), [3, 2])
        for text, total in (("32", 6), ("05", 5), ("5", 5), ("x2", 3), ("۳۲", 5), ("", 0), ("1" * 25, 25)):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    decode_sizes(text, total=total)

    def test_split_rows_follows_the_sizes(self) -> None:
        self.assertEqual(split_rows(["a", "b", "c"], [1, 2]), [["a"], ["b", "c"]])
        with self.assertRaises(ValueError):
            split_rows(["a", "b"], [3])


class OrderRuleTests(unittest.TestCase):
    WALLET = MenuOrder("c.wallet", ("a", "b", "c"), (2, 1), (("a", "b"), ("c",)), has_saved_rows=False)
    MAIN = MenuOrder(
        "a.main", ("shop", "wallet", "support", "admin"), (2, 2), (("shop", "wallet"), ("support", "admin")),
        has_saved_rows=False, external_order=("shop", "services", "wallet", "support"),
    )

    def test_a_move_swaps_neighbours_only(self) -> None:
        self.assertEqual(moved_order(self.WALLET, 0, 1), ["b", "a", "c"])
        self.assertEqual(moved_order(self.WALLET, 2, 1), ["a", "c", "b"])
        for index, target in ((0, -1), (2, 3)):
            with self.subTest(edge=(index, target)), self.assertRaises(ValueError):
                moved_order(self.WALLET, index, target)
        for index, target in ((0, 2), (5, 4), (1, 1)):
            with self.subTest(forged=(index, target)), self.assertRaises(ValueError):
                moved_order(self.WALLET, index, target)

    def test_the_new_order_keeps_the_row_shapes(self) -> None:
        values = order_values(self.WALLET, ["c", "a", "b"])
        self.assertEqual(parse_layout(values[setting_key("c.wallet")]), (("c", "a"), ("b",)))
        self.assertEqual(order_values(self.WALLET, ["a", "b", "c"]), {setting_key("c.wallet"): ""})

    def test_a_web_ordered_menu_writes_the_web_setting_and_keeps_hidden_items_in_place(self) -> None:
        self.assertEqual(self.MAIN.movable, ("shop", "wallet", "support"))
        values = order_values(self.MAIN, ["wallet", "shop", "support"])
        self.assertEqual(values, {"menu_order": "wallet,services,shop,support"})
        saved = MenuOrder(**{**{f: getattr(self.MAIN, f) for f in MenuOrder.__slots__}, "has_saved_rows": True})
        self.assertEqual(
            parse_layout(order_values(saved, ["wallet", "shop", "support"])[setting_key("a.main")]),
            (("wallet", "shop"), ("support", "admin")),
        )

    def test_orders_that_do_not_match_the_menu_are_refused(self) -> None:
        for new in (["a", "b"], ["a", "b", "b"], ["a", "b", "x"]):
            with self.subTest(new=new), self.assertRaises(ValueError):
                order_values(self.WALLET, new)
        with self.assertRaises(ValueError):
            order_values(self.MAIN, ["admin", "shop", "wallet", "support"])

    def test_default_order(self) -> None:
        moved = MenuOrder("c.wallet", ("c", "a", "b"), (2, 1), (("a", "b"), ("c",)), has_saved_rows=True)
        self.assertEqual(default_order(moved, ()), ["a", "b", "c"])
        main = MenuOrder(
            "c.main", ("trial", "wallet", "shop"), (3,), (("trial", "wallet", "shop"),), has_saved_rows=False,
            external_order=("trial", "wallet", "hidden", "shop"),
        )
        # «trial» is not in the shipped list, so it keeps its slot; «hidden» is not shown, so it is untouched.
        self.assertEqual(default_order(main, ("shop", "hidden", "wallet")), ["trial", "shop", "wallet"])

    def test_resetting_rows_keeps_an_order_saved_in_the_bot(self) -> None:
        moved = MenuOrder("c.wallet", ("c", "a", "b"), (1, 1, 1), (("a", "b"), ("c",)), has_saved_rows=True)
        self.assertEqual(parse_layout(rows_reset_value(moved)), (("c", "a"), ("b",)))
        self.assertEqual(rows_reset_value(self.WALLET), "")
        self.assertEqual(rows_reset_value(self.MAIN), "")


class RealMenuTests(unittest.TestCase):
    def test_menus_without_a_saved_layout_are_unchanged(self) -> None:
        ui = default_ui()
        self.assertEqual(_keys(hubs.wallet_hub_keyboard(ui)), [["nv:w:topup", "nv:w:tx"], ["menu:home"]])
        with menu_screen(None):
            rows = arrange_buttons([["x"], ["y", "z"]], ui, key=str)
        self.assertEqual(rows, [["x"], ["y", "z"]])

    def test_saved_rows_change_only_the_menu_own_buttons(self) -> None:
        ui = _with_layout("a.settings", [["adm:st:sec:shop", "adm:st:sec:menu", "adm:st:sec:labels"]])
        rows = _keys(hubs.admin_settings_hub_keyboard(ui))
        default = _keys(hubs.admin_settings_hub_keyboard(default_ui()))
        self.assertEqual(rows[0], default[0])  # the search row stays first
        self.assertEqual(rows[-1], default[-1])  # «بازگشت» stays last
        self.assertEqual(rows[1], ["adm:st:sec:shop", "adm:st:sec:menu", "adm:st:sec:labels"])
        self.assertEqual(sorted(k for row in rows for k in row), sorted(k for row in default for k in row))

    def test_a_layout_belongs_to_one_menu(self) -> None:
        ui = _with_layout("c.wallet", [["nv:w:topup"], ["nv:w:tx"]])
        self.assertEqual(_keys(hubs.wallet_hub_keyboard(ui))[:2], [["nv:w:topup"], ["nv:w:tx"]])
        self.assertEqual(_keys(hubs.loyalty_hub_keyboard(ui)), _keys(hubs.loyalty_hub_keyboard(default_ui())))

    def test_service_card_layout_applies_to_every_service(self) -> None:
        ui = _with_layout("c.service", [["svc:renew:#"], ["svc:addon:#", "svc:link:#", "guide:svc:#"], ["svc:more:#"]])
        rows = hubs.service_card_keyboard(42, ui).inline_keyboard
        self.assertEqual([[b.callback_data for b in row] for row in rows[:3]], [
            ["svc:renew:42"], ["svc:addon:42", "svc:link:42", "guide:svc:42"], ["svc:more:42"],
        ])

    def test_hidden_button_shortens_its_row_at_run_time(self) -> None:
        ui = _with_layout("c.support", [["faq:l", "nv:s:new", "nv:s:list"]])
        self.assertEqual(_keys(hubs.support_hub_keyboard(ui, show_faq=True))[0], ["faq:l", "nv:s:new", "nv:s:list"])
        self.assertEqual(_keys(hubs.support_hub_keyboard(ui, show_faq=False))[0], ["nv:s:new", "nv:s:list"])

    def test_main_menu_rows_for_customers_and_admins_are_separate(self) -> None:
        ui = _with_layout("c.main", [["shop", "services", "wallet"], ["support", "loyalty"]])
        customer = rk.main_reply_keyboard("user", has_services=True, ui=ui).keyboard
        admin = rk.main_reply_keyboard("admin", has_services=True, ui=ui).keyboard
        default_admin = rk.main_reply_keyboard("admin", has_services=True, ui=default_ui()).keyboard
        self.assertEqual([len(row) for row in customer[:2]], [3, 2])
        self.assertEqual([[b.text for b in row] for row in admin], [[b.text for b in row] for row in default_admin])

    def test_admin_home_keeps_its_queue_shortcuts_outside_the_layout(self) -> None:
        body = screen_body("a.home", ScreenContext(ui=default_ui()))
        assert body is not None
        ui = _with_layout("a.home", [list(body.keys[:3])])
        rows = _keys(hubs.admin_groups_hub_keyboard(ui, pending_payments=2))
        self.assertEqual(rows[1], list(body.keys[:3]))
        self.assertNotIn(rows[0][0], body.keys)

    def test_every_editor_callback_fits_telegram_limit(self) -> None:
        ctx = ScreenContext(ui=default_ui(), profile=sample_reseller_profile())
        for screen_id in SCREENS:
            for actor in (OWNER, SHOP_OWNER):
                body = screen_body(screen_id, ctx)
                if body is None:
                    continue
                draft = [1] * len(body.keys)
                _, markup = rows_ui.rows_view(actor, SCREENS[screen_id], body, draft)
                with self.subTest(screen=screen_id):
                    for row in markup.inline_keyboard:
                        for button in row:
                            self.assertLessEqual(len(button.callback_data.encode()), 64)
                            self.assertIsNotNone(rows_ui.parse_request(button.callback_data)
                                                 or button.callback_data.startswith("ed:"))

    def test_button_texts_are_escaped_in_the_editor(self) -> None:
        body = MenuBody("c.wallet", ("a", "b"), ("<b>x</b>", "&y"), (("a", "b"),), (("a", "b"),))
        text, _ = rows_ui.rows_view(OWNER, SCREENS["c.wallet"], body, [2])
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", text)
        self.assertIn("&amp;y", text)
        self.assertNotIn("<b>x</b>", text)

    def test_malformed_callbacks_are_rejected(self) -> None:
        for data in (
            "edl:a:o:no.such", "edl:x:o:c.wallet", "edl:a:p:c.wallet:2", "edl:a:p:c.wallet:20:0:1",
            "edl:a:s:c.wallet", "edl:a:o:c.wallet:2", "edl:a:p:c.wallet:25:0", "edl:a:x:c.wallet", "", None,
            "edl:a:v:c.wallet:0:1", "edl:a:v:c.wallet:0:1:XYZ123", "edl:a:v:no.such:0:1:abcdef",
        ):
            with self.subTest(data=data):
                self.assertIsNone(rows_ui.parse_request(data))


def _callback(data: str) -> MagicMock:
    callback = MagicMock()
    callback.data = data
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()
    return callback


class EditorFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from app.db import Base
        import app.db.models  # noqa: F401  (registers every table on Base)

        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        await add_shop_users(self.Session, SHOP_A, SHOP_B)
        self.state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))
        clear_settings_cache()

    async def asyncTearDown(self) -> None:
        edit_mode_store.deactivate(OWNER)
        edit_mode_store.deactivate(SHOP_OWNER)
        clear_settings_cache()
        await self.engine.dispose()

    async def _owner(self, data: str) -> MagicMock:
        callback = _callback(data)
        async with self.Session() as session:
            await rows_ui.owner_rows(callback, session, MagicMock(id=OWNER.user_id))
        return callback

    async def _shop(self, data: str, *, on_shop_bot: bool = True) -> MagicMock:
        callback = _callback(data)
        with patch.object(editor_ui, "shop_settings_shop_id", AsyncMock(return_value=SHOP_A)):
            async with self.Session() as session:
                await rows_ui.shop_rows(
                    callback, session, MagicMock(id=SHOP_OWNER.user_id),
                    is_reseller_bot=on_shop_bot, reseller_owner_id=SHOP_A,
                )
        return callback

    async def _panel(self, data: str) -> MagicMock:
        callback = _callback(data)
        async with self.Session() as session:
            await editor_ui.owner_editor(callback, session, MagicMock(id=OWNER.user_id), self.state)
        return callback

    async def _start_editing(self) -> None:
        async with self.Session() as session:
            session.add(Setting(key=SCREEN_EDITOR_SETTING, value="1"))
            await session.commit()
        clear_settings_cache()
        edit_mode_store.activate(OWNER)
        edit_mode_store.activate(SHOP_OWNER)

    async def _platform(self, key: str) -> str | None:
        async with self.Session() as session:
            return await session.scalar(select(Setting.value).where(Setting.key == key))

    async def _shop_value(self, shop_id: int, key: str) -> str | None:
        async with self.Session() as session:
            return await session.scalar(
                select(ResellerSetting.value).where(ResellerSetting.reseller_user_id == shop_id, ResellerSetting.key == key)
            )

    @staticmethod
    def _shown(callback: MagicMock) -> tuple[str, InlineKeyboardMarkup]:
        args, kwargs = callback.message.edit_text.await_args
        return args[0], kwargs["reply_markup"]

    @staticmethod
    def _alerted(callback: MagicMock) -> bool:
        return bool(callback.answer.await_args and callback.answer.await_args.kwargs.get("show_alert"))

    @staticmethod
    def _data(markup: InlineKeyboardMarkup) -> list[str]:
        return [b.callback_data for row in markup.inline_keyboard for b in row]

    async def test_edit_panel_links_to_the_layout_and_back_returns_to_it(self) -> None:
        await self._start_editing()
        _, panel = self._shown(await self._panel("ed:a:o:c.wallet"))
        self.assertIn("edl:a:o:c.wallet", self._data(panel))
        _, shop_panel = self._shown(await self._panel("ed:a:o:c.shop"))
        # The shop's first step has its own list tools (columns), but no «▦ چیدمان منو».
        self.assertFalse(any(d.startswith("edl:a:o:") for d in self._data(shop_panel)))
        _, view = self._shown(await self._owner("edl:a:o:c.wallet"))
        self.assertEqual(view.inline_keyboard[-1][0].callback_data, "ed:a:o:c.wallet")
        _, back = self._shown(await self._panel(view.inline_keyboard[-1][0].callback_data))
        self.assertIn("edl:a:o:c.wallet", self._data(back))

    async def test_layout_view_colors_save_and_reset_and_reads_row_sizes_clearly(self) -> None:
        await self._start_editing()
        _, view = self._shown(await self._owner("edl:a:o:a.settings"))
        actions = next(row for row in view.inline_keyboard if row[0].text == rows_ui.BTN_SAVE)
        self.assertEqual([(b.text, b.style) for b in actions], [(rows_ui.BTN_SAVE, "success"), (rows_ui.BTN_RESET, "danger")])
        self.assertEqual(view.inline_keyboard[0][1].text, "ردیف ۱: ۲ ستون")

    async def test_plus_and_minus_change_only_the_draft_until_saved(self) -> None:
        await self._start_editing()
        text, view = self._shown(await self._owner("edl:a:m:c.wallet:2:0"))
        self.assertIn(rows_ui.TEXT_UNSAVED, text)
        self.assertIn("edl:a:s:c.wallet:11", self._data(view))
        self.assertIsNone(await self._platform(setting_key("c.wallet")))
        await self._owner("edl:a:s:c.wallet:11")
        self.assertEqual(await self._platform(setting_key("c.wallet")), '[["nv:w:topup"],["nv:w:tx"]]')
        async with self.Session() as session:
            ui = await get_all_settings(session)
        self.assertEqual(_keys(hubs.wallet_hub_keyboard(ui))[:2], [["nv:w:topup"], ["nv:w:tx"]])

    async def test_saving_twice_or_saving_the_default_rows_is_safe(self) -> None:
        await self._start_editing()
        await self._owner("edl:a:s:c.wallet:11")
        await self._owner("edl:a:s:c.wallet:11")
        self.assertEqual(await self._platform(setting_key("c.wallet")), '[["nv:w:topup"],["nv:w:tx"]]')
        await self._owner("edl:a:s:c.wallet:2")
        self.assertEqual(await self._platform(setting_key("c.wallet")), "")
        await self._owner("edl:a:s:c.wallet:11")
        callback = await self._owner("edl:a:r:c.wallet")
        self.assertEqual(callback.answer.await_args.args[0], rows_ui.TEXT_RESET)
        self.assertEqual(await self._platform(setting_key("c.wallet")), "")

    async def test_a_draft_that_no_longer_fits_the_menu_is_refused(self) -> None:
        await self._start_editing()
        for data in ("edl:a:s:c.wallet:111", "edl:a:p:c.wallet:2:0", "edl:a:m:c.wallet:11:0"):
            with self.subTest(data=data):
                self.assertTrue(self._alerted(await self._owner(data)))
        self.assertIsNone(await self._platform(setting_key("c.wallet")))

    async def test_nothing_changes_without_edit_mode(self) -> None:
        self.assertTrue(self._alerted(await self._owner("edl:a:s:c.wallet:11")))
        await self._start_editing()
        edit_mode_store.deactivate(OWNER)
        self.assertTrue(self._alerted(await self._owner("edl:a:s:c.wallet:11")))
        self.assertIsNone(await self._platform(setting_key("c.wallet")))

    async def test_menus_without_editable_rows_are_refused(self) -> None:
        await self._start_editing()
        self.assertTrue(self._alerted(await self._owner("edl:a:o:c.shop")))
        self.assertTrue(self._alerted(await self._owner("edl:a:r:c.services")))
        self.assertIsNone(await self._platform(setting_key("c.services")))

    async def test_shop_owner_changes_only_their_own_shop(self) -> None:
        await self._start_editing()
        await self._shop("edl:r:s:c.wallet:11")
        self.assertEqual(await self._shop_value(SHOP_A, setting_key("c.wallet")), '[["nv:w:topup"],["nv:w:tx"]]')
        self.assertIsNone(await self._shop_value(SHOP_B, setting_key("c.wallet")))
        self.assertIsNone(await self._platform(setting_key("c.wallet")))
        async with self.Session() as session:
            platform_ui = await get_all_settings(session)
            shop_b_ui = await get_all_settings(session, reseller_id=SHOP_B)
        self.assertEqual(_keys(hubs.wallet_hub_keyboard(platform_ui))[0], ["nv:w:topup", "nv:w:tx"])
        self.assertEqual(_keys(hubs.wallet_hub_keyboard(shop_b_ui))[0], ["nv:w:topup", "nv:w:tx"])

    async def test_shop_owner_cannot_reach_admin_menus_or_the_owner_view(self) -> None:
        await self._start_editing()
        self.assertTrue(self._alerted(await self._shop("edl:r:s:a.home:2222")))
        self.assertTrue(self._alerted(await self._shop("edl:r:o:a.settings")))
        self.assertTrue(self._alerted(await self._shop("edl:a:o:c.wallet")))
        self.assertTrue(self._alerted(await self._shop("edl:r:s:c.wallet:11", on_shop_bot=False)))
        self.assertIsNone(await self._shop_value(SHOP_A, setting_key("a.home")))
        self.assertIsNone(await self._shop_value(SHOP_A, setting_key("c.wallet")))

    async def test_owner_saves_admin_and_customer_main_menus_separately(self) -> None:
        await self._start_editing()
        _, view = self._shown(await self._owner("edl:a:o:c.main"))
        self.assertIn("edl:a:s:c.main:222", self._data(view))
        await self._owner("edl:a:s:c.main:321")
        self.assertIsNone(await self._platform(setting_key("a.main")))
        async with self.Session() as session:
            ui = await get_all_settings(session)
        customer = rk.main_reply_keyboard("user", has_services=True, ui=ui).keyboard
        self.assertEqual([len(row) for row in customer[:3]], [3, 2, 1])
        self.assertEqual(DEFAULT_SETTINGS.get(setting_key("c.main"), ""), "")

    # ── Order: ⬆️/⬇️ in the edit panel ──

    async def _panel_markup(self, screen_id: str) -> InlineKeyboardMarkup:
        return self._shown(await self._panel(f"ed:a:o:{screen_id}"))[1]

    @staticmethod
    def _move_data(markup: InlineKeyboardMarkup, index: int, target: int) -> str:
        prefix = f":{index}:{target}:"
        return next(d for d in EditorFlowTests._data(markup) if d.startswith("edl:") and ":v:" in d and prefix in d)

    async def test_panel_shows_arrows_beside_the_menu_own_buttons_only(self) -> None:
        await self._start_editing()
        markup = await self._panel_markup("c.support")
        sides = {editor_ui.BTN_UP, editor_ui.BTN_DOWN, editor_ui.BTN_NO_MOVE}
        arrow_rows = [row for row in markup.inline_keyboard if len(row) == 3 and row[0].text in sides]
        # ➖ stands where an arrow would lead nowhere: no ⬆️ on the first button, no ⬇️ on the last.
        self.assertEqual([(row[0].text, row[2].text) for row in arrow_rows], [
            (editor_ui.BTN_NO_MOVE, editor_ui.BTN_DOWN),
            (editor_ui.BTN_UP, editor_ui.BTN_DOWN),
            (editor_ui.BTN_UP, editor_ui.BTN_NO_MOVE),
        ])
        self.assertEqual(arrow_rows[0][0].callback_data, "edl:a:e:c.support")
        self.assertEqual(arrow_rows[-1][2].callback_data, "edl:a:e:c.support")
        back_label = default_ui()["btn_back"]
        back = next(row for row in markup.inline_keyboard if any(b.text.endswith(back_label) for b in row))
        self.assertEqual(len(back), 1)  # «بازگشت» is fixed: no arrows
        tools = next(row for row in markup.inline_keyboard if row[0].text == editor_ui.BTN_ORDER_RESET)
        self.assertEqual([(b.text, b.style) for b in tools], [
            (editor_ui.BTN_ORDER_RESET, "danger"), (editor_ui.BTN_MENU_ROWS, "primary"),
        ])

    async def test_a_move_is_saved_and_shown_to_customers(self) -> None:
        await self._start_editing()
        callback = await self._owner(self._move_data(await self._panel_markup("c.wallet"), 0, 1))
        self.assertEqual(callback.answer.await_args.args[0], rows_ui.TEXT_MOVED)
        self.assertEqual(await self._platform(setting_key("c.wallet")), '[["nv:w:tx","nv:w:topup"]]')
        async with self.Session() as session:
            ui = await get_all_settings(session)
        self.assertEqual(_keys(hubs.wallet_hub_keyboard(ui))[0], ["nv:w:tx", "nv:w:topup"])

    async def test_a_double_tap_moves_a_button_one_slot_only(self) -> None:
        await self._start_editing()
        data = self._move_data(await self._panel_markup("a.settings"), 0, 1)
        await self._owner(data)
        self.assertTrue(self._alerted(await self._owner(data)))
        async with self.Session() as session:
            ui = await get_all_settings(session)
        body = screen_body("a.settings", ScreenContext(ui=ui))
        assert body is not None
        default = screen_body("a.settings", ScreenContext(ui=default_ui()))
        assert default is not None
        self.assertEqual(list(body.keys[:3]), [default.keys[1], default.keys[0], default.keys[2]])

    async def test_an_older_message_cannot_recolor_the_wrong_button(self) -> None:
        await self._start_editing()
        old_panel = await self._panel_markup("c.wallet")
        old_color = next(d for d in self._data(old_panel) if d.startswith("ed:a:b:"))
        await self._owner(self._move_data(old_panel, 0, 1))
        index, state = old_color.split(":")[4], old_color.split(":")[5]
        stale = await self._panel(f"ed:a:c:c.wallet:{index}:d:{state}")
        self.assertTrue(self._alerted(stale))
        self.assertIsNone(await self._platform("btn_style_wallet_topup"))
        self.assertIsNone(await self._platform("btn_style_wallet_tx"))

    async def test_moves_keep_the_saved_row_shapes(self) -> None:
        await self._start_editing()
        await self._owner("edl:a:s:a.settings:3332")
        await self._owner(self._move_data(await self._panel_markup("a.settings"), 0, 1))
        rows = parse_layout(await self._platform(setting_key("a.settings")))
        assert rows is not None
        self.assertEqual([len(row) for row in rows], [3, 3, 3, 2])

    async def test_main_menu_order_is_the_web_panel_menu_order(self) -> None:
        await self._start_editing()
        before = DEFAULT_SETTINGS["menu_order"].split(",")
        await self._owner(self._move_data(await self._panel_markup("a.main"), 0, 1))
        stored = (await self._platform("menu_order") or "").split(",")
        self.assertEqual(stored[:2], [before[1], before[0]])
        # «درخواست نمایندگی» is not on the admin's menu, yet it keeps its place in the web setting.
        self.assertEqual(stored.index("reseller_apply"), before.index("reseller_apply"))
        self.assertIsNone(await self._platform(setting_key("a.main")))
        async with self.Session() as session:
            ui = await get_all_settings(session)
        customer = rk.main_reply_keyboard("user", has_services=True, ui=ui).keyboard
        admin = rk.main_reply_keyboard("admin", has_services=True, ui=ui).keyboard
        self.assertEqual(customer[0][0].text, ui["btn_services"])
        self.assertEqual(admin[-2][-1].text, ui["btn_admin"])  # role buttons stay last, above «شروع مجدد»

    async def test_role_buttons_have_no_arrows(self) -> None:
        await self._start_editing()
        markup = await self._panel_markup("a.main")
        admin_label = default_ui()["btn_admin"]
        admin_row = next(row for row in markup.inline_keyboard if any(admin_label in b.text for b in row))
        self.assertEqual(len(admin_row), 1)

    async def test_web_panel_reorder_sets_saved_rows_aside_and_says_so(self) -> None:
        await self._start_editing()
        await self._owner("edl:a:s:c.main:33")
        async with self.Session() as session:
            row = await session.scalar(select(Setting).where(Setting.key == "menu_order"))
            if row is None:
                session.add(Setting(key="menu_order", value="wallet,shop,services,support,loyalty,reseller_apply"))
            else:
                row.value = "wallet,shop,services,support,loyalty,reseller_apply"
            await session.commit()
        clear_settings_cache()
        text, _ = self._shown(await self._owner("edl:a:o:c.main"))
        self.assertIn(rows_ui.TEXT_STALE_LAYOUT, text)
        async with self.Session() as session:
            ui = await get_all_settings(session)
        customer = rk.main_reply_keyboard("user", has_services=True, ui=ui).keyboard
        self.assertEqual([len(r) for r in customer[:3]], [2, 2, 2])

    async def test_club_order_is_the_web_panel_club_order(self) -> None:
        await self._start_editing()
        await self._owner(self._move_data(await self._panel_markup("c.loyalty"), 0, 1))
        stored = (await self._platform("loyalty_submenu_order") or "").split(",")
        default = DEFAULT_SETTINGS["loyalty_submenu_order"].split(",")
        self.assertEqual(stored[:2], [default[1], default[0]])

    async def test_order_reset_returns_to_the_default_and_is_safe_to_repeat(self) -> None:
        await self._start_editing()
        await self._owner(self._move_data(await self._panel_markup("c.wallet"), 0, 1))
        callback = await self._owner("edl:a:z:c.wallet")
        self.assertEqual(callback.answer.await_args.args[0], rows_ui.TEXT_ORDER_RESET)
        self.assertEqual(await self._platform(setting_key("c.wallet")), "")
        again = await self._owner("edl:a:z:c.wallet")
        self.assertEqual(again.answer.await_args.args[0], rows_ui.TEXT_ORDER_ALREADY_DEFAULT)
        await self._owner(self._move_data(await self._panel_markup("c.main"), 1, 2))
        await self._owner("edl:a:z:c.main")
        self.assertEqual(await self._platform("menu_order"), DEFAULT_SETTINGS["menu_order"])

    async def test_edge_arrows_and_moves_without_edit_mode_change_nothing(self) -> None:
        await self._start_editing()
        data = self._move_data(await self._panel_markup("c.wallet"), 0, 1)
        edge = await self._owner("edl:a:e:c.wallet")
        self.assertEqual(edge.answer.await_args.args[0], rows_ui.TEXT_ORDER_EDGE)
        edit_mode_store.deactivate(OWNER)
        self.assertTrue(self._alerted(await self._owner(data)))
        self.assertIsNone(await self._platform(setting_key("c.wallet")))

    async def test_shop_owner_reorders_only_their_own_shop(self) -> None:
        await self._start_editing()
        async with self.Session() as session:
            with patch.object(editor_ui, "shop_settings_shop_id", AsyncMock(return_value=SHOP_A)):
                panel = _callback("ed:r:o:c.main")
                await editor_ui.shop_editor(
                    panel, session, MagicMock(id=SHOP_OWNER.user_id), self.state,
                    is_reseller_bot=True, reseller_owner_id=SHOP_A,
                )
        data = self._move_data(self._shown(panel)[1], 0, 1)
        self.assertTrue(data.startswith("edl:r:v:c.main:"))
        await self._shop(data)
        self.assertIsNotNone(await self._shop_value(SHOP_A, "menu_order"))
        self.assertIsNone(await self._shop_value(SHOP_B, "menu_order"))
        self.assertIsNone(await self._platform("menu_order"))
        self.assertTrue(self._alerted(await self._shop("edl:r:z:a.home")))
        self.assertTrue(self._alerted(await self._shop(data.replace("c.main", "a.home"))))

