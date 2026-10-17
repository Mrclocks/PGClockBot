"""Editable menu titles: defaults match the old titles, menus show the setting, editor saves it."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import MessageEntity
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.handlers import screen_editor as ui_module
from app.bot.screen_registry import SCREENS
from app.db.models import ResellerSetting, Setting
from app.services.screen_editor import EditorActor, edit_mode_store
from app.services.screen_titles import (
    SCREEN_TITLE_DEFAULTS,
    screen_heading,
    screen_title,
    title_setting_key,
    validate_screen_title,
)
from app.services.rich_text import custom_emoji_html, pack_rich_text
from app.services.screen_titles import (
    SCREEN_CAPTION_DEFAULTS,
    caption_setting_key,
    screen_caption,
    screen_title_html,
    with_caption,
)
from app.services.users import DEFAULT_SETTINGS, clear_settings_cache

# What the menus rendered before titles became settings; the defaults must keep them byte-for-byte.
OLD_HEADINGS = {
    "c.services": "📦 <b>سرویس‌های شما</b>",
    "a.home": "🛠 <b>پنل ادمین</b>",
    "a.ops": "🗓 <b>عملیات روزانه</b>",
    "a.people": "👤 <b>افراد</b>",
    "a.product": "📦 <b>محصول و پاسارگارد</b>",
    "a.marketing": "🎯 <b>بازاریابی</b>",
    "a.system": "🛠 <b>سیستم</b>",
    "a.pg": "🖥 <b>عملیات پاسارگارد</b>",
    "a.loyalty": "⭐ <b>باشگاه مشتریان</b>",
    "a.users": "👥 <b>کاربران ربات</b>",
    "a.resellers": "🤝 <b>نمایندگان</b>",
    "a.settings": "⚙️ <b>تنظیمات سریع</b>",
    "a.backup": "💾 <b>بکاپ / ریستور</b>",
    "a.broadcast": "📢 <b>پیام گروهی</b>",
    "a.plans": "💎 <b>پلن‌ها</b>",
    "a.plans.users": "👥 <b>پلن‌های کاربران</b>",
    "a.plans.resellers": "🤝 <b>پلن‌های نمایندگان</b>",
    "r.settings": "⚙️ <b>تنظیمات سریع فروشگاه</b>",
    "r.plans": "💎 <b>پلن‌های فروش</b>",
}
OLD_PLAIN_TITLES = {
    "c.wallet": "👛 کیف پول",
    "c.support": "🎧 پشتیبانی",
    "c.loyalty": "⭐ باشگاه مشتریان",
    "r.home": "🤝 پنل نماینده",
}
OWNER = EditorActor(user_id=7, shop_id=None)
SHOP_A = 501
SHOP_OWNER = EditorActor(user_id=8, shop_id=SHOP_A)


class TitleDefaultTests(unittest.TestCase):
    def test_defaults_render_exactly_the_old_titles(self) -> None:
        for screen_id, heading in OLD_HEADINGS.items():
            with self.subTest(screen=screen_id):
                self.assertEqual(screen_heading({}, screen_id), heading)
                self.assertEqual(screen_heading(dict(DEFAULT_SETTINGS), screen_id), heading)
        for screen_id, title in OLD_PLAIN_TITLES.items():
            with self.subTest(screen=screen_id):
                self.assertEqual(screen_title(dict(DEFAULT_SETTINGS), screen_id), title)

    def test_every_title_belongs_to_a_registered_menu(self) -> None:
        self.assertEqual(set(SCREEN_TITLE_DEFAULTS), set(OLD_HEADINGS) | set(OLD_PLAIN_TITLES))
        self.assertLessEqual(set(SCREEN_TITLE_DEFAULTS), set(SCREENS))

    def test_stored_title_wins_and_blank_falls_back(self) -> None:
        key = title_setting_key("a.ops")
        self.assertEqual(screen_heading({key: "🚀 کارهای امروز"}, "a.ops"), "🚀 <b>کارهای امروز</b>")
        self.assertEqual(screen_heading({key: "کارهای امروز"}, "a.ops"), "<b>کارهای امروز</b>")
        self.assertEqual(screen_heading({key: "   "}, "a.ops"), OLD_HEADINGS["a.ops"])

    def test_heading_escapes_whatever_is_stored(self) -> None:
        key = title_setting_key("a.ops")
        self.assertEqual(screen_heading({key: "<i>x</i>"}, "a.ops"), "<b>&lt;i&gt;x&lt;/i&gt;</b>")

    def test_title_validation(self) -> None:
        self.assertEqual(validate_screen_title("  🚀  کارهای   امروز "), "🚀 کارهای امروز")
        for text in ("", "خط\nدوم", "x" * 65, "<b>bold</b>", "A & B"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                validate_screen_title(text)


class MenuRendersTitleTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_ops_hub_shows_the_stored_title(self) -> None:
        from app.bot.handlers import reply_nav

        ui = dict(DEFAULT_SETTINGS)
        ui[title_setting_key("a.ops")] = "🚀 کارهای امروز"
        with (
            patch.object(reply_nav, "_deny_unless_owner", AsyncMock(return_value=True)),
            patch.object(reply_nav, "get_all_settings", AsyncMock(return_value=ui)),
            patch("app.services.admin_counters.pending_counts", AsyncMock(side_effect=RuntimeError)),
            patch("app.bot.nav_inline.present_nav_panel", AsyncMock()) as present,
        ):
            await reply_nav.open_admin_ops_hub(MagicMock(), MagicMock(), MagicMock(id=1), None)
        self.assertTrue(present.await_args.kwargs["text"].startswith("🚀 <b>کارهای امروز</b>\n"))

    async def test_wallet_home_shows_the_stored_title(self) -> None:
        from app.bot.handlers import reply_nav

        ui = dict(DEFAULT_SETTINGS)
        ui[title_setting_key("c.wallet")] = "💰 حساب من"
        with (
            patch.object(reply_nav, "get_all_settings", AsyncMock(return_value=ui)),
            patch("app.services.wallet.wallet_balance_for_context", AsyncMock(return_value=0)),
            patch("app.bot.nav_inline.present_inline_only", AsyncMock()) as present,
        ):
            await reply_nav.open_wallet_home(MagicMock(), MagicMock(), MagicMock(id=1), None)
        self.assertIn("<b>💰 حساب من</b>", present.await_args.kwargs["text"])

    def test_backup_panel_shows_its_title_once(self) -> None:
        from app.bot.handlers.admin_backup import _hub_text

        text = _hub_text([], dict(DEFAULT_SETTINGS))
        self.assertEqual(text.count("بکاپ / ریستور</b>"), 1)
        self.assertTrue(text.startswith(OLD_HEADINGS["a.backup"]))


PREMIUM_ID = "5368324170671202286"


def _premium(text: str) -> list[MessageEntity]:
    """The first emoji of ``text`` as a Telegram Premium custom emoji (two UTF-16 units)."""
    return [MessageEntity(type="custom_emoji", offset=0, length=2, custom_emoji_id=PREMIUM_ID)]


class PremiumAndCaptionRenderingTests(unittest.TestCase):
    def test_premium_emoji_is_kept_and_everything_else_is_escaped(self) -> None:
        text = "👍 کارها <b>"
        html_text = custom_emoji_html(text, _premium(text))
        self.assertEqual(html_text, f'<tg-emoji emoji-id="{PREMIUM_ID}">👍</tg-emoji> کارها &lt;b&gt;')

    def test_premium_title_keeps_its_emoji_in_headings_and_cards(self) -> None:
        ui = {title_setting_key("a.ops"): pack_rich_text("👍 کارهای امروز", _premium("👍"))}
        emoji = f'<tg-emoji emoji-id="{PREMIUM_ID}">👍</tg-emoji>'
        self.assertEqual(screen_heading(ui, "a.ops"), f"{emoji} <b>کارهای امروز</b>")
        self.assertEqual(screen_title_html(ui, "a.ops"), f"{emoji} کارهای امروز")
        self.assertEqual(screen_title(ui, "a.ops"), "👍 کارهای امروز")

    def test_caption_default_custom_hidden_and_escaped(self) -> None:
        key = caption_setting_key("a.people")
        self.assertEqual(screen_caption(dict(DEFAULT_SETTINGS), "a.people"), "مدیریت کاربران ربات و نمایندگان.")
        self.assertEqual(screen_caption({key: "   "}, "a.people"), "مدیریت کاربران ربات و نمایندگان.")
        self.assertEqual(screen_caption({key: "مشتری‌ها\nو <نماینده‌ها>"}, "a.people"), "مشتری‌ها\nو &lt;نماینده‌ها&gt;")
        self.assertEqual(screen_caption({key: "-"}, "a.people"), "")
        self.assertEqual(with_caption("🗓 <b>x</b>", screen_caption({key: "-"}, "a.people")), "🗓 <b>x</b>")

    def test_every_titled_menu_has_a_caption_and_the_support_menu_reuses_its_text(self) -> None:
        self.assertEqual(set(SCREEN_CAPTION_DEFAULTS) | {"c.support"}, set(SCREEN_TITLE_DEFAULTS))
        self.assertEqual(caption_setting_key("c.support"), "support_text")
        for screen_id, caption in SCREEN_CAPTION_DEFAULTS.items():
            with self.subTest(screen=screen_id):
                self.assertEqual(DEFAULT_SETTINGS[caption_setting_key(screen_id)], caption)


class MenuCaptionTests(unittest.IsolatedAsyncioTestCase):
    async def _people_hub(self, ui: dict) -> str:
        from app.bot.handlers import reply_nav

        with (
            patch.object(reply_nav, "_deny_unless_owner", AsyncMock(return_value=True)),
            patch.object(reply_nav, "get_all_settings", AsyncMock(return_value=ui)),
            patch("app.bot.nav_inline.present_nav_panel", AsyncMock()) as present,
        ):
            await reply_nav.open_admin_people_hub(MagicMock(), MagicMock(), MagicMock(id=1), None)
        return present.await_args.kwargs["text"]

    async def test_the_people_menu_shows_the_stored_or_hidden_caption(self) -> None:
        ui = dict(DEFAULT_SETTINGS)
        self.assertEqual(await self._people_hub(ui), "👤 <b>افراد</b>\nمدیریت کاربران ربات و نمایندگان.")
        ui[caption_setting_key("a.people")] = pack_rich_text("👍 مشتری‌ها و همکاران", _premium("👍"))
        self.assertEqual(
            await self._people_hub(ui), f'👤 <b>افراد</b>\n<tg-emoji emoji-id="{PREMIUM_ID}">👍</tg-emoji> مشتری‌ها و همکاران'
        )
        ui[caption_setting_key("a.people")] = "-"
        self.assertEqual(await self._people_hub(ui), "👤 <b>افراد</b>")

    async def test_the_plans_menu_shows_one_caption_from_both_entry_points(self) -> None:
        from app.bot.handlers import admin_plans, reply_nav

        ui = dict(DEFAULT_SETTINGS)
        ui[caption_setting_key("a.plans")] = "یک مخاطب انتخاب کنید."
        state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))
        with (
            patch.object(reply_nav, "_deny_unless_owner", AsyncMock(return_value=True)),
            patch.object(reply_nav, "get_all_settings", AsyncMock(return_value=ui)),
            patch("app.bot.nav_inline.present_nav_panel", AsyncMock()) as present,
        ):
            await reply_nav.open_admin_plans_hub(MagicMock(), MagicMock(), MagicMock(id=1), state)
        with (
            patch.object(admin_plans, "get_all_settings", AsyncMock(return_value=ui)),
            patch("app.bot.nav_inline.present_inline_only", AsyncMock()) as inline,
        ):
            await admin_plans.sync_plans_reply_keyboard(MagicMock(), MagicMock(), MagicMock(id=1), state)
        expected = "💎 <b>پلن‌ها</b>\nیک مخاطب انتخاب کنید."
        self.assertEqual(present.await_args.kwargs["text"], expected)
        self.assertEqual(inline.await_args.kwargs["text"], expected)

    async def test_the_shop_settings_panel_says_each_sentence_once(self) -> None:
        from app.bot.handlers import reseller_settings

        profile = MagicMock(user_id=5, bot_username="shop_bot")
        with (
            patch.object(reseller_settings, "get_all_settings", AsyncMock(return_value=dict(DEFAULT_SETTINGS))),
            patch("app.services.resellers.get_reseller_panel_base_url", AsyncMock(return_value="")),
        ):
            text, _markup = await reseller_settings._hub_panel(MagicMock(), profile)
        self.assertEqual(text.count("ظاهر، رنگ، گزارش روزانه → وب‌پنل."), 1)
        self.assertTrue(text.startswith("⚙️ <b>تنظیمات سریع فروشگاه</b>\nربات: <code>@shop_bot</code>\n"))


def _callback(data: str) -> MagicMock:
    callback = MagicMock()
    callback.data = data
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()
    return callback


def _message(text: str, *, premium: bool = False) -> MagicMock:
    message = MagicMock()
    message.text = text
    message.entities = _premium(text) if premium else None
    message.answer = AsyncMock()
    return message


class TitleEditorFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from app.db import Base
        import app.db.models  # noqa: F401  (registers every table on Base)

        self._tmpdir = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmpdir.name) / 'titles.db'}")
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

    async def _start_editing(self) -> None:
        await self._owner("ed:a:f1")
        await self._owner("ed:a:m1")
        edit_mode_store.activate(SHOP_OWNER)

    async def _owner_text(self, text: str) -> MagicMock:
        message = _message(text)
        async with self.Session() as session:
            await ui_module.owner_label_text(message, self.state, session, MagicMock(id=OWNER.user_id))
        return message

    async def _platform(self, key: str) -> str | None:
        async with self.Session() as session:
            return await session.scalar(select(Setting.value).where(Setting.key == key))

    async def _shop_value(self, key: str) -> str | None:
        async with self.Session() as session:
            return await session.scalar(
                select(ResellerSetting.value).where(ResellerSetting.reseller_user_id == SHOP_A, ResellerSetting.key == key)
            )

    async def test_panel_offers_the_title_first(self) -> None:
        await self._start_editing()
        callback = await self._owner("ed:a:o:a.ops")
        markup = callback.message.edit_text.await_args.kwargs["reply_markup"]
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, "ed:a:n:a.ops")
        self.assertIn("🗓 عملیات روزانه", markup.inline_keyboard[0][0].text)

    async def test_owner_changes_a_title_and_returns_to_the_menu(self) -> None:
        await self._start_editing()
        edit_mode_store.activate(OWNER, now=time.monotonic() - 25 * 60)
        await self._owner("ed:a:n:a.ops")
        message = await self._owner_text("🚀 کارهای امروز")
        self.assertEqual(await self._platform(title_setting_key("a.ops")), "🚀 کارهای امروز")
        self.assertEqual(edit_mode_store.minutes_left(OWNER), 30)
        self.assertIsNone(await self.state.get_state())
        markup = message.answer.await_args.kwargs["reply_markup"]
        self.assertIn("🚀 کارهای امروز", markup.inline_keyboard[0][0].text)
        self.assertEqual(markup.inline_keyboard[-1][0].callback_data, "nv:adm:ops")

    async def test_unsafe_title_is_refused_and_input_stays_open(self) -> None:
        await self._start_editing()
        await self._owner("ed:a:n:a.ops")
        await self._owner_text("<b>x</b>")
        self.assertIsNone(await self._platform(title_setting_key("a.ops")))
        self.assertEqual(await self.state.get_state(), ui_module.EditorStates.owner_label.state)

    async def test_button_edit_after_a_title_edit_saves_a_button_label(self) -> None:
        await self._start_editing()
        await self._owner("ed:a:n:a.ops")
        await self._owner_text("🚀 کارهای امروز")
        await self._owner("ed:a:t:c.wallet:0")
        await self._owner_text("💳 شارژ سریع")
        self.assertEqual(await self._platform("btn_wallet_topup"), "💳 شارژ سریع")
        self.assertIsNone(await self._platform(title_setting_key("c.wallet")))

    async def test_shop_owner_titles_stay_in_their_shop(self) -> None:
        await self._start_editing()
        with patch.object(ui_module, "shop_settings_shop_id", AsyncMock(return_value=SHOP_A)):
            async with self.Session() as session:
                await ui_module.shop_editor(
                    _callback("ed:r:n:c.wallet"), session, MagicMock(id=SHOP_OWNER.user_id), self.state,
                    is_reseller_bot=True, reseller_owner_id=SHOP_A,
                )
                await ui_module.shop_label_text(
                    _message("💰 حساب من"), self.state, session, MagicMock(id=SHOP_OWNER.user_id),
                    is_reseller_bot=True, reseller_owner_id=SHOP_A,
                )
                refused = _callback("ed:r:n:a.ops")
                await ui_module.shop_editor(
                    refused, session, MagicMock(id=SHOP_OWNER.user_id), self.state,
                    is_reseller_bot=True, reseller_owner_id=SHOP_A,
                )
        self.assertEqual(await self._shop_value(title_setting_key("c.wallet")), "💰 حساب من")
        self.assertIsNone(await self._platform(title_setting_key("c.wallet")))
        self.assertTrue(refused.answer.await_args.kwargs.get("show_alert"))


class CaptionEditorFlowTests(TitleEditorFlowTests):
    """Captions and premium titles through the editor, reusing the title flow's database and helpers."""

    async def test_the_panel_offers_the_caption_after_the_title(self) -> None:
        await self._start_editing()
        callback = await self._owner("ed:a:o:a.ops")
        row = callback.message.edit_text.await_args.kwargs["reply_markup"].inline_keyboard[1][0]
        self.assertEqual(row.callback_data, "ed:a:d:a.ops")
        self.assertIn("داشبورد، سفارش‌ها", row.text)

    async def test_owner_changes_a_caption_with_a_premium_emoji(self) -> None:
        await self._start_editing()
        await self._owner("ed:a:d:a.ops")
        message = _message("👍 کارهای روزانهٔ ادمین", premium=True)
        async with self.Session() as session:
            await ui_module.owner_label_text(message, self.state, session, MagicMock(id=OWNER.user_id))
        stored = await self._platform(caption_setting_key("a.ops"))
        self.assertIn(PREMIUM_ID, stored)
        self.assertEqual(screen_caption({caption_setting_key("a.ops"): stored}, "a.ops"),
                         f'<tg-emoji emoji-id="{PREMIUM_ID}">👍</tg-emoji> کارهای روزانهٔ ادمین')
        self.assertTrue(message.answer.await_args.args[0].startswith(ui_module.TEXT_CAPTION_SAVED))

    async def test_owner_saves_a_premium_title(self) -> None:
        await self._start_editing()
        await self._owner("ed:a:n:a.ops")
        message = _message("👍 کارهای امروز", premium=True)
        async with self.Session() as session:
            await ui_module.owner_label_text(message, self.state, session, MagicMock(id=OWNER.user_id))
        stored = await self._platform(title_setting_key("a.ops"))
        self.assertEqual(screen_heading({title_setting_key("a.ops"): stored}, "a.ops"),
                         f'<tg-emoji emoji-id="{PREMIUM_ID}">👍</tg-emoji> <b>کارهای امروز</b>')

    async def test_hide_and_reset_the_caption(self) -> None:
        await self._start_editing()
        hidden = await self._owner("ed:a:dh:a.ops")
        self.assertEqual(await self._platform(caption_setting_key("a.ops")), "-")
        self.assertEqual(hidden.answer.await_args.args[0], ui_module.TEXT_CAPTION_HIDDEN)
        await self._owner("ed:a:dr:a.ops")
        self.assertEqual(await self._platform(caption_setting_key("a.ops")), SCREEN_CAPTION_DEFAULTS["a.ops"])
        await self._owner("ed:a:nr:a.ops")
        self.assertEqual(await self._platform(title_setting_key("a.ops")), DEFAULT_SETTINGS[title_setting_key("a.ops")])

    async def test_the_support_caption_is_the_support_text_and_stays_html_safe(self) -> None:
        await self._start_editing()
        await self._owner("ed:a:d:c.support")
        await self._owner_text("سؤال دارید؟ <اینجا> بپرسید")
        self.assertIsNone(await self._platform("support_text"))
        await self._owner_text("سؤال دارید؟ همین‌جا بپرسید.")
        self.assertEqual(await self._platform("support_text"), "سؤال دارید؟ همین‌جا بپرسید.")
        refused = await self._owner("ed:a:dh:c.support")
        self.assertTrue(refused.answer.await_args.kwargs.get("show_alert"))

    async def test_shop_owner_captions_stay_in_their_shop(self) -> None:
        await self._owner("ed:a:f1")
        edit_mode_store.activate(SHOP_OWNER)
        with patch.object(ui_module, "shop_settings_shop_id", AsyncMock(return_value=SHOP_A)):
            async with self.Session() as session:
                await ui_module.shop_editor(
                    _callback("ed:r:d:r.settings"), session, MagicMock(id=SHOP_OWNER.user_id), self.state,
                    is_reseller_bot=True, reseller_owner_id=SHOP_A,
                )
                await ui_module.shop_label_text(
                    _message("تنظیمات فروشگاه من."), self.state, session, MagicMock(id=SHOP_OWNER.user_id),
                    is_reseller_bot=True, reseller_owner_id=SHOP_A,
                )
                refused = _callback("ed:r:dh:a.ops")
                await ui_module.shop_editor(
                    refused, session, MagicMock(id=SHOP_OWNER.user_id), self.state,
                    is_reseller_bot=True, reseller_owner_id=SHOP_A,
                )
        self.assertEqual(await self._shop_value(caption_setting_key("r.settings")), "تنظیمات فروشگاه من.")
        self.assertIsNone(await self._platform(caption_setting_key("r.settings")))
        self.assertTrue(refused.answer.await_args.kwargs.get("show_alert"))


if __name__ == "__main__":
    unittest.main()
