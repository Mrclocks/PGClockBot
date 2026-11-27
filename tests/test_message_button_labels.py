"""Group-message buttons: screen buttons show the sender's menu labels, and the bot can add custom link buttons."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, MessageEntity

from app.bot import message_button_picker as picker
from app.db.models import ResellerSetting
from app.services.message_buttons import (
    SCREENS,
    ButtonInput,
    ButtonSpec,
    build_keyboard,
    dump_buttons,
    load_buttons,
    make_buttons,
    preview_keyboard,
    screen_labels,
    stored_keyboard,
)
from app.services.rich_text import pack_rich_text
from app.services.users import clear_settings_cache
from tests.test_message_buttons import LINKS, NO_LINKS, SHOP_A, SHOP_B, DbCase

PREMIUM_ID = "5368324170671202286"


def premium_label(text: str) -> str:
    """A menu label whose first character is a premium (custom) emoji, as the bot editor saves it."""
    entity = MessageEntity(type="custom_emoji", offset=0, length=2, custom_emoji_id=PREMIUM_ID)
    return pack_rich_text(text, [entity])


class ScreenLabelTests(unittest.TestCase):
    def test_a_screen_takes_its_menu_label_and_premium_emoji(self) -> None:
        labels = screen_labels({"btn_shop": "🟢🛒 خرید اشتراک", "btn_wallet": premium_label("👛 کیف من")})
        self.assertEqual(labels["shop"].text, "🟢🛒 خرید اشتراک")
        self.assertIsNone(labels["shop"].icon)
        self.assertEqual((labels["wallet"].text, labels["wallet"].icon), ("کیف من", PREMIUM_ID))
        # No menu label of its own, or an empty setting: the fixed label.
        self.assertEqual(labels["custom"].text, SCREENS["custom"][1])
        self.assertEqual(screen_labels({"btn_faq": "  "})["faq"].text, SCREENS["faq"][1])

    def test_an_empty_label_shows_the_menu_label_and_a_typed_one_is_kept(self) -> None:
        labels = screen_labels({"btn_shop": "🛍 فروشگاه ما", "btn_wallet": premium_label("👛 کیف من")})
        buttons = make_buttons([ButtonInput("shop"), ButtonInput("support", "سؤال؟"), ButtonInput("wallet")], NO_LINKS)
        row = [b for r in build_keyboard(buttons, labels) for b in r]
        self.assertEqual([b.text for b in row], ["🛍 فروشگاه ما", "سؤال؟", "کیف من"])
        self.assertEqual(row[2].icon_custom_emoji_id, PREMIUM_ID)
        self.assertIsNone(row[0].icon_custom_emoji_id)

    def test_a_button_stored_with_the_old_fixed_label_also_follows_the_menu(self) -> None:
        old = ButtonSpec("screen", SCREENS["shop"][1], "shop")
        row = build_keyboard([old], screen_labels({"btn_shop": "🛍 فروشگاه ما"}))
        self.assertEqual(row[0][0].text, "🛍 فروشگاه ما")

    def test_without_labels_the_fixed_label_is_shown(self) -> None:
        row = build_keyboard(make_buttons([ButtonInput("shop")], NO_LINKS))
        self.assertEqual(row[0][0].text, SCREENS["shop"][1])


class PickerPremiumEmojiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.labels = screen_labels({"btn_wallet": premium_label("👛 کیف من"), "btn_shop": "🛒 خرید"})

    def test_the_checklist_shows_each_screens_premium_emoji(self) -> None:
        markup = picker.checklist_markup(["wallet"], renew=False, presets=[], labels=self.labels)
        buttons = {b.callback_data: b for row in markup.inline_keyboard for b in row}
        wallet, shop = buttons["mbp:t:wallet"], buttons["mbp:t:shop"]
        self.assertEqual((wallet.text, wallet.icon_custom_emoji_id), ("\u200fکیف من ✅", PREMIUM_ID))
        self.assertIsNone(shop.icon_custom_emoji_id)

    def test_the_colour_step_keeps_the_premium_emoji(self) -> None:
        labels = picker.option_labels(renew=False, labels=self.labels, links={})
        markup = picker.colors_markup(
            ["wallet", "shop"], {"wallet": "success"}, labels=labels, icons=picker.option_icons(self.labels)
        )
        wallet, shop = markup.inline_keyboard[0][0], markup.inline_keyboard[1][0]
        self.assertEqual((wallet.icon_custom_emoji_id, wallet.style), (PREMIUM_ID, "success"))
        self.assertIsNone(shop.icon_custom_emoji_id)


class LinkPremiumEmojiTests(unittest.TestCase):
    def label_message(self, text: str, entities: list | None) -> MagicMock:
        message = MagicMock(spec=Message)
        message.text, message.entities = text, entities
        return message

    def test_a_premium_emoji_typed_with_the_label_becomes_the_buttons_icon(self) -> None:
        entity = MessageEntity(type="custom_emoji", offset=0, length=2, custom_emoji_id=PREMIUM_ID)
        self.assertEqual(picker.link_label_from_message(self.label_message("📣 کانال ما", [entity])), ("کانال ما", PREMIUM_ID))
        self.assertEqual(picker.link_label_from_message(self.label_message("📣 کانال ما", None)), ("📣 کانال ما", ""))
        # Only a premium emoji: its fallback character stays as the text, without an icon.
        self.assertEqual(picker.link_label_from_message(self.label_message("📣", [entity])), ("📣", ""))

    def test_the_icon_is_stored_with_the_link_and_sent_on_the_button(self) -> None:
        links = {"u1": {"label": "کانال ما", "url": "https://t.me/example", "icon": PREMIUM_ID}}
        [item] = picker.chosen_inputs(["u1"], {}, links)
        stored = dump_buttons(make_buttons([item], LINKS))
        [[button]] = build_keyboard(load_buttons(stored, LINKS))
        self.assertEqual((button.text, button.url, button.icon_custom_emoji_id), ("کانال ما", "https://t.me/example", PREMIUM_ID))
        [[preview]] = preview_keyboard(stored)
        self.assertEqual(preview.icon_custom_emoji_id, PREMIUM_ID)
        self.assertEqual(picker.option_icons(None, links), {"u1": PREMIUM_ID})

    def test_an_icon_that_is_not_a_telegram_id_is_dropped(self) -> None:
        for bad in ("abc", "1" * 40, "12 34", "<b>"):
            with self.subTest(bad=bad):
                [spec] = make_buttons([ButtonInput("link", "x", "https://t.me/a", icon=bad)], LINKS)
                self.assertEqual(spec.icon, "")


class ShopLabelTests(DbCase):
    async def test_each_shop_sends_its_own_menu_label(self) -> None:
        async with self.Session() as session:
            session.add(ResellerSetting(reseller_user_id=SHOP_A, key="btn_shop", value="🛍 خرید از فروشگاه A"))
            await session.commit()
            clear_settings_cache()
            stored = dump_buttons(make_buttons([ButtonInput("shop")], NO_LINKS))
            shop_a = await stored_keyboard(session, stored, shop_id=SHOP_A)
            shop_b = await stored_keyboard(session, stored, shop_id=SHOP_B)
        clear_settings_cache()
        self.assertEqual(shop_a[0][0].text, "🛍 خرید از فروشگاه A")
        self.assertNotEqual(shop_b[0][0].text, "🛍 خرید از فروشگاه A")


def _message(text: str) -> MagicMock:
    message = MagicMock(spec=Message)
    message.text = text
    message.entities = None  # a plain text message
    message.answer = AsyncMock()
    return message


def _callback(data: str) -> MagicMock:
    callback = MagicMock(data=data)
    callback.answer = AsyncMock()
    callback.message = MagicMock(spec=Message)
    callback.message.edit_text = AsyncMock()
    callback.message.edit_reply_markup = AsyncMock()
    return callback


class LinkPickerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))
        await picker.ask_for_buttons(_message(""), self.state, flow="links", shop_id=SHOP_A)
        self.session = MagicMock()
        for name, value in (("sender_screen_labels", {}), ("list_presets", [])):
            patcher = patch.object(picker, name, AsyncMock(return_value=value))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.finished = AsyncMock()
        picker.register_flow("links", self.finished)

    def allow_links(self, allowed: bool) -> None:
        patcher = patch.object(picker, "shop_may_link", AsyncMock(return_value=allowed))
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_a_custom_link_is_added_and_sent_with_the_other_buttons(self) -> None:
        self.allow_links(True)
        await self.state.update_data(mbp_selected=["shop"])
        await picker.on_link(_callback(picker.CB_LINK), self.state, self.session)
        self.assertEqual(await self.state.get_state(), picker.PickerStates.link_label.state)
        await picker.on_link_label(_message("📣 کانال ما"), self.state)
        await picker.on_link_url(_message("https://t.me/example"), self.state, self.session)
        self.assertEqual(await self.state.get_state(), picker.PickerStates.picking.state)
        await picker.on_send(_callback(picker.CB_SEND), self.state, self.session, MagicMock())
        buttons = self.finished.await_args.args[4]
        self.assertEqual(buttons[1], ButtonInput("link", "📣 کانال ما", "https://t.me/example", ""))
        self.assertEqual(buttons[0].target, "shop")

    async def test_a_link_that_is_not_https_is_refused_and_asked_again(self) -> None:
        self.allow_links(True)
        await picker.on_link(_callback(picker.CB_LINK), self.state, self.session)
        await picker.on_link_label(_message("سایت"), self.state)
        for bad in ("http://example.com", "javascript:alert(1)", "https://exa mple.com"):
            reply = _message(bad)
            await picker.on_link_url(reply, self.state, self.session)
            self.assertIn("https://", reply.answer.await_args.args[0])
        self.assertEqual(await self.state.get_state(), picker.PickerStates.link_url.state)
        self.assertEqual((await self.state.get_data())["mbp_links"], {})

    async def test_a_shop_without_the_link_permission_is_not_asked_for_a_link(self) -> None:
        self.allow_links(False)
        callback = _callback(picker.CB_LINK)
        await picker.on_link(callback, self.state, self.session)
        callback.answer.assert_awaited_with(picker.EXPIRED, show_alert=True)
        self.assertEqual(await self.state.get_state(), picker.PickerStates.picking.state)

    async def test_a_link_cannot_pass_the_button_limit(self) -> None:
        self.allow_links(True)
        await self.state.update_data(mbp_selected=["shop", "wallet", "support", "faq"])
        callback = _callback(picker.CB_LINK)
        await picker.on_link(callback, self.state, self.session)
        callback.answer.assert_awaited_with(picker.TOO_MANY, show_alert=True)

    def test_the_checklist_offers_the_link_only_when_allowed(self) -> None:
        def callbacks(may_link: bool) -> list[str]:
            markup = picker.checklist_markup([], renew=False, presets=[], may_link=may_link)
            return [b.callback_data for row in markup.inline_keyboard for b in row]

        self.assertIn(picker.CB_LINK, callbacks(True))
        self.assertNotIn(picker.CB_LINK, callbacks(False))


if __name__ == "__main__":
    unittest.main()
