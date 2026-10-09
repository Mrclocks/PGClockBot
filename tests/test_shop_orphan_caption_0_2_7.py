"""Shop entry must not spam orphan «فروشگاه:» chrome captions."""

from __future__ import annotations

import inspect
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


class ShopOrphanCaptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_present_send_sets_inline_then_lasting_chrome(self):
        from app.bot.handlers.shop import present_shop_kind_picker

        message = AsyncMock()
        shop_msg = AsyncMock()
        chrome = AsyncMock()
        chrome.delete = AsyncMock()
        message.answer = AsyncMock(side_effect=[shop_msg, chrome])

        with patch("app.bot.handlers.shop.kb.shop_reply_keyboard", return_value="REPLY"):
            with patch(
                "app.bot.handlers.shop.kb.shop_kind_keyboard", return_value="INLINE"
            ):
                await present_shop_kind_picker(
                    message,
                    ui={"nav_mode": "classic"},
                    body="body",
                    fixed_on=True,
                    trial_on=False,
                    custom_on=False,
                    wholesale_on=False,
                    mode="send",
                )

        self.assertEqual(message.answer.await_count, 2)
        self.assertEqual(
            message.answer.await_args_list[0].kwargs.get("reply_markup"), "INLINE"
        )
        chrome.delete.assert_not_awaited()

    async def test_present_send_inline_nav_no_chrome_followup(self):
        from app.bot.handlers.shop import present_shop_kind_picker

        message = AsyncMock()
        message.answer = AsyncMock(return_value=AsyncMock())

        with patch(
            "app.bot.handlers.shop.kb.shop_kind_keyboard", return_value="INLINE"
        ):
            await present_shop_kind_picker(
                message,
                ui={"nav_mode": "inline"},
                body="body",
                fixed_on=True,
                trial_on=False,
                custom_on=False,
                wholesale_on=False,
                mode="send",
            )

        self.assertEqual(message.answer.await_count, 1)
        self.assertEqual(
            message.answer.await_args.kwargs.get("reply_markup"), "INLINE"
        )

    async def test_present_edit_does_not_answer_chrome_caption(self):
        from app.bot.handlers.shop import present_shop_kind_picker

        message = AsyncMock()
        message.answer = AsyncMock()
        with patch("app.bot.handlers.shop.safe_edit_text", new_callable=AsyncMock) as edit:
            with patch(
                "app.bot.handlers.shop.kb.shop_kind_keyboard", return_value="INLINE"
            ):
                await present_shop_kind_picker(
                    message,
                    ui={},
                    body="body",
                    fixed_on=True,
                    trial_on=False,
                    custom_on=False,
                    wholesale_on=False,
                    mode="edit",
                )
        edit.assert_awaited_once()
        message.answer.assert_not_awaited()


class ShopOrphanCaptionSourceGuards(unittest.TestCase):
    def test_no_orphan_shop_caption_answers(self):
        shop = Path("app/bot/handlers/shop.py").read_text(encoding="utf-8")
        reply = Path("app/bot/handlers/reply_nav.py").read_text(encoding="utf-8")
        self.assertNotIn('"فروشگاه:"', shop)
        self.assertNotIn('"فروشگاه:"', reply)
        self.assertNotIn("'فروشگاه:'", shop)
        self.assertNotIn("'فروشگاه:'", reply)

    def test_open_shop_list_uses_present_helper(self):
        from app.bot.handlers import reply_nav

        src = inspect.getsource(reply_nav.open_shop_list)
        self.assertIn("present_shop_kind_picker", src)
        self.assertIn('mode="send"', src)

    def test_shop_list_uses_edit_mode(self):
        from app.bot.handlers import shop

        src = inspect.getsource(shop.shop_list)
        self.assertIn("present_shop_kind_picker", src)
        self.assertIn('mode="edit"', src)


if __name__ == "__main__":
    unittest.main()
