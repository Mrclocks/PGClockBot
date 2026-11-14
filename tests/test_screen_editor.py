"""In-menu screen editor: edit mode, the edit row, permissions, shop isolation and the flow."""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, MessageEntity, Update, User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot import keyboards as kb
from app.bot import menu_nav as nav
from app.bot.edit_mode import EDIT_ROW_TEXT, EditModeMiddleware, editing_as
from app.bot.handlers import screen_editor as ui_module
from app.bot.screen_registry import SCREENS, ScreenContext, screen_buttons
from app.db.models import BotUser, ResellerSetting, Setting
from app.services.screen_editor import (
    SCREEN_EDITOR_SETTING,
    EditModeStore,
    EditorActor,
    can_edit_label,
    edit_mode_store,
    validate_button_label,
)
from app.services.rich_text import button_icon_custom_emoji_id
from app.services.users import DEFAULT_SETTINGS, clear_settings_cache
from tests.test_bot_ui_keyboard_snapshots import default_ui, sample_reseller_profile

OWNER = EditorActor(user_id=7, shop_id=None)
SHOP_A = 501
SHOP_B = 502
SHOP_OWNER = EditorActor(user_id=8, shop_id=SHOP_A)


class EditModeStoreTests(unittest.TestCase):
    def test_switch_expires_after_its_window(self) -> None:
        store = EditModeStore(minutes=30)
        store.activate(OWNER, now=1000.0)
        self.assertTrue(store.is_active(OWNER, now=1000.0 + 29 * 60))
        self.assertEqual(store.minutes_left(OWNER, now=1000.0 + 10 * 60), 20)
        self.assertFalse(store.is_active(OWNER, now=1000.0 + 30 * 60))

    def test_a_change_restarts_the_window_but_never_revives_an_expired_switch(self) -> None:
        store = EditModeStore(minutes=30)
        store.activate(OWNER, now=0.0)
        self.assertEqual(store.minutes_left(OWNER, now=0.0), 30)
        self.assertTrue(store.extend(OWNER, now=25 * 60.0))
        self.assertTrue(store.is_active(OWNER, now=50 * 60.0))
        self.assertFalse(store.extend(OWNER, now=60 * 60.0))
        self.assertFalse(store.is_active(OWNER, now=60 * 60.0))

    def test_switch_is_per_person_and_per_shop(self) -> None:
        store = EditModeStore()
        store.activate(SHOP_OWNER, now=0.0)
        self.assertFalse(store.is_active(EditorActor(user_id=8, shop_id=None), now=1.0))
        self.assertFalse(store.is_active(EditorActor(user_id=8, shop_id=SHOP_B), now=1.0))
        store.deactivate(SHOP_OWNER)
        self.assertFalse(store.is_active(SHOP_OWNER, now=1.0))

    def test_store_never_grows_past_its_capacity(self) -> None:
        store = EditModeStore(capacity=3)
        for user_id in range(10):
            store.activate(EditorActor(user_id=user_id, shop_id=None), now=float(user_id))
        active = [u for u in range(10) if store.is_active(EditorActor(user_id=u, shop_id=None), now=10.0)]
        self.assertEqual(active, [7, 8, 9])


class LabelRuleTests(unittest.TestCase):
    def test_label_validation(self) -> None:
        self.assertEqual(validate_button_label("  کیف   پول  ", other_labels=[], reserved=[]), "کیف پول")
        for text, others, reserved in (
            ("", [], []),
            ("خط اول\nخط دوم", [], []),
            ("x" * 65, [], []),
            ("انصراف", [], ["انصراف"]),
            ("🟢 خرید", ["🟢 خرید"], []),
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                validate_button_label(text, other_labels=others, reserved=reserved)

    def test_owner_and_shop_label_permissions(self) -> None:
        self.assertTrue(can_edit_label(OWNER, "btn_admin"))
        self.assertFalse(can_edit_label(SHOP_OWNER, "btn_admin"))
        self.assertTrue(can_edit_label(SHOP_OWNER, "btn_wallet"))
        self.assertFalse(can_edit_label(OWNER, "welcome_text"))
        self.assertFalse(can_edit_label(OWNER, None))


class EditRowTests(unittest.TestCase):
    def _last_row(self, screen_id: str, actor: EditorActor | None) -> list:
        ctx = ScreenContext(ui=default_ui(), profile=sample_reseller_profile())
        with editing_as(actor):
            markup = SCREENS[screen_id].build(ctx)
        if not isinstance(markup, InlineKeyboardMarkup):
            return []
        return markup.inline_keyboard[-1]

    def test_every_inline_screen_offers_its_own_editor_to_allowed_people(self) -> None:
        for screen_id, spec in SCREENS.items():
            if screen_id in {"c.main", "a.main"}:
                continue  # reply keyboards; reached from «✏️ ویرایش منوی اصلی»
            for actor, code in ((OWNER, "a"), (SHOP_OWNER, "r")):
                row = self._last_row(screen_id, actor)
                allowed = spec.audience.value in actor.audiences
                with self.subTest(screen=screen_id, actor=code):
                    if allowed:
                        self.assertEqual(row[0].text, EDIT_ROW_TEXT)
                        self.assertEqual(row[0].callback_data, f"ed:{code}:o:{screen_id}")
                    else:
                        self.assertNotEqual(row[0].text, EDIT_ROW_TEXT)

    def test_no_edit_row_without_an_active_editor(self) -> None:
        for screen_id in SCREENS:
            for button in self._last_row(screen_id, None):
                self.assertNotEqual(button.text, EDIT_ROW_TEXT)

    def test_editor_lists_buttons_without_its_own_row(self) -> None:
        with editing_as(OWNER):
            texts = [b.text for b in screen_buttons("c.wallet", ScreenContext(ui=default_ui()))]
        self.assertNotIn(EDIT_ROW_TEXT, texts)


def _bot_routers() -> list:
    """Every Router defined in the bot packages, read without attaching them to a dispatcher."""
    import importlib
    import pkgutil

    from aiogram import Router

    import app.bot
    import app.bot.handlers

    routers: list[Router] = []
    for package in (app.bot, app.bot.handlers):
        for info in pkgutil.iter_modules(package.__path__):
            module = importlib.import_module(f"{package.__name__}.{info.name}")
            routers.extend(v for v in vars(module).values() if isinstance(v, Router) and v not in routers)
    return routers


class ReopenCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_screen_returns_through_a_registered_handler(self) -> None:
        routers = _bot_routers()
        user = User(id=1, is_bot=False, first_name="t")
        for screen_id, spec in SCREENS.items():
            event = CallbackQuery(id="1", from_user=user, chat_instance="1", data=spec.reopen_callback)
            matched = False
            for router in routers:
                for handler in router.callback_query.handlers:
                    try:
                        ok, _ = await handler.check(event)
                    except Exception:  # filters that need runtime data cannot match a bare event
                        ok = False
                    if ok:
                        matched = True
                        break
                if matched:
                    break
            with self.subTest(screen=screen_id, callback=spec.reopen_callback):
                self.assertTrue(matched)


class MiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def _seen_editor(self, *, active: bool, feature_on: bool, preview: bool) -> EditorActor | None:
        from app.bot.edit_mode import active_editor

        state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))
        if preview:
            await state.update_data({nav.USER_PREVIEW_ACTIVE: True})
        actor = EditorActor(user_id=99, shop_id=None)
        if active:
            edit_mode_store.activate(actor)
        seen: list[EditorActor | None] = []

        async def handler(_event, _data):
            seen.append(active_editor())

        data = {"db_user": MagicMock(id=99), "is_reseller_bot": False, "session": MagicMock(), "state": state}
        try:
            with patch("app.bot.edit_mode.is_editor_enabled", AsyncMock(return_value=feature_on)):
                await EditModeMiddleware()(handler, MagicMock(), data)
        finally:
            edit_mode_store.deactivate(actor)
        return seen[0]

    async def test_editor_is_published_only_when_switched_on_enabled_and_not_previewing(self) -> None:
        self.assertEqual(await self._seen_editor(active=True, feature_on=True, preview=False), EditorActor(99, None))
        self.assertIsNone(await self._seen_editor(active=False, feature_on=True, preview=False))
        self.assertIsNone(await self._seen_editor(active=True, feature_on=False, preview=False))
        self.assertIsNone(await self._seen_editor(active=True, feature_on=True, preview=True))


def _callback(data: str) -> MagicMock:
    callback = MagicMock()
    callback.data = data
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()
    return callback


PREMIUM_EMOJI_ID = "5368324170671202286"


def _premium_entities(text: str) -> list[MessageEntity]:
    """The leading emoji of ``text`` sent as a Telegram Premium custom emoji."""
    return [MessageEntity(type="custom_emoji", offset=0, length=len(text.split(" ", 1)[0].encode("utf-16-le")) // 2,
                          custom_emoji_id=PREMIUM_EMOJI_ID)]


def _message(text: str, *, premium: bool = False) -> MagicMock:
    message = MagicMock()
    message.text = text
    message.entities = _premium_entities(text) if premium else None
    message.answer = AsyncMock()
    return message


class EditorFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from app.db import Base
        import app.db.models  # noqa: F401  (registers every table on Base)

        self._tmpdir = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmpdir.name) / 'editor.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))
        clear_settings_cache()

    async def asyncTearDown(self) -> None:
        edit_mode_store.deactivate(OWNER)
        edit_mode_store.deactivate(SHOP_OWNER)
        clear_settings_cache()
        await self.engine.dispose()
        self._tmpdir.cleanup()

    async def _owner(self, data: str) -> MagicMock:
        callback = _callback(data)
        async with self.Session() as session:
            await ui_module.owner_editor(callback, session, MagicMock(id=OWNER.user_id), self.state)
        return callback

    async def _shop(self, data: str, *, on_shop_bot: bool = True, has_access: bool = True) -> MagicMock:
        callback = _callback(data)
        with patch.object(ui_module, "shop_settings_shop_id", AsyncMock(return_value=SHOP_A if has_access else None)):
            async with self.Session() as session:
                await ui_module.shop_editor(
                    callback, session, MagicMock(id=SHOP_OWNER.user_id), self.state,
                    is_reseller_bot=on_shop_bot, reseller_owner_id=SHOP_A,
                )
        return callback

    async def _platform(self, key: str) -> str | None:
        async with self.Session() as session:
            return await session.scalar(select(Setting.value).where(Setting.key == key))

    async def _shop_value(self, shop_id: int, key: str) -> str | None:
        async with self.Session() as session:
            return await session.scalar(
                select(ResellerSetting.value).where(ResellerSetting.reseller_user_id == shop_id, ResellerSetting.key == key)
            )

    def _index(self, screen_id: str, label_key: str) -> int:
        buttons = screen_buttons(screen_id, ScreenContext(ui=dict(DEFAULT_SETTINGS), profile=sample_reseller_profile()))
        return next(i for i, b in enumerate(buttons) if b.label_key == label_key)

    async def _start_editing(self) -> None:
        await self._owner("ed:a:f1")
        await self._owner("ed:a:m1")
        await self._shop("ed:r:m1")

    def _alerted(self, callback: MagicMock) -> bool:
        return bool(callback.answer.await_args and callback.answer.await_args.kwargs.get("show_alert"))

    async def test_edit_mode_needs_the_feature_switch(self) -> None:
        self.assertTrue(self._alerted(await self._owner("ed:a:m1")))
        self.assertFalse(edit_mode_store.is_active(OWNER))
        await self._owner("ed:a:f1")
        self.assertEqual(await self._platform(SCREEN_EDITOR_SETTING), "1")
        await self._owner("ed:a:m1")
        self.assertTrue(edit_mode_store.is_active(OWNER))
        await self._owner("ed:a:f0")
        self.assertFalse(edit_mode_store.is_active(OWNER))

    async def test_switching_edit_mode_tells_how_long_it_stays_on(self) -> None:
        await self._owner("ed:a:f1")
        on = await self._owner("ed:a:m1")
        self.assertIn("30 دقیقه", on.answer.await_args.args[0])
        text = on.message.edit_text.await_args.args[0]
        self.assertIn("30 دقیقه تغییری ندهید", text)
        off = await self._owner("ed:a:m0")
        self.assertEqual(off.answer.await_args.args[0], ui_module.TEXT_EDIT_MODE_OFF)
        main_menu = [b.callback_data for row in off.message.edit_text.await_args.kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertNotIn("ed:a:o:c.main", main_menu)

    async def test_menu_panel_shows_the_minutes_left(self) -> None:
        await self._start_editing()
        callback = await self._owner("ed:a:o:c.wallet")
        self.assertIn("⏱ حالت ویرایش تا 30 دقیقهٔ دیگر روشن است.", callback.message.edit_text.await_args.args[0])

    async def test_old_editor_buttons_explain_that_edit_mode_expired(self) -> None:
        await self._start_editing()
        index = self._index("c.wallet", "btn_wallet_topup")
        edit_mode_store.deactivate(OWNER)
        for data in ("ed:a:o:c.wallet", f"ed:a:c:c.wallet:{index}:d", f"ed:a:t:c.wallet:{index}", "ed:a:n:c.wallet"):
            with self.subTest(data=data):
                callback = await self._owner(data)
                self.assertTrue(self._alerted(callback))
                self.assertEqual(callback.answer.await_args.args[0], ui_module.TEXT_EDIT_MODE_EXPIRED)
        self.assertIsNone(await self._platform("btn_style_wallet_topup"))
        self.assertIsNone(await self.state.get_state())

    async def test_each_saved_change_restarts_the_edit_window(self) -> None:
        await self._start_editing()
        edit_mode_store.activate(OWNER, now=time.monotonic() - 25 * 60)
        index = self._index("c.wallet", "btn_wallet_topup")
        await self._owner(f"ed:a:c:c.wallet:{index}:d")
        self.assertEqual(edit_mode_store.minutes_left(OWNER), 30)
        edit_mode_store.activate(OWNER, now=time.monotonic() - 25 * 60)
        await self._owner(f"ed:a:t:c.wallet:{index}")
        async with self.Session() as session:
            await ui_module.owner_label_text(_message("💳 شارژ"), self.state, session, MagicMock(id=OWNER.user_id))
        self.assertEqual(edit_mode_store.minutes_left(OWNER), 30)

    async def test_owner_changes_a_color_and_a_label_from_the_menu(self) -> None:
        await self._start_editing()
        index = self._index("c.wallet", "btn_wallet_topup")
        await self._owner(f"ed:a:c:c.wallet:{index}:d")
        self.assertEqual(await self._platform("btn_style_wallet_topup"), "danger")
        await self._owner(f"ed:a:t:c.wallet:{index}")
        self.assertEqual(await self.state.get_state(), ui_module.EditorStates.owner_label.state)
        message = _message("💳 شارژ")
        async with self.Session() as session:
            await ui_module.owner_label_text(message, self.state, session, MagicMock(id=OWNER.user_id))
        self.assertEqual(await self._platform("btn_wallet_topup"), "💳 شارژ")
        self.assertIsNone(await self.state.get_state())
        # The same menu's edit panel comes back with the new text and a way back to the menu.
        self.assertTrue(message.answer.await_args.args[0].startswith(ui_module.TEXT_LABEL_SAVED))
        markup = message.answer.await_args.kwargs["reply_markup"]
        self.assertIn("💳 شارژ", [b.text.split(" ", 1)[1] for row in markup.inline_keyboard for b in row])
        self.assertEqual(markup.inline_keyboard[-1][0].callback_data, "nv:w:home")

    async def test_owner_renames_the_admin_panel_button_from_the_admin_main_menu(self) -> None:
        await self._start_editing()
        texts = [b.text for b in screen_buttons("a.main", ScreenContext(ui=dict(DEFAULT_SETTINGS)))]
        self.assertIn(DEFAULT_SETTINGS["btn_admin"], texts)
        self.assertNotIn(DEFAULT_SETTINGS["btn_reseller_apply"], texts)
        panel = await self._owner("ed:a:o:a.main")
        self.assertFalse(self._alerted(panel))
        index = self._index("a.main", "btn_admin")
        await self._owner(f"ed:a:t:a.main:{index}")
        async with self.Session() as session:
            await ui_module.owner_label_text(_message("🧰 مدیریت"), self.state, session, MagicMock(id=OWNER.user_id))
        self.assertEqual(await self._platform("btn_admin"), "🧰 مدیریت")

    async def test_tapping_a_menu_button_leaves_the_editor_and_lets_the_menu_open(self) -> None:
        await self._start_editing()
        await self._owner(f"ed:a:t:c.wallet:{self._index('c.wallet', 'btn_wallet_topup')}")
        for tapped in ("⬅️ بازگشت", "👥 کاربران", "🏠 منوی اصلی"):
            with self.subTest(tapped=tapped):
                await self.state.set_state(ui_module.EditorStates.owner_label)
                async with self.Session() as session:
                    with self.assertRaises(ui_module.SkipHandler):
                        await ui_module.owner_label_text(_message(tapped), self.state, session, MagicMock(id=OWNER.user_id))
                self.assertIsNone(await self.state.get_state())
        self.assertIsNone(await self._platform("btn_wallet_topup"))

    async def test_label_that_repeats_another_button_is_refused(self) -> None:
        await self._start_editing()
        index = self._index("c.wallet", "btn_wallet_topup")
        await self._owner(f"ed:a:t:c.wallet:{index}")
        other = screen_buttons("c.wallet", ScreenContext(ui=dict(DEFAULT_SETTINGS)))[index + 1].text
        # Typed with a premium emoji, so it is a new name rather than a tap on that button.
        message = _message(other, premium=True)
        async with self.Session() as session:
            await ui_module.owner_label_text(message, self.state, session, MagicMock(id=OWNER.user_id))
        self.assertIsNone(await self._platform("btn_wallet_topup"))
        self.assertEqual(await self.state.get_state(), ui_module.EditorStates.owner_label.state)

    async def test_cancel_leaves_the_label_unchanged(self) -> None:
        await self._start_editing()
        await self._owner(f"ed:a:t:c.wallet:{self._index('c.wallet', 'btn_wallet_topup')}")
        async with self.Session() as session:
            await ui_module.owner_label_text(_message("انصراف"), self.state, session, MagicMock(id=OWNER.user_id))
        self.assertIsNone(await self._platform("btn_wallet_topup"))
        self.assertIsNone(await self.state.get_state())

    async def test_back_from_a_prompt_stops_waiting_for_the_text(self) -> None:
        await self._start_editing()
        index = self._index("c.wallet", "btn_wallet_topup")
        for prompt, back in ((f"ed:a:t:c.wallet:{index}", f"ed:a:b:c.wallet:{index}"), ("ed:a:n:c.wallet", "ed:a:o:c.wallet")):
            with self.subTest(prompt=prompt):
                await self._owner(prompt)
                self.assertEqual(await self.state.get_state(), ui_module.EditorStates.owner_label.state)
                await self._owner(back)
                self.assertIsNone(await self.state.get_state())

    async def test_typed_cancel_of_a_name_shows_the_button_like_its_back(self) -> None:
        await self._start_editing()
        index = self._index("c.wallet", "btn_wallet_topup")
        await self._owner(f"ed:a:t:c.wallet:{index}")
        message = _message("انصراف")
        async with self.Session() as session:
            await ui_module.owner_label_text(message, self.state, session, MagicMock(id=OWNER.user_id))
        typed = message.answer.await_args.kwargs["reply_markup"]
        tapped = await self._owner(f"ed:a:b:c.wallet:{index}")
        shown = tapped.message.edit_text.await_args
        self.assertEqual(typed, shown.kwargs["reply_markup"])

    async def test_shop_owner_edits_only_their_own_shop(self) -> None:
        await self._start_editing()
        index = self._index("c.wallet", "btn_wallet_topup")
        await self._shop(f"ed:r:c:c.wallet:{index}:d")
        await self._shop(f"ed:r:t:c.wallet:{index}")
        async with self.Session() as session:
            with patch.object(ui_module, "shop_settings_shop_id", AsyncMock(return_value=SHOP_A)):
                await ui_module.shop_label_text(
                    _message("💳 شارژ"), self.state, session, MagicMock(id=SHOP_OWNER.user_id),
                    is_reseller_bot=True, reseller_owner_id=SHOP_A,
                )
        self.assertEqual(await self._shop_value(SHOP_A, "btn_style_wallet_topup"), "danger")
        self.assertEqual(await self._shop_value(SHOP_A, "btn_wallet_topup"), "💳 شارژ")
        self.assertIsNone(await self._shop_value(SHOP_B, "btn_wallet_topup"))
        self.assertIsNone(await self._platform("btn_wallet_topup"))
        self.assertIsNone(await self._platform("btn_style_wallet_topup"))

    async def test_shop_owner_cannot_open_admin_menus_or_work_from_the_main_bot(self) -> None:
        await self._start_editing()
        self.assertTrue(self._alerted(await self._shop("ed:r:o:a.home")))
        self.assertTrue(self._alerted(await self._shop("ed:r:o:a.main")))
        self.assertTrue(self._alerted(await self._shop("ed:r:h", on_shop_bot=False)))
        self.assertTrue(self._alerted(await self._shop("ed:r:h", has_access=False)))
        self.assertTrue(self._alerted(await self._shop("ed:r:f1")))
        self.assertEqual(await self._platform(SCREEN_EDITOR_SETTING), "1")

    async def test_forged_or_stale_button_numbers_are_refused(self) -> None:
        await self._start_editing()
        for data in ("ed:a:c:c.wallet:99:d", "ed:a:t:c.wallet:-1", "ed:a:c:c.wallet:0:z"):
            with self.subTest(data=data):
                self.assertTrue(self._alerted(await self._owner(data)))
        self.assertIsNone(await self._platform("btn_style_wallet_topup"))

    async def test_malformed_callbacks_are_rejected(self) -> None:
        for data in ("ed:x:h", "ed:a:o:no.such", "ed:a:b:c.wallet:one", "ed:a:c:c.wallet:0", "other", None):
            with self.subTest(data=data):
                self.assertIsNone(ui_module.parse_request(data))



class EditorInputRoutingTests(unittest.IsolatedAsyncioTestCase):
    """A new button name goes through the bot's real router order and reaches the editor."""

    async def asyncSetUp(self) -> None:
        from app.db import Base
        import app.db.models  # noqa: F401  (registers every table on Base)
        from aiogram import Bot, Dispatcher

        import app.bot as bot_package

        self._tmpdir = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmpdir.name) / 'routing.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add(BotUser(id=OWNER.user_id, telegram_id=OWNER.user_id, referral_code="own", role="admin"))
            session.add(BotUser(id=SHOP_OWNER.user_id, telegram_id=SHOP_OWNER.user_id, referral_code="shp", role="reseller"))
            await session.commit()
        self.on_shop_bot = False
        clear_settings_cache()
        self.routers = bot_package.bot_routers()
        for router in self.routers:
            router._parent_router = None  # module routers attach once; another test may have attached them
        self.dp = Dispatcher(storage=MemoryStorage())
        for router in self.routers:
            self.dp.include_router(router)

        async def inject(handler, event, data):
            async with self.Session() as session:
                # A real admin, so the menu navigation knows every admin label and would match them.
                user_id = SHOP_OWNER.user_id if self.on_shop_bot else OWNER.user_id
                db_user = await session.get(BotUser, user_id)
                data.update(session=session, db_user=db_user, is_reseller_bot=self.on_shop_bot,
                            reseller_owner_id=SHOP_A if self.on_shop_bot else None)
                return await handler(event, data)

        self.dp.update.outer_middleware(inject)
        for gate in (bot_package._RequireBotOwnerPrincipal, bot_package._BlockPlatformAdminOnResellerBot):
            patcher = patch.object(gate, "__call__", lambda _self, handler, event, data: handler(event, data))
            patcher.start()
            self.addCleanup(patcher.stop)
        answer = patch.object(Message, "answer", AsyncMock())
        self.answer = answer.start()
        self.addCleanup(answer.stop)
        self.bot = Bot("123456:TEST")
        edit_mode_store.activate(OWNER)
        edit_mode_store.activate(SHOP_OWNER)
        shop_scope = patch.object(ui_module, "shop_settings_shop_id", AsyncMock(return_value=SHOP_A))
        shop_scope.start()
        self.addCleanup(shop_scope.stop)

    async def asyncTearDown(self) -> None:
        for router in self.routers:
            router._parent_router = None
        edit_mode_store.deactivate(OWNER)
        edit_mode_store.deactivate(SHOP_OWNER)
        clear_settings_cache()
        await self.engine.dispose()
        self._tmpdir.cleanup()

    async def _type_new_name(
        self, screen_id: str, label_key: str, text: str, *, premium: bool = False, nav_level: str = nav.NAV_ADMIN
    ) -> None:
        context = ScreenContext(ui=dict(DEFAULT_SETTINGS), profile=sample_reseller_profile())
        index = next(i for i, b in enumerate(screen_buttons(screen_id, context)) if b.label_key == label_key)
        user_id = SHOP_OWNER.user_id if self.on_shop_bot else OWNER.user_id
        state = FSMContext(storage=self.dp.storage, key=StorageKey(bot_id=self.bot.id, chat_id=user_id, user_id=user_id))
        await state.set_data({})
        # The admin is inside that menu, where the menu navigation knows its labels.
        await nav.set_nav_level(state, nav_level)
        await state.set_state(ui_module.EditorStates.shop_label if self.on_shop_bot else ui_module.EditorStates.owner_label)
        await state.update_data(ed_screen=screen_id, ed_title=False, ed_index=index, ed_key=label_key)
        user = User(id=user_id, is_bot=False, first_name="editor")
        message = Message(
            message_id=1, date=datetime.now(timezone.utc), chat=Chat(id=user_id, type="private"), from_user=user,
            text=text, entities=_premium_entities(text) if premium else None,
        ).as_(self.bot)
        await self.dp.feed_update(self.bot, Update(update_id=1, message=message))

    async def _stored(self, key: str) -> str | None:
        async with self.Session() as session:
            return await session.scalar(select(Setting.value).where(Setting.key == key))

    async def test_a_new_name_with_the_letters_of_a_menu_label_is_saved(self) -> None:
        cases = [
            ("a.people", "btn_adm_users", "👍 کاربران", nav.NAV_ADMIN_PEOPLE),
            ("a.people", "btn_back", "🔙 بازگشت", nav.NAV_ADMIN_PEOPLE),
            ("c.main", "btn_restart", "♻️ شروع مجدد", nav.NAV_MAIN),
        ]
        for screen_id, label_key, text, level in cases:
            with self.subTest(text=text):
                await self._type_new_name(screen_id, label_key, text, nav_level=level)
                self.assertEqual(await self._stored(label_key), text)

    async def test_a_shop_owner_on_the_shop_bot_saves_a_back_button_name(self) -> None:
        self.on_shop_bot = True
        await self._type_new_name("r.settings", "btn_back", "🔙 بازگشت", nav_level=nav.NAV_RESELLER_SETTINGS)
        async with self.Session() as session:
            stored = await session.scalar(select(ResellerSetting.value).where(
                ResellerSetting.reseller_user_id == SHOP_A, ResellerSetting.key == "btn_back"))
        self.assertEqual(stored, "🔙 بازگشت")
        self.assertIsNone(await self._stored("btn_back"))

    async def test_a_premium_emoji_name_is_saved_with_its_emoji(self) -> None:
        await self._type_new_name("a.people", "btn_adm_users", "👥 کاربران", premium=True, nav_level=nav.NAV_ADMIN_PEOPLE)
        stored = await self._stored("btn_adm_users")
        ui = {"btn_adm_users": stored}
        self.assertEqual(button_icon_custom_emoji_id(stored), PREMIUM_EMOJI_ID)
        self.assertEqual(kb._t(ui, "btn_adm_users"), "کاربران")


class EntryPointTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_opens_edit_mode_from_the_admin_home(self) -> None:
        from app.bot.nav_inline import admin_groups_hub_keyboard

        data = [b.callback_data for row in admin_groups_hub_keyboard(default_ui()).inline_keyboard for b in row]
        self.assertIn("ed:a:h", data)
        _text, markup = ui_module.switches_view(OWNER, feature_on=True, minutes_left=0)
        self.assertEqual(markup.inline_keyboard[-1][0].callback_data, "nv:adm:home")

    async def _shop_menu_section_data(self, *, feature_on: bool) -> list[str]:
        from app.bot.handlers import reseller_settings

        callback = _callback("res:st:sec:menu")
        with patch.object(reseller_settings, "is_editor_enabled", AsyncMock(return_value=feature_on)):
            await reseller_settings._render_section(callback, MagicMock(), "menu", SHOP_A)
        markup = callback.message.edit_text.await_args.kwargs["reply_markup"]
        return [b.callback_data for row in markup.inline_keyboard for b in row]

    async def test_owner_edits_the_main_menu_as_an_admin_and_as_a_customer(self) -> None:
        _text, markup = ui_module.switches_view(OWNER, feature_on=True, minutes_left=10)
        data = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("ed:a:o:a.main", data)
        self.assertIn("ed:a:o:c.main", data)
        _text, markup = ui_module.switches_view(SHOP_OWNER, feature_on=True, minutes_left=10)
        data = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("ed:r:o:c.main", data)
        self.assertNotIn("ed:r:o:a.main", data)

    async def test_shop_owner_sees_the_entry_only_while_the_feature_is_on(self) -> None:
        self.assertIn("ed:r:h", await self._shop_menu_section_data(feature_on=True))
        self.assertNotIn("ed:r:h", await self._shop_menu_section_data(feature_on=False))

    async def test_owner_settings_no_longer_carry_the_entry(self) -> None:
        from app.bot.handlers import admin_settings

        callback = _callback("adm:st:sec:menu")
        await admin_settings._render_section(callback, MagicMock(), "menu")
        markup = callback.message.edit_text.await_args.kwargs["reply_markup"]
        self.assertNotIn("ed:a:h", [b.callback_data for row in markup.inline_keyboard for b in row])

    async def test_admin_home_with_the_entry_is_shown_to_the_owner_only(self) -> None:
        from app.bot.handlers import reply_nav

        message = MagicMock()
        message.answer = AsyncMock()
        with (
            patch("app.bot.auth.is_bot_owner_principal", AsyncMock(return_value=False)),
            patch("app.bot.nav_inline.present_nav_panel", AsyncMock()) as present,
        ):
            await reply_nav.open_admin_home(message, MagicMock(), MagicMock(id=5))
        present.assert_not_awaited()

    async def test_settings_flow_shortcut_never_skips_the_owner_check_for_editors(self) -> None:
        from app.bot.auth import bot_admin_settings_in_flow

        state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))
        await state.update_data({nav.NAV_LEVEL: nav.NAV_ADMIN_SETTINGS})
        await state.set_state(ui_module.EditorStates.owner_label)
        for data in ("ed:a:h", "ed:a:f1", "bcl:a:g", "bcl:a:s:0:wallet_topup:d"):
            with self.subTest(data=data):
                self.assertFalse(await bot_admin_settings_in_flow(MagicMock(data=data), {"state": state}))
        self.assertFalse(await bot_admin_settings_in_flow(MagicMock(spec=[]), {"state": state}))


if __name__ == "__main__":
    unittest.main()
