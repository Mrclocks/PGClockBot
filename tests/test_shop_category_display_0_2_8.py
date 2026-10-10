"""Shop kind/category inline keyboard must appear on the shop bubble."""

from __future__ import annotations

import inspect
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


class ShopKeyboardAttachTests(unittest.TestCase):
    def test_open_shop_list_attaches_inline_via_present_helper(self):
        from app.bot.handlers import reply_nav, shop

        src = inspect.getsource(reply_nav.open_shop_list)
        self.assertIn("present_shop_kind_picker", src)
        self.assertIn('mode="send"', src)
        helper = inspect.getsource(shop.present_shop_kind_picker)
        self.assertIn("shop_kind_keyboard", helper)
        # Must send inline on the shop message — not ReplyKeyboard-then-edit
        self.assertIn("reply_markup=inline", helper)
        self.assertNotIn("edit_reply_markup(", helper)
        # Option B: single inline message (no lasting shop reply chrome)
        self.assertIn("present_inline_only", helper)
        self.assertNotIn(".delete(", helper)

    def test_shop_list_callback_edits_without_orphan_caption(self):
        from app.bot.handlers import shop

        src = inspect.getsource(shop.shop_list)
        self.assertIn("present_shop_kind_picker", src)
        self.assertIn('mode="edit"', src)
        self.assertNotIn('"فروشگاه:"', src)


class ShopCategoryDisplayFixTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_mode_stale_classic_single_inline(self):
        """Option B: nav_mode=classic no longer attaches shop reply chrome."""
        from app.bot.handlers.shop import present_shop_kind_picker

        message = AsyncMock()
        message.answer = AsyncMock(return_value=AsyncMock())

        with patch(
            "app.bot.handlers.shop.kb.shop_kind_keyboard", return_value="INLINE"
        ):
            await present_shop_kind_picker(
                message,
                ui={"nav_mode": "classic"},
                body="ابتدا دسته را انتخاب کنید",
                fixed_on=True,
                trial_on=False,
                custom_on=False,
                wholesale_on=False,
                categories=[object()],
                mode="send",
            )

        self.assertEqual(message.answer.await_count, 1)
        self.assertEqual(
            message.answer.await_args.kwargs.get("reply_markup"), "INLINE"
        )

    async def test_send_mode_inline_nav_single_message(self):
        from app.bot.handlers.shop import present_shop_kind_picker

        message = AsyncMock()
        message.answer = AsyncMock(return_value=AsyncMock())

        with patch(
            "app.bot.handlers.shop.kb.shop_kind_keyboard", return_value="INLINE"
        ):
            await present_shop_kind_picker(
                message,
                ui={"nav_mode": "inline"},
                body="ابتدا دسته را انتخاب کنید",
                fixed_on=True,
                trial_on=False,
                custom_on=False,
                wholesale_on=False,
                categories=[object()],
                mode="send",
            )

        self.assertEqual(message.answer.await_count, 1)
        self.assertEqual(
            message.answer.await_args.kwargs.get("reply_markup"), "INLINE"
        )

    async def test_edit_mode_still_edits_inline(self):
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
        self.assertEqual(edit.await_args.kwargs.get("reply_markup"), "INLINE")
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
